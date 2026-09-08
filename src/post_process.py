import re
import shutil
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.job import TranscodeJob
    from src.pipeline import TranscodePipeline
from src import config
from src.path_utils import _final_dest_path, _format_size
from src.probe import get_frame_count
from src.quality import vmaf_verdict
from src.temp_cleanup import (
    _active_temp_dirs,
    _active_temp_dirs_lock,
    _cleanup_temp,
)
from src.vmaf_async import is_shutting_down

log = __import__("logging").getLogger(__name__)


def _land_output(output_path, filepath, filename, input_dir, output_dir):
    """
    把 temp 区产物复制到目标库。成功返回 (final_path, None)，
    失败返回 (None, err_msg)（已打日志）。

    Copy the temp output into the target library. Returns (final_path, None)
    on success, (None, err_msg) on failure (already logged).
    """
    final_path = _final_dest_path(filepath, filename, input_dir, output_dir)
    if final_path.name != filename:
        log.info("DEST EXISTS: using %s instead of %s", final_path.name, filename)
    # 跨文件系统不用 rename，NFS 上不用 copy2/chmod，只用 copyfile
    # No rename across filesystems, no copy2/chmod (may raise EPERM on NFS).
    try:
        shutil.copyfile(str(output_path), str(final_path))
    except OSError as e:
        log.error("FILE MOVE FAILED: %s — %s", final_path.name, e)
        return None, f"File move failed: {e}"
    return final_path, None


def _resolve_output(pipeline, job):
    """
    前置公共段：取产物路径→算 ratio→ratio 门（含 KEEP_TARGET 落盘）。
    返回 (ctx, terminal)：terminal 为 None 表示继续；否则为已记终态的结果。
    产物缺失返回 (None, False)；ratio 超标返回 (ctx, True)。

    Shared preamble: resolve output, compute ratio, apply the ratio gate
    (including KEEP_TARGET landing). Returns (ctx, terminal): terminal None
    means continue; otherwise the already-recorded terminal result.
    Missing output returns (None, False); over-ratio returns (ctx, True).
    """
    history = pipeline.history
    filepath = Path(job.filepath)
    stem = filepath.stem
    expect_filename = f"{stem}.mkv"
    input_size = filepath.stat().st_size
    raw_output = history.get_output_path(str(filepath))
    if raw_output is None or not Path(raw_output).exists():
        history.mark_failed(str(filepath), "output path missing after transcode")
        return None, False
    output_path = Path(raw_output)
    temp_dir = output_path.parent
    output_size = output_path.stat().st_size
    ratio = output_size / input_size if input_size else 0
    ctx = {
        "filepath": filepath,
        "output_path": output_path,
        "temp_dir": temp_dir,
        "output_size": output_size,
        "ratio": ratio,
        "expect_filename": expect_filename,
    }

    # ratio 超标直接拒绝：不打分（三种模式一致，off 下同样拒绝）。
    # KEEP_TARGET=True 时仍落盘到目标库，仅记 rejected。
    # Reject outright on over-ratio: no scoring (all modes, off included).
    # With KEEP_TARGET the file is still landed, only recorded rejected.
    if ratio > config.ACCEPT_RATIO_MAX:
        landed_path = None
        if config.KEEP_TARGET:
            final_path, move_err = _land_output(
                output_path, filepath, expect_filename, job.lib_input, job.lib_output
            )
            if move_err is not None:
                _cleanup_temp(temp_dir)
                history.mark_failed(str(filepath), move_err)
                return ctx, False
            landed_path = str(final_path)
        if output_path.exists():
            output_path.unlink()
        _cleanup_temp(temp_dir)
        history.mark_done(str(filepath), landed_path, vmaf_score=None, rejected=True)
        log.warning(
            "REJECT: %s (ratio %.1f%% exceeds %.0f%%, keeping original)",
            filepath.name,
            ratio * 100,
            config.ACCEPT_RATIO_MAX * 100,
        )
        return ctx, True
    return ctx, None


def _score_inline(pipeline, job, ctx, dist_path=None):
    """
    对指定产物 inline 打分（默认 temp 区产物；async 打落盘后的文件），
    返回 (vmaf_score, vmaf_elapsed)，并回填 job.vmaf_score。

    Score the given output inline (the temp output by default; async scores
    the landed file). Returns (vmaf_score, vmaf_elapsed) and fills back
    job.vmaf_score.
    """
    compute_vmaf = pipeline.compute_vmaf
    filepath = ctx["filepath"]
    output_path = ctx["output_path"]
    dist_path = dist_path if dist_path is not None else output_path
    stem = filepath.stem
    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80]
    vmaf_log = Path(config.VMAF_LOG_DIR) / f"{safe_stem}.{_time.strftime('%Y%m%d-%H%M%S')}.json"
    vmaf_start = _time.monotonic()
    # 源帧数走 nb_frames 容器索引快路径（mp4 秒级，mkv 回退全量计数），
    # 产物帧数已由转码 stderr 解析，两路都不再需要打分侧全量探测
    ref_frame_count = get_frame_count(filepath, pipeline.ffprobe_path)
    vmaf_score = compute_vmaf(
        filepath,
        dist_path,
        log_path=vmaf_log,
        ref_frame_count=ref_frame_count,
        dist_frame_count=job.dist_frame_count,
    )
    vmaf_elapsed = _time.monotonic() - vmaf_start
    job.vmaf_score = vmaf_score  # 回填 Job，打分结果随任务对象可查
    return vmaf_score, vmaf_elapsed


def _settle(pipeline, job, ctx, verdict, vmaf_score, vmaf_elapsed, landed=None):
    """
    按 verdict 落盘/清理并记终态，返回 bool。landed 非空表示已提前落盘
    （async）：accept 跳过复拷，reject 直接处置库中文件。

    Settle by the verdict (land/clean up and record the terminal state).
    Returns bool. A non-None landed path means the file was landed up front
    (async): accept skips the second copy, reject handles the library file.
    """
    history = pipeline.history
    keep_source = pipeline.keep_source
    filepath = ctx["filepath"]
    output_path = ctx["output_path"]
    temp_dir = ctx["temp_dir"]
    output_size = ctx["output_size"]
    ratio = ctx["ratio"]
    expect_filename = ctx["expect_filename"]
    elapsed = job.elapsed

    if verdict == "accept":
        if landed is None:
            # 先落盘后记 history
            # Land the file before recording history.
            final_path, move_err = _land_output(
                output_path, filepath, expect_filename, job.lib_input, job.lib_output
            )
            if move_err is not None:
                _cleanup_temp(temp_dir)
                history.mark_failed(str(filepath), move_err)
                return False
            if output_path.exists():
                output_path.unlink()
            _cleanup_temp(temp_dir)
        else:
            # 已提前落盘（async）：temp 已清，直接记终态
            # Already landed (async): temp is gone, record directly.
            final_path = landed
        if not keep_source:
            filepath.unlink()
        history.mark_done(str(filepath), str(final_path), vmaf_score=vmaf_score, rejected=False)
        if vmaf_elapsed is not None:
            log.info(
                "ACCEPT: %s (%s, %s, %.1f%%, VMAF %.2f, codec %.1fs + vmaf %.1fs)",
                final_path.name,
                _format_size(output_size),
                "smaller" if ratio < 1 else "larger",
                ratio * 100,
                vmaf_score,
                elapsed,
                vmaf_elapsed,
            )
        else:
            log.info(
                "ACCEPT: %s (%s, %s, %.1f%%, %.1fs)",
                final_path.name,
                _format_size(output_size),
                "smaller" if ratio < 1 else "larger",
                ratio * 100,
                elapsed,
            )
    else:
        # KEEP_TARGET=True 时落盘到目标库但仍记 rejected（源保留，供人工复核）；
        # 否则不落盘、只清 temp。
        # With KEEP_TARGET land into the target library but still record rejected
        # (source kept, for manual review); otherwise no landing, temp cleanup only.
        if landed is not None:
            # 已在库中（async 早落盘）：KEEP_TARGET 留之，否则删之；源始终保留
            # Already in the library (async landed up front): keep it with
            # KEEP_TARGET, otherwise remove it; the source is always kept.
            if config.KEEP_TARGET:
                landed_path = str(landed)
                log.warning(
                    "REJECT (kept): %s landed at %s for manual review",
                    filepath.name,
                    landed_path,
                )
            else:
                if landed.exists():
                    landed.unlink()
                landed_path = None
        else:
            landed_path = None
            if config.KEEP_TARGET:
                final_path, move_err = _land_output(
                    output_path, filepath, expect_filename, job.lib_input, job.lib_output
                )
                if move_err is not None:
                    _cleanup_temp(temp_dir)
                    history.mark_failed(str(filepath), move_err)
                    return False
                landed_path = str(final_path)
                log.warning(
                    "REJECT (kept): %s landed at %s for manual review",
                    filepath.name,
                    landed_path,
                )
            if output_path.exists():
                output_path.unlink()
            _cleanup_temp(temp_dir)
        history.mark_done(str(filepath), landed_path, vmaf_score=vmaf_score, rejected=True)
        if vmaf_elapsed is not None:
            log.warning(
                "REJECT: %s (VMAF %s, ratio %.1f%%, codec %.1fs + vmaf %.1fs, keeping original)",  # noqa: E501  # 日志格式串含多个 %s，换行破坏占位符对齐
                filepath.name,
                f"{vmaf_score:.2f}" if vmaf_score is not None else "N/A",
                ratio * 100,
                elapsed,
                vmaf_elapsed,
            )
        else:
            log.warning(
                "REJECT: %s (VMAF %s, ratio %.1f%%, keeping original, %.1fs)",
                filepath.name,
                f"{vmaf_score:.2f}" if vmaf_score is not None else "N/A",
                ratio * 100,
                elapsed,
            )
    return True


def _post_process_ratio_only(pipeline, job):
    """
    off 模式：只做 ratio 门，无打分，前置检查后直接 accept 落盘。
    返回 (ok, temp_dir) 供 dispatcher 的 finally 释放活跃集。

    Off mode: ratio gate only, no scoring; accept and land after the preamble.
    Returns (ok, temp_dir) for the dispatcher's finally clause.
    """
    ctx, terminal = _resolve_output(pipeline, job)
    temp_dir = ctx["temp_dir"] if ctx is not None else None
    if terminal is not None:
        return terminal, temp_dir
    return _settle(pipeline, job, ctx, "accept", None, None), temp_dir


def _post_process_sync(pipeline, job):
    """
    sync 模式：inline 打分（阻塞转码线程）。
    Sync mode: inline scoring (blocks the transcode thread).
    """
    ctx, terminal = _resolve_output(pipeline, job)
    temp_dir = ctx["temp_dir"] if ctx is not None else None
    if terminal is not None:
        return terminal, temp_dir
    vmaf_score, vmaf_elapsed = _score_inline(pipeline, job, ctx)
    verdict = vmaf_verdict(vmaf_score, ctx["ratio"])
    return _settle(pipeline, job, ctx, verdict, vmaf_score, vmaf_elapsed), temp_dir


def _post_process_async(pipeline, job):
    """
    async 模式：先落盘、后打分。打分对象是目标库里的文件（所见即所得），
    inline 打分（旧后台 worker 已退役）。
    返回 (ok, temp_dir) 供 dispatcher 的 finally 释放活跃集。

    Async mode: land first, then score. The scoring target is the landed file
    in the target library (what you see is what you score); scoring is inline
    (the background worker is retired).
    Returns (ok, temp_dir) for the dispatcher's finally clause.
    """
    history = pipeline.history
    ctx, terminal = _resolve_output(pipeline, job)
    temp_dir = ctx["temp_dir"] if ctx is not None else None
    if terminal is not None:
        return terminal, temp_dir
    # 先落盘：后续打分与处置都针对库中文件（早落盘的孤儿窗口与扫描器可见性
    # 为 land-first 固有代价，见评审）。temp 随即清理，打分不再依赖它。
    # Land first: scoring and settling both target the library file (the
    # orphan window and scanner visibility are inherent to land-first).
    # Temp is cleaned right away; scoring no longer depends on it.
    final_path, move_err = _land_output(
        ctx["output_path"],
        ctx["filepath"],
        ctx["expect_filename"],
        job.lib_input,
        job.lib_output,
    )
    if move_err is not None:
        _cleanup_temp(temp_dir)
        history.mark_failed(str(ctx["filepath"]), move_err)
        return False, temp_dir
    if ctx["output_path"].exists():
        ctx["output_path"].unlink()
    _cleanup_temp(temp_dir)
    vmaf_score, vmaf_elapsed = _score_inline(pipeline, job, ctx, dist_path=final_path)
    verdict = vmaf_verdict(vmaf_score, ctx["ratio"])
    return (
        _settle(pipeline, job, ctx, verdict, vmaf_score, vmaf_elapsed, landed=final_path),
        temp_dir,
    )


def post_process_stage(pipeline: "TranscodePipeline", job: "TranscodeJob") -> bool:
    """
    后处理 dispatcher：按 pipeline.vmaf_mode 路由到三模式函数，
    并持有唯一的 try/except/finally 护栏（关停豁免/失败兜底/活跃集释放）。

    路由：off → _post_process_ratio_only；sync → _post_process_sync；
    async → _post_process_async；未知值警告后走 async。

    Post-process dispatcher: routes by pipeline.vmaf_mode and owns the
    single try/except/finally guard (shutdown exemption, failure fallback,
    active-set release).

    :param pipeline: 进程级共享上下文，提供 history/ffprobe 路径/
                     keep_source/compute_vmaf/vmaf_mode
                     (Process-wide shared context: history, ffprobe path,
                     keep_source flag, scoring callback and VMAF mode)
    :param job: 单文件任务：filepath/lib_* 定位文件，dist_frame_count/
                elapsed 为转码段回填的阶段产物
                (Per-file job: paths plus the transcode stage's
                dist_frame_count/elapsed)
    :return: True 表示处理流程完成（含拒绝）；False 表示失败已记 failed
             (True if processing completed, including rejections; False if failed)
    :rtype: bool
    """
    temp_dir = None
    try:
        mode = pipeline.vmaf_mode or config.VMAF_MODE
        if mode == config.VMAF_MODE_OFF:
            ok, temp_dir = _post_process_ratio_only(pipeline, job)
        elif mode == config.VMAF_MODE_SYNC:
            ok, temp_dir = _post_process_sync(pipeline, job)
        else:
            if mode != config.VMAF_MODE_ASYNC:
                log.warning("未知 vmaf_mode %r，按 async/inline 流程处理", mode)
            ok, temp_dir = _post_process_async(pipeline, job)
        return ok
    except Exception as e:
        history = pipeline.history
        filepath = Path(job.filepath)
        # 关停期被打断：非真实质量问题，不标 failed；记录保持 pending_transcode，
        # 留待下次启动重入队转码。
        # Interrupted by shutdown: not a real failure; the record stays
        # pending_transcode and will be requeued on next startup.
        if is_shutting_down():
            log.warning("POSTPROCESS INTERRUPTED (shutting down): %s kept pending", filepath)
            return False
        # 非关停异常：清理已知临时目录后标记 failed（temp_dir 为 None 说明前置
        # 检查都没过，无可清理；不查库，避免 DB 故障时兜底自身再抛）。
        # Non-shutdown failure: clean the known temp dir, then mark failed
        # (nothing to clean when temp_dir is None; no DB round-trip, so a
        # broken DB cannot break the fallback itself).
        if temp_dir is not None:
            try:
                _cleanup_temp(temp_dir)
            except Exception:
                log.exception("cleanup failed for %s", filepath)
        try:
            history.mark_failed(str(filepath), str(e))
        except Exception:
            log.exception("mark_failed failed for %s", filepath)
        return False
    finally:
        if temp_dir is not None:
            with _active_temp_dirs_lock:
                _active_temp_dirs.discard(str(temp_dir))

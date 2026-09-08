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


def post_process_stage(pipeline: "TranscodePipeline", job: "TranscodeJob") -> bool:
    """
    对转码成功的产物执行后处理（质量判定 + 落盘），与 transcode 构成两段式流水线：
      0. 从 history 取回产物路径并校验存在性，计算压缩比 ratio
      1. ratio 超标直接拒绝（不打分、不落盘，三种模式一致），保留源文件
      2. 非 off 模式调用 compute_vmaf 对 temp 区产物 inline 打分，
         按 verdict 决定接受/拒绝（sync/async 同序：先打分、后落盘）
      3. 接受：落盘→清 temp→删源（除非 keep_source），记 mark_done；
         拒绝：记 mark_done(rejected)，保留源文件；
         KEEP_TARGET=True 时拒绝也落盘到目标库（供人工复核），
         否则拒绝不落盘、只清 temp
      4. 任何未预期异常统一 mark_failed 兜底

    与 transcode 的分工：transcode 只负责编码，产物留在 cache 临时目录；
    本函数负责此后的一切——质量门、VMAF 打分、移动落盘与最终状态记录。

    Post-processes a successfully transcoded output (quality gate + final placement),
    forming the second stage of the pipeline started by transcode:
      0. Fetch and verify the output path from history, compute compression ratio
      1. Reject outright on over-ratio (no scoring, no landing, all modes)
      2. Non-off modes score the temp output inline via compute_vmaf and decide
         by the verdict (sync/async share the order: score first, land later)
      3. Accept: land the file, clean temp, delete source unless keep_source,
         record mark_done; reject: record mark_done(rejected), keep the source;
         with KEEP_TARGET the rejected file is still landed into the target
         library (for manual review), otherwise no landing, only temp cleanup
      4. Any unexpected exception is caught and recorded as failed

    Division of labor: transcode only encodes and leaves the output in the cache
    temp dir; this function owns everything after — quality gate, VMAF scoring,
    moving to destination, and final history state.

    :param pipeline: 进程级共享上下文，提供 history/ffprobe 路径/
                     keep_source/compute_vmaf
                     (Process-wide shared context: history, ffprobe path,
                     keep_source flag and scoring callback)
    :param job: 单文件任务：filepath/lib_* 定位文件，dist_frame_count/
                elapsed 为转码段回填的阶段产物
                (Per-file job: paths plus the transcode stage's
                dist_frame_count/elapsed)
    :return: True 表示处理流程完成（含拒绝）；False 表示失败已记 failed
             (True if processing completed, including rejections; False if failed)
    :rtype: bool
    """
    history = pipeline.history
    ffprobe_path = pipeline.ffprobe_path
    keep_source = pipeline.keep_source
    compute_vmaf = pipeline.compute_vmaf
    vmaf_mode = pipeline.vmaf_mode
    filepath = Path(job.filepath)
    dist_frame_count = job.dist_frame_count
    elapsed = job.elapsed
    input_dir = job.lib_input
    output_dir = job.lib_output
    temp_dir = None
    try:
        stem = filepath.stem
        expect_filename = f"{stem}.mkv"
        input_size = filepath.stat().st_size
        raw_output = history.get_output_path(str(filepath))
        if raw_output is None or not Path(raw_output).exists():
            history.mark_failed(str(filepath), "output path missing after transcode")
            return False
        output_path = Path(raw_output)
        temp_dir = output_path.parent
        output_size = output_path.stat().st_size
        ratio = output_size / input_size if input_size else 0

        # ratio 超标直接拒绝：不打分（三种模式一致，off 下同样拒绝）。
        # KEEP_TARGET=True 时仍落盘到目标库，仅记 rejected。
        # Reject outright on over-ratio: no scoring (all modes, off included).
        # With KEEP_TARGET the file is still landed, only recorded rejected.
        if ratio > config.ACCEPT_RATIO_MAX:
            landed_path = None
            if config.KEEP_TARGET:
                final_path, move_err = _land_output(
                    output_path, filepath, expect_filename, input_dir, output_dir
                )
                if move_err is not None:
                    _cleanup_temp(temp_dir)
                    history.mark_failed(str(filepath), move_err)
                    return False
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
            return True

        vmaf_score = None
        vmaf_elapsed = None
        if vmaf_mode != "off":
            safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80]
            vmaf_log = (
                Path(config.VMAF_LOG_DIR) / f"{safe_stem}.{_time.strftime('%Y%m%d-%H%M%S')}.json"
            )
            vmaf_start = _time.monotonic()
            # 源帧数走 nb_frames 容器索引快路径（mp4 秒级，mkv 回退全量计数），
            # 产物帧数已由转码 stderr 解析，两路都不再需要打分侧全量探测
            ref_frame_count = get_frame_count(filepath, ffprobe_path)
            vmaf_score = compute_vmaf(
                filepath,
                output_path,
                log_path=vmaf_log,
                ref_frame_count=ref_frame_count,
                dist_frame_count=dist_frame_count,
            )
            vmaf_elapsed = _time.monotonic() - vmaf_start
            job.vmaf_score = vmaf_score  # 回填 Job，打分结果随任务对象可查

        if vmaf_mode != "off":
            verdict = vmaf_verdict(vmaf_score, ratio)
        else:
            verdict = "accept"

        if verdict == "accept":
            # 先落盘后记 history
            # Land the file before recording history.
            final_path, move_err = _land_output(
                output_path, filepath, expect_filename, input_dir, output_dir
            )
            if move_err is not None:
                _cleanup_temp(temp_dir)
                history.mark_failed(str(filepath), move_err)
                return False
            if output_path.exists():
                output_path.unlink()
            _cleanup_temp(temp_dir)
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
            landed_path = None
            if config.KEEP_TARGET:
                final_path, move_err = _land_output(
                    output_path, filepath, expect_filename, input_dir, output_dir
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
    except Exception as e:
        # 关停期被打断：非真实质量问题，不标 failed，产物留给启动时 recover。
        # Interrupted by shutdown: not a real failure; leave the record for
        # startup recovery instead of marking failed.
        if is_shutting_down():
            log.warning("POSTPROCESS INTERRUPTED (shutting down): %s kept pending", filepath)
            return False
        # 非关停异常：清理临时目录后标记 failed
        out = history.get_output_path(str(filepath))
        if out:
            try:
                _cleanup_temp(Path(out).parent)
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

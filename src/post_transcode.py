import re
import shutil
import time as _time
from pathlib import Path

from src import config
from src.path_utils import _final_dest_path, _format_size
from src.probe import get_frame_count
from src.quality import vmaf_verdict
from src.temp_cleanup import (
    _cleanup_temp,
)
from src.vmaf_async import (
    is_shutting_down,
    vmaf_tasks,
)

log = __import__("logging").getLogger(__name__)


def post_transcode(
    transcode_info: dict,
    filepath,
    history,
    ffmpeg_path,
    ffprobe_path,
    input_dir=None,
    output_dir=None,
    cache_dir=None,
    keep_source=False,
    compute_vmaf=None,
):
    """
    对转码成功的产物执行后处理（质量判定 + 落盘），与 transcode 构成两段式流水线：
      0. 从 history 取回产物路径并校验存在性，计算压缩比 ratio
      1. ratio 超标直接拒绝（必拒，省去昂贵的采样打分），保留源文件
      2. 异步模式：产物复制到最终目录但不删源，投入 vmaf_tasks 由后台打分
         同步模式：调用 compute_vmaf 打分，按 verdict 决定接受/拒绝/重试
         - 关停期间打分被中断（score=None 且 is_shutting_down）不误标 failed，
           转为 await_postprocess 待下次启动 recover 重打分
      3. 接受：删除源文件（除非 keep_source）；拒绝：保留源文件；均记 mark_done
      4. 任何未预期异常统一 mark_failed 兜底

    与 transcode 的分工：transcode 只负责编码，产物留在 cache 临时目录；
    本函数负责此后的一切——质量门、VMAF 打分、移动落盘与最终状态记录。

    Post-processes a successfully transcoded output (quality gate + final placement),
    forming the second stage of the pipeline started by transcode:
      0. Fetch and verify the output path from history, compute compression ratio
      1. Reject outright if ratio exceeds ACCEPT_RATIO_MAX (skip expensive scoring)
      2. Async mode: copy output into place, queue for background VMAF scoring.
         Sync mode: score via compute_vmaf; on shutdown-interrupted scoring
         (score=None and is_shutting_down) defer as await_postprocess instead of
         marking failed
      3. Accept: delete source unless keep_source; reject: keep source;
         both recorded via mark_done
      4. Any unexpected exception is caught and recorded as failed

    Division of labor: transcode only encodes and leaves the output in the cache
    temp dir; this function owns everything after — quality gate, VMAF scoring,
    moving to destination, and final history state.

    :param transcode_info: transcode 返回的元数据字典，必须包含两个键：
                           "dist_frame_count"（从 ffmpeg stderr 进度行解析的
                           产物帧数，int 或 None）、"elapsed"（转码耗时秒，
                           monotonic 差值，仅用于日志）
                           (Metadata dict from transcode; required keys:
                           "dist_frame_count" (int|None, parsed from ffmpeg's
                           final progress line) and "elapsed" (seconds, for logs))
    :type transcode_info: dict
    :param filepath: 源文件路径 (Path to the source video file)
    :param history: 历史记录对象，经 get_output_path 取产物路径，
                    最终写入 done/failed/await_postprocess
                    (History tracker; reads output path, writes final state)
    :param ffmpeg_path: FFmpeg 可执行文件路径，透传给 VMAF 任务队列
                        (FFmpeg binary path, forwarded to the VMAF task queue)
    :param ffprobe_path: ffprobe 可执行文件路径，用于源帧数探测与 VMAF 任务
                         (ffprobe binary path, used for frame probing and VMAF tasks)
    :param input_dir: 源库根目录，用于推导产物的相对子目录结构
                      (Source library root, used to mirror subdirectories)
    :param output_dir: 输出根目录，产物最终落盘位置 (Output root directory)
    :param cache_dir: 未使用，仅为与 transcode 签名对齐而保留
                      (Unused; kept for signature symmetry with transcode)
    :param keep_source: True=接受后仍保留源文件 (True=keep source after accept)
    :param compute_vmaf: 打分回调 callable(filepath, output_path, log_path=...,
                         ref_frame_count=..., dist_frame_count=...) -> score|None，
                         仅同步打分模式使用
                         (Scoring callback for sync mode)
    :return: True 表示处理流程完成（含拒绝）；False 表示失败已记 failed
             (True if processing completed, including rejections; False if failed)
    :rtype: bool
    """
    try:
        dist_frame_count = transcode_info["dist_frame_count"]
        elapsed = transcode_info["elapsed"]
        filepath = Path(filepath)
        stem = filepath.stem
        output_filename = f"{stem}.mkv"
        input_size = filepath.stat().st_size
        output_path = Path(history.get_output_path(str(filepath)))
        if output_path is None or not output_path.exists():
            history.mark_failed(str(filepath), "output path missing after transcode")
            return False
        output_size = output_path.stat().st_size
        ratio = output_size / input_size if input_size else 0
        temp_dir = output_path.parent
        # 异步 VMAF：文件先移入输出区但不删源，后台 worker 打分后决定去留。
        # 与内联打分完全互斥（内联 VMAF 耗时数分钟到数小时，阻塞转码线程）。
        # Async VMAF: move the output into place but keep the source; a background
        # worker scores it later and keeps exactly one of the two files.
        if config.VMAF_ASYNC:
            if ratio > config.ACCEPT_RATIO_MAX:
                # ratio 超标必拒（VMAF ≥ 93 也要求 ratio ≤ ACCEPT_RATIO_MAX），
                # 直接拒绝不入队，省去昂贵的采样打分。
                _cleanup_temp(temp_dir)
                history.mark_done(str(filepath), None, vmaf_score=None, rejected=True)
                log.warning(
                    "REJECT (async): %s (ratio %.1f%% exceeds %.0f%%, keeping original)",
                    filepath.name,
                    ratio * 100,
                    config.ACCEPT_RATIO_MAX * 100,
                )
                return True
            final_path = _final_dest_path(filepath, output_filename, input_dir, output_dir)
            if final_path.name != output_filename:
                log.info("DEST EXISTS: using %s instead of %s", final_path.name, output_filename)
            try:
                shutil.copyfile(str(output_path), str(final_path))
                output_path.unlink()
                _cleanup_temp(temp_dir)
            except OSError as e:
                log.error(
                    "FILE MOVE FAILED (async): %s — %s",
                    final_path.name,
                    e,
                )
                _cleanup_temp(temp_dir)
                history.mark_failed(str(filepath), f"File move failed: {e}")
                return False
            history.mark_await_postprocess(str(filepath), str(final_path), dist_frame_count)
            safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80]
            vmaf_tasks.put(
                (
                    str(filepath),
                    str(final_path),
                    ratio,
                    safe_stem,
                    history,
                    elapsed,
                    keep_source,
                    dist_frame_count,
                    ffprobe_path,
                )
            )
            log.info(
                "VMAF PENDING (async): %s (%s, ratio %.1f%%, codec %.1fs, queued for scoring)",
                final_path.name,
                _format_size(output_size),
                ratio * 100,
                elapsed,
            )
            return True

        # ratio 超标则直接跳过 VMAF（必拒，省去几十分钟到2小时的采样打分），
        # 与 async 路径一致：不入队、不做任何质量判定。
        if ratio > config.ACCEPT_RATIO_MAX:
            _cleanup_temp(temp_dir)
            history.mark_done(str(filepath), None, vmaf_score=None, rejected=True)
            log.warning(
                "REJECT: %s (ratio %.1f%% exceeds %.0f%%, keeping original)",
                filepath.name,
                ratio * 100,
                config.ACCEPT_RATIO_MAX * 100,
            )
            return True

        vmaf_score = None
        vmaf_elapsed = None
        if config.VMAF_REQUIRED:
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

        # off 模式（--vmaf off）：无质量门，仅按大小比接受（此处 ratio 已 ≤ MAX）。
        # sync 模式：打分失败(None)或极端低分(< VMAF_EXTREME_LOW) 与 async 一致，
        # 记 failed 永久排除，仅 --retry-failed 启动时手动重试。
        if config.VMAF_REQUIRED:
            verdict = vmaf_verdict(vmaf_score, ratio)
        else:
            verdict = "accept"

        if verdict == "retry":
            if vmaf_score is None and is_shutting_down():
                # 关停中被中断/失败的打分：非真实质量问题，转 await_postprocess
                # 保留产物供下次启动 recover 重新打分，不误标 failed。
                # Scoring interrupted by shutdown: keep the output as pending and
                # rescore it on next startup instead of marking it failed.
                final_path = _final_dest_path(filepath, output_filename, input_dir, output_dir)
                if final_path.name != output_filename:
                    log.info(
                        "DEST EXISTS: using %s instead of %s", final_path.name, output_filename
                    )
                try:
                    shutil.copyfile(str(output_path), str(final_path))
                    output_path.unlink()
                    _cleanup_temp(temp_dir)
                except OSError as e:
                    log.error("FILE MOVE FAILED (async): %s — %s", final_path.name, e)
                    _cleanup_temp(temp_dir)
                    history.mark_failed(str(filepath), f"File move failed: {e}")
                    return False
                history.mark_await_postprocess(str(filepath), str(final_path), dist_frame_count)
                safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80]
                vmaf_tasks.put(
                    (
                        str(filepath),
                        str(final_path),
                        ratio,
                        safe_stem,
                        history,
                        elapsed,
                        keep_source,
                        dist_frame_count,
                        ffprobe_path,
                    )
                )
                log.info(
                    "VMAF DEFERRED (sync, shutting down): %s kept pending for rescore",
                    final_path.name,
                )
                return True
            _cleanup_temp(temp_dir)
            if vmaf_score is None:
                history.mark_failed(str(filepath), "VMAF scoring failed")
                log.warning(
                    "VMAF SCORING FAILED (sync): %s (ratio %.1f%%, marking failed)",
                    filepath.name,
                    ratio * 100,
                )
            else:
                history.mark_failed(
                    str(filepath),
                    f"VMAF {vmaf_score:.2f} below {config.VMAF_EXTREME_LOW}, input may be incomplete",  # noqa: E501
                )
                log.warning(
                    "VMAF EXTREME LOW (sync): %s (VMAF %.2f, ratio %.1f%%, marking failed)",
                    filepath.name,
                    vmaf_score,
                    ratio * 100,
                )
            return False

        if verdict == "accept":
            final_path = _final_dest_path(filepath, output_filename, input_dir, output_dir)
            if final_path.name != output_filename:
                log.info("DEST EXISTS: using %s instead of %s", final_path.name, output_filename)
            # 先移动文件，成功后再记录 history；失败不影响 VMAF 日志
            # Copy output before logging acceptance — file I/O may fail on NFS
            try:
                # 跨文件系统时 os.rename 不可用，且 copy2/chmod 在 NFS 上 EPERM
                # os.rename fails across filesystems; copy2/copymode raises EPERM on NFS
                shutil.copyfile(str(output_path), str(final_path))
                output_path.unlink()
                _cleanup_temp(temp_dir)
                if not keep_source:
                    filepath.unlink()
            except OSError as e:
                log.error(
                    "FILE MOVE FAILED: %s (VMAF %.2f, ratio %.1f%%) — %s",
                    final_path.name,
                    vmaf_score or 0,
                    ratio * 100,
                    e,
                )
                _cleanup_temp(temp_dir)
                history.mark_failed(str(filepath), f"File move failed: {e}")
                return False
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
            _cleanup_temp(temp_dir)
            history.mark_done(str(filepath), None, vmaf_score=vmaf_score, rejected=True)
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

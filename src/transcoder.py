import re
import shutil
import subprocess
import time as _time
from pathlib import Path

from src import config
from src.ffmpeg_runner import (
    _build_ffmpeg_cmd,
    _parse_progress_fps,
    _parse_progress_time,
    _run_ffmpeg_with_progress,
)
from src.path_utils import _format_size, _random_suffix, _unique_path
from src.probe import (
    get_expected_frame_count,
    get_streams_info,
    get_video_frame_count,
    get_video_frame_rate,
)
from src.process_guard import (
    _active_ffmpeg_lock,
    _active_ffmpeg_pids,
    _active_transcodes,
    _active_transcodes_lock,
    kill_active_ffmpeg,
    mark_stale_started_as_failed,
    register_ffmpeg_pid,
    unregister_ffmpeg_pid,
)
from src.quality import compute_vmaf, should_accept
from src.temp_cleanup import (
    _active_temp_dirs,
    _active_temp_dirs_lock,
    _cleanup_temp,
    _rmtree_robust,
    active_temp_dirs_snapshot,
    cleanup_stale_temp_dirs,
)
from src.vmaf_async import (
    _VMAF_STOP,
    _handle_vmaf_task,
    _vmaf_worker,
    _vmaf_worker_threads,
    _vmaf_workers_lock,
    _vmaf_workers_started,
    drain_vmaf_tasks,
    recover_pending_vmaf,
    start_vmaf_workers,
    vmaf_tasks,
)

log = __import__("logging").getLogger(__name__)

# 模块拆分后此文件仅保留 transcode() 主流程，其余职责迁移至：
#   path_utils.py    —— _unique_path / _format_size / _random_suffix
#   ffmpeg_runner.py —— _build_ffmpeg_cmd / 进度解析 / _run_ffmpeg_with_progress
#   process_guard.py —— ffmpeg PID 与进行中转码登记 / 孤儿进程清理
#   temp_cleanup.py  —— 临时目录清理与 stale 清理
#   vmaf_async.py    —— 异步 VMAF 任务队列与 worker
# 下方 re-export 保持 src.transcoder.* 兼容，供 main.py 与既有测试引用。
# After the module split this file only keeps the transcode() workflow; the
# responsibilities above moved to the listed modules. The re-exports below
# keep src.transcoder.* compatible for main.py and existing tests.

__all__ = [
    "active_temp_dirs_snapshot",
    "cleanup_stale_temp_dirs",
    "drain_vmaf_tasks",
    "kill_active_ffmpeg",
    "mark_stale_started_as_failed",
    "recover_pending_vmaf",
    "start_vmaf_workers",
    "transcode",
    "_VMAF_STOP",
    "_active_ffmpeg_lock",
    "_active_ffmpeg_pids",
    "_active_temp_dirs",
    "_active_temp_dirs_lock",
    "_active_transcodes",
    "_active_transcodes_lock",
    "_build_ffmpeg_cmd",
    "_cleanup_temp",
    "_final_dest_path",
    "_format_size",
    "_handle_vmaf_task",
    "_parse_progress_fps",
    "_parse_progress_time",
    "_random_suffix",
    "_rmtree_robust",
    "_run_ffmpeg_with_progress",
    "_unique_path",
    "_vmaf_worker",
    "_vmaf_worker_threads",
    "_vmaf_workers_lock",
    "_vmaf_workers_started",
    "register_ffmpeg_pid",
    "unregister_ffmpeg_pid",
    "vmaf_tasks",
]


def _final_dest_path(filepath, output_filename, input_dir, output_dir):
    """计算输出区目标路径（保持相对目录结构，重名自动编号）。"""
    if output_dir:
        output_dir = Path(output_dir)
        if input_dir:
            input_dir = Path(input_dir)
            try:
                rel = filepath.parent.relative_to(input_dir)
            except ValueError:
                rel = Path()
            dest_dir = output_dir / rel
        else:
            dest_dir = output_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        final_path = _unique_path(dest_dir / output_filename)
    else:
        final_path = _unique_path(filepath.parent / output_filename)
    return final_path


def transcode(
    filepath, history, ffmpeg_path, ffprobe_path, input_dir=None, output_dir=None, cache_dir=None
):
    """
    对单个视频文件执行转码的完整流程：
      1. 创建临时工作目录
      2. 构建 FFmpeg 命令并执行（含实时进度日志）
      3. 成功时用输出文件替换原文件
      4. 失败/异常时清理临时目录并标记历史

    Complete transcoding workflow for a single video file:
      1. Create a temporary working directory
      2. Build and run the FFmpeg command (with real-time progress logging)
      3. On success, replace the original file with the output
      4. On failure/exception, clean up the temp directory and mark history

    :param filepath: 源文件路径 (Path to the source video file)
    :param history: 历史记录对象 (History tracking object)
    :param ffmpeg_path: FFmpeg 可执行文件路径 (Path to the FFmpeg executable)
    :param ffprobe_path: ffprobe 可执行文件路径 (Path to the ffprobe executable)
    :param output_dir: 输出目录，不设置则同源目录 (Output directory, defaults to source parent)
    :param cache_dir: 缓存（临时）目录，不设置则同源目录
                      (Cache/temp directory, defaults to source parent)
    :return: True 表示成功 (True on success), False 表示失败 (False on failure)
    """
    filepath = Path(filepath)
    stem = filepath.stem
    suffix = _random_suffix()
    if cache_dir:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = cache_dir / f"{config.TEMP_PREFIX}{suffix}"
    else:
        temp_dir = filepath.parent / f"{config.TEMP_PREFIX}{suffix}"
    start_time = _time.monotonic()
    input_size = filepath.stat().st_size

    output_filename = f"{stem}.mkv"
    output_path = temp_dir / output_filename

    with _active_temp_dirs_lock:
        _active_temp_dirs.add(str(temp_dir))
    history.mark_started(str(filepath))
    with _active_transcodes_lock:
        _active_transcodes.add(str(filepath))

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "TRANSCODE: %s (%s) -> %s (temp: %s)",
            filepath.name,
            _format_size(input_size),
            output_filename,
            temp_dir.name,
        )

        cmd, total_duration = _build_ffmpeg_cmd(
            ffmpeg_path, ffprobe_path, filepath, output_path, sw_fallback=False
        )
        log.info("FFMPEG CMD: %s %s", filepath.name, " ".join(str(c) for c in cmd))

        returncode, stderr = _run_ffmpeg_with_progress(
            cmd, total_duration, filepath.name, timeout=config.CODEC_TIMEOUT
        )

        # 第一次运行（QSV）stderr 存入 .ffmpeg.log.qsv
        try:
            (temp_dir / ".ffmpeg.log.qsv").write_text(stderr, encoding="utf-8")
        except OSError:
            pass

        # 软件缩放重试辅助函数（防双重重试：无论何种触发条件，最多重试一次）
        sw_fallback_attempted = False

        def _retry_sw_fallback():
            nonlocal returncode, stderr, temp_dir, output_path, sw_fallback_attempted
            if sw_fallback_attempted:
                return
            sw_fallback_attempted = True
            log.warning("QSV SW FALLBACK: %s - re-trying with software scale", filepath.name)
            old_temp_dir = output_path.parent
            _cleanup_temp(temp_dir)
            suffix = _random_suffix()
            if cache_dir:
                temp_dir = Path(cache_dir) / f"{config.TEMP_PREFIX}{suffix}"
            else:
                temp_dir = filepath.parent / f"{config.TEMP_PREFIX}{suffix}"
            with _active_temp_dirs_lock:
                _active_temp_dirs.discard(str(old_temp_dir))
                _active_temp_dirs.add(str(temp_dir))
            temp_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_dir / output_filename
            cmd, _ = _build_ffmpeg_cmd(
                ffmpeg_path, ffprobe_path, filepath, output_path, sw_fallback=True
            )
            log.info(
                "FFMPEG CMD (sw fallback): %s %s", filepath.name, " ".join(str(c) for c in cmd)
            )
            returncode, stderr = _run_ffmpeg_with_progress(
                cmd, total_duration, filepath.name, timeout=config.CODEC_TIMEOUT
            )
            try:
                (temp_dir / ".ffmpeg.log").write_text(stderr, encoding="utf-8")
            except OSError:
                pass

        # QSV filter reinit 失败则立即用软件缩放重试（新建临时目录避免冲突）
        if returncode != 0 and (
            "Error reinitializing filters" in stderr or "Error initializing filters" in stderr
        ):
            _retry_sw_fallback()

        # 最终 stderr 写入 .ffmpeg.log
        try:
            (temp_dir / ".ffmpeg.log").write_text(stderr, encoding="utf-8")
        except OSError:
            pass

        if returncode != 0:
            error_msg = stderr.strip() or f"exit code {returncode}"
            elapsed = _time.monotonic() - start_time
            log.error("TRANSCODE FAILED: %s (%.1fs) - %s", filepath.name, elapsed, error_msg)
            _cleanup_temp(temp_dir)
            history.mark_failed(str(filepath), error_msg)
            return False

        if not output_path.exists():
            elapsed = _time.monotonic() - start_time
            log.error("TRANSCODE FAILED: output not found for %s (%.1fs)", filepath.name, elapsed)
            _cleanup_temp(temp_dir)
            history.mark_failed(str(filepath), "Output file not found after transcode")
            return False

        # 帧数验证（Option A）：vpp_qsv 偶发只编码十几帧就 rc=0 退出，
        # 输出文件存在且 returncode 为 0，但视频帧数严重不足。此时用软件缩放重试一次。
        # 先做廉价的时长探测，可疑时才做精确帧数统计（避免对健康文件全量解码计数）。
        # Frame-count validation: vpp_qsv can exit rc=0 after encoding only a handful
        # of frames, leaving a present-but-truncated output. We detect it here and
        # retry once with software scaling. A cheap duration probe gates the exact
        # (slow, full-decode) frame count so healthy files aren't penalized.
        if not sw_fallback_attempted:
            expected_frames = get_expected_frame_count(
                total_duration, get_video_frame_rate(filepath, ffprobe_path)
            )
            if expected_frames:
                out_info = get_streams_info(output_path, ffprobe_path)
                out_duration = out_info["duration"] if out_info else None
                # 时长异常（远短于源）才进入精确帧数统计；duration 探测失败时保守按异常处理
                duration_suspicious = out_duration is None or (
                    total_duration is not None
                    and out_duration < total_duration * config.FRAME_COUNT_MIN_RATIO
                )
                if duration_suspicious:
                    actual_frames = get_video_frame_count(output_path, ffprobe_path)
                    if (
                        actual_frames is not None
                        and actual_frames < expected_frames * config.FRAME_COUNT_MIN_RATIO
                    ):
                        log.warning(
                            "FRAME COUNT ANOMALY: %s - got %d frames, expected ~%d; "
                            "retrying with software scale",
                            filepath.name,
                            actual_frames,
                            expected_frames,
                        )
                        _retry_sw_fallback()
                        # 重试后重新校验（returncode / 输出存在性 / 帧数）
                        try:
                            (temp_dir / ".ffmpeg.log").write_text(stderr, encoding="utf-8")
                        except OSError:
                            pass
                        if returncode != 0:
                            error_msg = stderr.strip() or f"exit code {returncode}"
                            elapsed = _time.monotonic() - start_time
                            log.error(
                                "TRANSCODE FAILED: %s (%.1fs) - %s",
                                filepath.name,
                                elapsed,
                                error_msg,
                            )
                            _cleanup_temp(temp_dir)
                            history.mark_failed(str(filepath), error_msg)
                            return False
                        if not output_path.exists():
                            elapsed = _time.monotonic() - start_time
                            log.error(
                                "TRANSCODE FAILED: output not found for %s (%.1fs)",
                                filepath.name,
                                elapsed,
                            )
                            _cleanup_temp(temp_dir)
                            history.mark_failed(
                                str(filepath), "Output file not found after transcode"
                            )
                            return False
                        # 软件缩放输出仍需帧数校验：若依旧帧数不足，判定失败（不再次重试）
                        actual_frames_retry = get_video_frame_count(output_path, ffprobe_path)
                        if (
                            actual_frames_retry is not None
                            and actual_frames_retry
                            < expected_frames * config.FRAME_COUNT_MIN_RATIO
                        ):
                            elapsed = _time.monotonic() - start_time
                            log.error(
                                "TRANSCODE FAILED: frame count still low after sw fallback for %s "
                                "(%d frames, expected ~%d, %.1fs)",
                                filepath.name,
                                actual_frames_retry,
                                expected_frames,
                                elapsed,
                            )
                            _cleanup_temp(temp_dir)
                            history.mark_failed(
                                str(filepath),
                                f"Frame count anomaly after sw fallback: "
                                f"{actual_frames_retry} frames, expected ~{expected_frames}",
                            )
                            return False

        output_size = output_path.stat().st_size
        ratio = output_size / input_size if input_size else 0
        elapsed = _time.monotonic() - start_time

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
            history.mark_pending_vmaf(str(filepath), str(final_path))
            safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80]
            vmaf_tasks.put((str(filepath), str(final_path), ratio, safe_stem, history, elapsed))
            log.info(
                "VMAF PENDING (async): %s (%s, ratio %.1f%%, codec %.1fs, queued for scoring)",
                final_path.name,
                _format_size(output_size),
                ratio * 100,
                elapsed,
            )
            return True

        # ratio 超标则直接跳过 VMAF（必拒，省去几十分钟到2小时的采样打分）
        vmaf_score = None
        vmaf_elapsed = None
        if config.VMAF_REQUIRED and ratio <= config.ACCEPT_RATIO_MAX:
            safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80]
            vmaf_log = (
                Path(config.VMAF_LOG_DIR) / f"{safe_stem}.{_time.strftime('%Y%m%d-%H%M%S')}.json"
            )
            vmaf_start = _time.monotonic()
            vmaf_score = compute_vmaf(filepath, output_path, log_path=vmaf_log)
            vmaf_elapsed = _time.monotonic() - vmaf_start

        if should_accept(vmaf_score, ratio):
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

    except subprocess.TimeoutExpired:
        elapsed = _time.monotonic() - start_time
        log.error("TRANSCODE TIMEOUT: %s (%.1fs)", filepath.name, elapsed)
        _cleanup_temp(temp_dir)
        history.mark_failed(str(filepath), "Transcode timed out")
        return False
    except Exception as e:
        elapsed = _time.monotonic() - start_time
        log.error("TRANSCODE ERROR: %s (%.1fs) - %s", filepath.name, elapsed, e)
        _cleanup_temp(temp_dir)
        history.mark_failed(str(filepath), str(e))
        return False
    finally:
        with _active_transcodes_lock:
            _active_transcodes.discard(str(filepath))
        with _active_temp_dirs_lock:
            _active_temp_dirs.discard(str(temp_dir))

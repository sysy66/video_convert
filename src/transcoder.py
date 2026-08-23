"""
转码模块。实现单个视频文件的完整转码流程：构建 FFmpeg 命令、执行并实时
记录进度、成功时替换源文件，失败时清理并标记历史状态。

Transcoding module. Implements the full transcode workflow for a single video:
build the FFmpeg command, run it with live progress logging, replace the source
on success, and clean up / mark history on failure.
"""

import subprocess
import time as _time
from pathlib import Path

from src import config
from src.ffmpeg_runner import (
    _build_ffmpeg_cmd,
    _parse_progress_fps,
    _parse_progress_time,
    _run_ffmpeg_with_progress,
    _summarize_error_stderr,
    parse_frame_count_from_stderr,
)
from src.path_utils import _final_dest_path, _format_size, _random_suffix, _unique_path
from src.probe import get_frame_count, get_video_codec
from src.process_guard import (
    _active_ffmpeg_lock,
    _active_ffmpeg_pids,
    kill_active_ffmpeg,
    register_ffmpeg_pid,
    unregister_ffmpeg_pid,
)
from src.quality import vmaf_verdict
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
    is_shutting_down,
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
    "recover_pending_vmaf",
    "start_vmaf_workers",
    "transcode",
    "_VMAF_STOP",
    "_active_ffmpeg_lock",
    "_active_ffmpeg_pids",
    "_active_temp_dirs",
    "_active_temp_dirs_lock",
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
    "_summarize_error_stderr",
    "_unique_path",
    "_vmaf_worker",
    "_vmaf_worker_threads",
    "_vmaf_workers_lock",
    "_vmaf_workers_started",
    "get_frame_count",
    "get_video_codec",
    "is_shutting_down",
    "register_ffmpeg_pid",
    "unregister_ffmpeg_pid",
    "vmaf_tasks",
    "vmaf_verdict",
]


def transcode(
    filepath,
    history,
    ffmpeg_path,
    ffprobe_path,
    input_dir,
    output_dir,
    cache_dir,
    keep_source=False,
    compute_vmaf=None,
):
    """
    对单个视频执行转码（仅编码阶段）：创建临时工作目录、构建并运行 FFmpeg
    命令（含 QSV 软件缩放/软解码各一次的降级重试），成功时产物保留在
    cache 临时目录中，质量判定与落盘由 post_transcode 接手。
      - 失败/超时/异常：清理临时目录并 mark_failed，返回 (False, None, None)
      - 成功：返回 (True, dist_frame_count, elapsed)，其中帧数取自 ffmpeg
        最终进度行的 frame=N（与 -count_frames 同源），免去打分侧再对 mkv
        全量解码计数；elapsed 为 monotonic 耗时秒数，仅供日志使用

    注意：转码侧刻意不产生任何帧率规整参数——实验证明 -r 会导致 VMAF 虚低、
    -fps_mode cfr 会导致产物帧率翻倍（2026-08-13）；源/产物帧率对齐只在打分侧
    按帧序号进行。

    Transcodes a single video (encoding stage only): creates the temp working
    dir, builds and runs the FFmpeg command (with at most one software-scale
    fallback and one software-decode fallback on QSV failures). On success the
    output stays in the cache temp dir; verdict/scoring/move-to-dest are handled
    by post_transcode.
      - Failure/timeout/exception: temp dir cleaned up, record marked failed,
        returns (False, None, None)
      - Success: returns (True, dist_frame_count, elapsed); the frame count comes
        from ffmpeg's final progress line frame=N (same source as -count_frames),
        sparing a full decode count of the mkv on the scoring side; elapsed is a
        monotonic duration in seconds, for logging only.

    Deliberately no frame-rate options here: experiments showed -r deflates VMAF
    scores and -fps_mode cfr doubles output fps (2026-08-13); alignment happens
    on the scoring side by frame index only.

    :param filepath: 源文件路径 (Path to the source video file)
    :param history: 历史记录对象，转码前写入 pending_transcode 与产物路径
                    (History tracker; writes pending_transcode + output path)
    :param ffmpeg_path: FFmpeg 可执行文件路径 (Path to the FFmpeg binary)
    :param ffprobe_path: ffprobe 可执行文件路径 (Path to the ffprobe binary)
    :param input_dir: 源库根目录 (Source library root directory)
    :param output_dir: 输出根目录，产物最终落盘位置由 post_transcode 决定
                       (Output root; final destination resolved by post_transcode)
    :param cache_dir: 缓存（临时）目录，必填，临时工作目录创建于此
                      (Cache/temp directory, required; temp workdir lives here)
    :param keep_source: 透传给后处理：True=接受后不删源文件
                        (Forwarded to post-processing: True=keep source after accept)
    :param compute_vmaf: 打分回调 callable(filepath, output_path, log_path=...,
                         ref_frame_count=..., dist_frame_count=...) -> score|None，
                         仅由后处理使用，此处透传保存
                         (Scoring callback used by post-processing; passed through)
    :return: (是否成功, 产物帧数或 None, 转码耗时秒)
             (Tuple of (success, output frame count or None, elapsed seconds))
    :rtype: Tuple[bool, Optional[int], Optional[float]]
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    filepath = Path(filepath)
    stem = filepath.stem

    output_filename = f"{stem}.mkv"

    start_time = _time.monotonic()
    input_size = filepath.stat().st_size

    suffix = _random_suffix()
    temp_dir = cache_dir / f"{config.TEMP_PREFIX}{suffix}"
    with _active_temp_dirs_lock:
        _active_temp_dirs.add(str(temp_dir))
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        with _active_temp_dirs_lock:
            _active_temp_dirs.discard(str(temp_dir))
        log.error("Failed to create temp dir: %s", e)
        return (False, None, None)
    output_path = temp_dir / output_filename
    history.mark_pending_transcode(str(filepath), str(output_path))

    try:
        # 转码侧不产生任何帧率规整参数；对齐只在打分侧按帧序号进行。

        # 软件缩放重试辅助函数（防双重重试：无论何种触发条件，最多重试一次）
        sw_scale_fallback_attempted = False

        def _retry_sw_scale():
            nonlocal returncode, stderr, temp_dir, output_path, sw_scale_fallback_attempted  # noqa: E501
            if sw_scale_fallback_attempted:
                return
            sw_scale_fallback_attempted = True
            log.warning("QSV SW SCALE FALLBACK: %s - re-trying with software scale", filepath.name)

            old_temp_dir = output_path.parent
            _cleanup_temp(temp_dir)

            suffix = _random_suffix()
            temp_dir = Path(cache_dir) / f"{config.TEMP_PREFIX}{suffix}"
            with _active_temp_dirs_lock:
                _active_temp_dirs.discard(str(old_temp_dir))
                _active_temp_dirs.add(str(temp_dir))
            temp_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_dir / output_filename
            history.mark_pending_transcode(str(filepath), str(output_path))

            cmd, _ = _build_ffmpeg_cmd(
                ffmpeg_path,
                ffprobe_path,
                filepath,
                output_path,
                sw_scale_fallback=True,
            )
            log.info(
                "FFMPEG CMD (sw scale fallback): %s %s",
                filepath.name,
                " ".join(str(c) for c in cmd),
            )
            returncode, stderr = _run_ffmpeg_with_progress(
                cmd, total_duration, filepath.name, timeout=config.CODEC_TIMEOUT
            )
            try:
                (temp_dir / ".ffmpeg.log").write_text(stderr, encoding="utf-8")
            except OSError:
                pass

        # 软件解码重试辅助函数（防双重重试：无论何种触发条件，最多重试一次）
        sw_decode_fallback_attempted = False

        def _retry_sw_decode():
            nonlocal returncode, stderr, temp_dir, output_path, sw_decode_fallback_attempted  # noqa: E501
            if sw_decode_fallback_attempted:
                return
            sw_decode_fallback_attempted = True
            log.warning("QSV SW DECODE FALLBACK: %s - re-trying with software scale", filepath.name)

            old_temp_dir = output_path.parent
            _cleanup_temp(temp_dir)

            suffix = _random_suffix()
            temp_dir = Path(cache_dir) / f"{config.TEMP_PREFIX}{suffix}"
            with _active_temp_dirs_lock:
                _active_temp_dirs.discard(str(old_temp_dir))
                _active_temp_dirs.add(str(temp_dir))
            temp_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_dir / output_filename
            history.mark_pending_transcode(str(filepath), str(output_path))

            cmd, _ = _build_ffmpeg_cmd(
                ffmpeg_path,
                ffprobe_path,
                filepath,
                output_path,
                sw_decode_fallback=True,
            )
            log.info(
                "FFMPEG CMD (sw decode fallback): %s %s",
                filepath.name,
                " ".join(str(c) for c in cmd),
            )
            returncode, stderr = _run_ffmpeg_with_progress(
                cmd, total_duration, filepath.name, timeout=config.CODEC_TIMEOUT
            )
            try:
                (temp_dir / ".ffmpeg.log").write_text(stderr, encoding="utf-8")
            except OSError:
                pass

        codec = get_video_codec(filepath, ffprobe_path)
        if codec in config.SW_DECODE_FALLBACK_CODECS:
            sw_decode_fallback_attempted = True

        log.info(
            "TRANSCODE: %s (%s) -> %s (temp: %s)",
            filepath.name,
            _format_size(input_size),
            output_filename,
            temp_dir.name,
        )

        cmd, total_duration = _build_ffmpeg_cmd(
            ffmpeg_path,
            ffprobe_path,
            filepath,
            output_path,
            sw_decode_fallback=sw_decode_fallback_attempted,
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

        if returncode != 0:
            if sw_decode_fallback_attempted:
                pass  # 首轮已软解，失败直接落到底部判失败
            elif "Error reinitializing filters" in stderr or "Error initializing filters" in stderr:
                _retry_sw_scale()
                if returncode != 0:
                    _retry_sw_decode()  # 缩放失败 → 级联解码
            else:
                _retry_sw_decode()  # 硬解其他错误 → 直接软解重试

        # 最终 stderr 写入 .ffmpeg.log
        try:
            (temp_dir / ".ffmpeg.log").write_text(stderr, encoding="utf-8")
        except OSError:
            pass

        if returncode != 0:
            error_msg = _summarize_error_stderr(stderr) or f"exit code {returncode}"
            elapsed = _time.monotonic() - start_time
            log.error("TRANSCODE FAILED: %s (%.1fs) - %s", filepath.name, elapsed, error_msg)
            _cleanup_temp(temp_dir)
            history.mark_failed(str(filepath), error_msg)
            return (False, None, None)

        if not output_path.exists():
            elapsed = _time.monotonic() - start_time
            log.error("TRANSCODE FAILED: output not found for %s (%.1fs)", filepath.name, elapsed)
            _cleanup_temp(temp_dir)
            history.mark_failed(str(filepath), "Output file not found after transcode")
            return (False, None, None)

        # 产物帧数直接取自 ffmpeg 最终进度行的 frame=N（与 -count_frames 计数
        # 同源），免去打分前对 mkv 产物再做一次昂贵的全量解码计数。
        dist_frame_count = parse_frame_count_from_stderr(stderr)
        elapsed = _time.monotonic() - start_time
        return True, dist_frame_count, elapsed

    except subprocess.TimeoutExpired:
        elapsed = _time.monotonic() - start_time
        log.error("TRANSCODE TIMEOUT: %s (%.1fs)", filepath.name, elapsed)
        _cleanup_temp(temp_dir)
        history.mark_failed(str(filepath), "Transcode timed out")
        return (False, None, None)
    except Exception as e:
        elapsed = _time.monotonic() - start_time
        log.error("TRANSCODE ERROR: %s (%.1fs) - %s", filepath.name, elapsed, e)
        _cleanup_temp(temp_dir)
        history.mark_failed(str(filepath), str(e))
        return (False, None, None)
    finally:
        with _active_temp_dirs_lock:
            _active_temp_dirs.discard(str(temp_dir))

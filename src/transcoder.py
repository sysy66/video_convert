"""
转码模块。实现单个视频文件的完整转码流程：构建 FFmpeg 命令、执行并实时
记录进度、成功时替换源文件，失败时清理并标记历史状态。

Transcoding module. Implements the full transcode workflow for a single video:
build the FFmpeg command, run it with live progress logging, replace the source
on success, and clean up / mark history on failure.
"""

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
    _summarize_error_stderr,
    parse_frame_count_from_stderr,
)
from src.path_utils import _format_size, _random_suffix, _unique_path
from src.probe import get_frame_count, get_video_codec
from src.process_guard import (
    _active_ffmpeg_lock,
    _active_ffmpeg_pids,
    kill_active_ffmpeg,
    register_ffmpeg_pid,
    unregister_ffmpeg_pid,
)
from src.quality import compute_vmaf, vmaf_verdict
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
    "register_ffmpeg_pid",
    "unregister_ffmpeg_pid",
    "vmaf_tasks",
]


def _final_dest_path(filepath, output_filename, input_dir, output_dir):
    """
    计算输出区目标路径：按需保持 input_dir 下的相对目录结构，目标重名时自动编号。
    Compute the destination path in the output area: mirror the relative structure
    under input_dir when applicable, and auto-number if the name collides.
    """
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
    filepath,
    history,
    ffmpeg_path,
    ffprobe_path,
    input_dir=None,
    output_dir=None,
    cache_dir=None,
    keep_source=False,
):
    """
    对单个视频文件执行转码的完整流程：
      0. 开启 VMAF 时先档位化源帧率：档3/4 混合 VFR 直接 mark_failed，档2 记 CFR 目标
      1. 创建临时工作目录
      2. 构建 FFmpeg 命令并执行（含实时进度日志）
      3. 成功时用输出文件替换原文件
      4. 失败/异常时清理临时目录并标记历史

    Complete transcoding workflow for a single video file:
      0. With VMAF on, classify the source frame rate first: mixed VFR (tiers 3/4)
         is marked failed outright; tier 2 records a CFR target
      1. Create a temporary working directory
      2. Build and run the FFmpeg command (with real-time progress logging)
      3. On success, replace the original file with the output
      4. On failure/exception, clean up the temp directory and mark history

    :param filepath: 源文件路径 (Path to the source video file)
    :param history: 历史记录对象 (History tracking object)
    :param ffmpeg_path: FFmpeg 可执行文件路径 (Path to the FFmpeg executable)
    :param ffprobe_path: ffprobe 可执行文件路径 (Path to the ffprobe executable)
    :param input_dir: 源目录，用于在输出目录中保持相对目录结构
                      (Source directory, used to mirror the relative structure under output_dir)
    :param output_dir: 输出目录，不设置则同源目录 (Output directory, defaults to source parent)
    :param cache_dir: 缓存（临时）目录，不设置则同源目录
                      (Cache/temp directory, defaults to source parent)
    :param keep_source: True=成功后保留源文件，不删除
                        (True=keep the source file after success, do not delete it)
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

    try:
        # 转码侧不产生任何帧率规整参数（实验证明 -r 致 VMAF 虚低、-fps_mode
        # cfr 致产物双倍帧，2026-08-13）；对齐只在打分侧按帧序号进行。
        # Transcoding adds no frame-rate options (experiments showed -r deflates
        # scores and -fps_mode cfr doubles output frames, 2026-08-13);
        # alignment happens on the scoring side by frame index only.

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

        temp_dir.mkdir(parents=True, exist_ok=True)
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
            return False

        if not output_path.exists():
            elapsed = _time.monotonic() - start_time
            log.error("TRANSCODE FAILED: output not found for %s (%.1fs)", filepath.name, elapsed)
            _cleanup_temp(temp_dir)
            history.mark_failed(str(filepath), "Output file not found after transcode")
            return False

        output_size = output_path.stat().st_size
        ratio = output_size / input_size if input_size else 0
        elapsed = _time.monotonic() - start_time
        # 产物帧数直接取自 ffmpeg 最终进度行的 frame=N（与 -count_frames 计数
        # 同源），免去打分前对 mkv 产物再做一次昂贵的全量解码计数。
        dist_frame_count = parse_frame_count_from_stderr(stderr)

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
            history.mark_pending_vmaf(str(filepath), str(final_path), dist_frame_count)
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
                history.mark_pending_vmaf(str(filepath), str(final_path), dist_frame_count)
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
                    f"VMAF {vmaf_score:.2f} below {config.VMAF_EXTREME_LOW}, "
                    f"input may be incomplete",
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
        with _active_temp_dirs_lock:
            _active_temp_dirs.discard(str(temp_dir))

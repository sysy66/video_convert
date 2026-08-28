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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.job import TranscodeJob
    from src.pipeline import TranscodePipeline
from src import config
from src.ffmpeg_runner import (
    _build_ffmpeg_cmd,
    _run_ffmpeg_with_progress,
    _summarize_error_stderr,
    parse_frame_count_from_stderr,
)
from src.path_utils import _format_size, _random_suffix
from src.probe import get_video_codec
from src.temp_cleanup import (
    _active_temp_dirs,
    _active_temp_dirs_lock,
    _cleanup_temp,
)

log = __import__("logging").getLogger(__name__)


# 内部从 job.filepath / job.lib_* / pipeline.history 等取参
# 成功：job.output_path/dist_frame_count/elapsed 写入 job，return True
# 失败：清理并 mark_failed，return False
def transcode_stage(pipeline: "TranscodePipeline", job: "TranscodeJob") -> bool:
    """
    对单个视频执行转码（仅编码阶段）：创建临时工作目录、构建并运行 FFmpeg
    命令（含 QSV 软件缩放/软解码各一次的降级重试），成功时产物保留在
    cache 临时目录中，质量判定与落盘由 post_transcode 接手。
      - 失败/超时/异常：清理临时目录并 mark_failed，返回 False
      - 成功：返回 True，并把 output_path/dist_frame_count/elapsed 写入 job

    注意：转码侧刻意不产生任何帧率规整参数——实验证明 -r 会导致 VMAF 虚低、
    -fps_mode cfr 会导致产物帧率翻倍（2026-08-13）；源/产物帧率对齐只在打分侧
    按帧序号进行。
    """
    cache_dir = Path(job.lib_cache)
    cache_dir.mkdir(parents=True, exist_ok=True)

    filepath = Path(job.filepath)
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
        return False
    output_path = temp_dir / output_filename
    pipeline.history.mark_pending_transcode(str(job.filepath), str(output_path))

    try:
        # 转码侧不产生任何帧率规整参数；对齐只在打分侧按帧序号进行。

        # 软件缩放重试辅助函数（防双重重试：无论何种触发条件，最多重试一次）
        sw_scale_fallback_attempted = False

        def _retry_sw_scale():
            nonlocal returncode, stderr, temp_dir, output_path, sw_scale_fallback_attempted  # noqa: E501
            if sw_scale_fallback_attempted:
                return
            sw_scale_fallback_attempted = True
            log.warning(
                "QSV SW SCALE FALLBACK: %s - re-trying with software scale",
                job.filepath.name,
            )

            old_temp_dir = output_path.parent
            _cleanup_temp(temp_dir)

            suffix = _random_suffix()
            temp_dir = Path(job.lib_cache) / f"{config.TEMP_PREFIX}{suffix}"
            with _active_temp_dirs_lock:
                _active_temp_dirs.discard(str(old_temp_dir))
                _active_temp_dirs.add(str(temp_dir))
            temp_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_dir / output_filename
            pipeline.history.mark_pending_transcode(str(job.filepath), str(output_path))

            cmd, _ = _build_ffmpeg_cmd(
                pipeline.ffmpeg_path,
                pipeline.ffprobe_path,
                job.filepath,
                output_path,
                sw_scale_fallback=True,
            )
            log.info(
                "FFMPEG CMD (sw scale fallback): %s %s",
                job.filepath.name,
                " ".join(str(c) for c in cmd),
            )
            returncode, stderr = _run_ffmpeg_with_progress(
                cmd, total_duration, job.filepath.name, timeout=config.CODEC_TIMEOUT
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
            log.warning(
                "QSV SW DECODE FALLBACK: %s - re-trying with software decode",
                job.filepath.name,
            )

            old_temp_dir = output_path.parent
            _cleanup_temp(temp_dir)

            suffix = _random_suffix()
            temp_dir = Path(job.lib_cache) / f"{config.TEMP_PREFIX}{suffix}"
            with _active_temp_dirs_lock:
                _active_temp_dirs.discard(str(old_temp_dir))
                _active_temp_dirs.add(str(temp_dir))
            temp_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_dir / output_filename
            pipeline.history.mark_pending_transcode(str(job.filepath), str(output_path))

            cmd, _ = _build_ffmpeg_cmd(
                pipeline.ffmpeg_path,
                pipeline.ffprobe_path,
                job.filepath,
                output_path,
                sw_decode_fallback=True,
            )
            log.info(
                "FFMPEG CMD (sw decode fallback): %s %s",
                job.filepath.name,
                " ".join(str(c) for c in cmd),
            )
            returncode, stderr = _run_ffmpeg_with_progress(
                cmd, total_duration, job.filepath.name, timeout=config.CODEC_TIMEOUT
            )
            try:
                (temp_dir / ".ffmpeg.log").write_text(stderr, encoding="utf-8")
            except OSError:
                pass

        codec = get_video_codec(job.filepath, pipeline.ffprobe_path)
        if codec in config.SW_DECODE_FALLBACK_CODECS:
            sw_decode_fallback_attempted = True

        log.info(
            "TRANSCODE: %s (%s) -> %s (temp: %s)",
            job.filepath.name,
            _format_size(input_size),
            output_filename,
            temp_dir.name,
        )

        cmd, total_duration = _build_ffmpeg_cmd(
            pipeline.ffmpeg_path,
            pipeline.ffprobe_path,
            job.filepath,
            output_path,
            sw_decode_fallback=sw_decode_fallback_attempted,
        )
        log.info("FFMPEG CMD: %s %s", job.filepath.name, " ".join(str(c) for c in cmd))

        returncode, stderr = _run_ffmpeg_with_progress(
            cmd, total_duration, job.filepath.name, timeout=config.CODEC_TIMEOUT
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
            log.error("TRANSCODE FAILED: %s (%.1fs) - %s", job.filepath.name, elapsed, error_msg)
            _cleanup_temp(temp_dir)
            pipeline.history.mark_failed(str(job.filepath), error_msg)
            return False

        if not output_path.exists():
            elapsed = _time.monotonic() - start_time
            log.error(
                "TRANSCODE FAILED: output not found for %s (%.1fs)",
                job.filepath.name,
                elapsed,
            )
            _cleanup_temp(temp_dir)
            pipeline.history.mark_failed(str(job.filepath), "Output file not found after transcode")
            return False

        # 产物帧数直接取自 ffmpeg 最终进度行的 frame=N（与 -count_frames 计数
        # 同源），免去打分前对 mkv 产物再做一次昂贵的全量解码计数。
        dist_frame_count = parse_frame_count_from_stderr(stderr)
        elapsed = _time.monotonic() - start_time
        job.output_path = output_path
        job.dist_frame_count = dist_frame_count
        job.elapsed = elapsed
        return True

    except subprocess.TimeoutExpired:
        elapsed = _time.monotonic() - start_time
        log.error("TRANSCODE TIMEOUT: %s (%.1fs)", job.filepath.name, elapsed)
        _cleanup_temp(temp_dir)
        pipeline.history.mark_failed(str(job.filepath), "Transcode timed out")
        return False
    except Exception as e:
        elapsed = _time.monotonic() - start_time
        log.error("TRANSCODE ERROR: %s (%.1fs) - %s", job.filepath.name, elapsed, e)
        _cleanup_temp(temp_dir)
        pipeline.history.mark_failed(str(job.filepath), str(e))
        return False
    finally:
        with _active_temp_dirs_lock:
            _active_temp_dirs.discard(str(temp_dir))

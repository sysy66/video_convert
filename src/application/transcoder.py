"""
转码模块。实现单个视频文件的编码阶段：构建 FFmpeg 命令、执行并实时记录
进度；成功时把产物路径/帧数/耗时写回 job（质量判定与落盘交给
post_process_stage），失败时清理临时目录并标记历史状态。

Transcoding module. Implements the encoding phase for a single video: build
the FFmpeg command and run it with live progress logging. On success, writes
the output path/frame count/elapsed time back onto the job (quality
verdict and landing are handled by post_process_stage); on failure, cleans up
the temp dir and marks history.
"""

import subprocess
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.application.job import TranscodeJob
    from src.application.pipeline import TranscodePipeline
from src import config
from src.media.ffmpeg_runner import (
    _build_ffmpeg_cmd,
    _run_ffmpeg_with_progress,
    _summarize_error_stderr,
    parse_frame_count_from_stderr,
)
from src.runtime.temp_cleanup import (
    _active_temp_dirs,
    _active_temp_dirs_lock,
    _cleanup_temp,
    _random_suffix,
)
from src.support.format_utils import _format_size

log = __import__("logging").getLogger(__name__)


def _write_log(temp_dir: Path, name: str, content: str) -> None:
    """Best-effort write of ffmpeg stderr to temp_dir/name."""
    try:
        (temp_dir / name).write_text(content, encoding="utf-8")
    except OSError:
        pass


def transcode_stage(pipeline: "TranscodePipeline", job: "TranscodeJob") -> bool:
    """
    对单个视频执行转码（仅编码阶段）：创建临时工作目录、构建并运行 FFmpeg
    命令（含 QSV 软件缩放/软解码各一次的降级重试），成功时产物保留在
    cache 临时目录中，质量判定与落盘由 post_process_stage 接手。
      - 失败/超时/异常：清理临时目录并 mark_failed，返回 False
      - 成功：返回 True，并把 output_path/dist_frame_count/transcode_elapsed 写入 job

    注意：转码侧刻意不加任何帧率规整参数（-r 会导致 VMAF 虚低，-fps_mode cfr
    会让产物帧率翻倍）；源/产物帧率对齐只在打分侧按帧序号进行，见
    FfmpegVMAFScorer._frame_index_align_string。
    """
    cache_dir = Path(job.lib_cache)
    cache_dir.mkdir(parents=True, exist_ok=True)

    filepath = Path(job.filepath)
    stem = filepath.stem
    output_filename = f"{stem}.mkv"

    start_time = _time.monotonic()
    input_size = filepath.stat().st_size

    # --- temp dir helper: alloc/rotate 合一，消除重复 ---
    def _new_temp_dir(old_td: Path | None = None) -> tuple[Path, Path]:
        if old_td is not None:
            _cleanup_temp(old_td)
        suffix = _random_suffix()
        td = cache_dir / f"{config.TEMP_PREFIX}{suffix}"
        with _active_temp_dirs_lock:
            if old_td is not None:
                _active_temp_dirs.discard(str(old_td))
            _active_temp_dirs.add(str(td))
        try:
            td.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            with _active_temp_dirs_lock:
                _active_temp_dirs.discard(str(td))
            # 首次分配才打印，轮转失败由外层统一按 "Transcode ..." 报错，避免噪音
            if old_td is None:
                log.error("Failed to create temp dir: %s", e)
            raise
        op = td / output_filename
        # 仅首次分配工作目录开始一次转码记录；fallback 仍属于同一次转码尝试。
        # Record one transcode start; fallback retries remain part of the same attempt.
        if old_td is None:
            pipeline.history.mark_pending_transcode(str(job.filepath))
        return td, op

    try:
        temp_dir, output_path = _new_temp_dir()
    except Exception:
        return False

    success = False
    returncode: int = 0
    stderr: str = ""
    total_duration: float = 0.0

    def _fail(msg: str, kind: str = "failed") -> bool:
        """统一失败出口：日志前缀按 kind 保留原样，DB 文案原样。"""
        transcode_elapsed = _time.monotonic() - start_time
        if kind == "timeout":
            log.error("TRANSCODE TIMEOUT: %s (%.1fs)", job.filepath.name, transcode_elapsed)
        elif kind == "error":
            log.error("TRANSCODE ERROR: %s (%.1fs) - %s", job.filepath.name, transcode_elapsed, msg)
        else:
            log.error(
                "TRANSCODE FAILED: %s (%.1fs) - %s", job.filepath.name, transcode_elapsed, msg
            )
        _cleanup_temp(temp_dir)
        pipeline.history.mark_failed(str(job.filepath), msg)
        return False

    try:
        # 软件降级重试状态（防双重重试：每种最多一次）
        fallback_done = {"scale": False, "decode": False}

        # 编码已知 QSV 无法硬解时，首轮直接走软解码，不必先跑一次必然失败的
        # 硬解再触发重试；同时标记 fallback_done，避免下面再重试一次 decode。
        # For codecs known to be QSV-incompatible, use software decode on the
        # first attempt instead of a doomed hardware attempt followed by a
        # retry; this also marks fallback_done so decode isn't retried again.
        codec = pipeline.probe.get_codec(job.filepath)
        if codec in config.SW_DECODE_FALLBACK_CODECS:
            fallback_done["decode"] = True

        def _exec_ffmpeg(log_label: str, log_file: str, out_path: Path, **kwargs) -> None:
            """构建命令→执行→写日志三件套，首轮与重试共用。"""
            nonlocal returncode, stderr, total_duration
            cmd, total_duration = _build_ffmpeg_cmd(
                pipeline.ffmpeg_path,
                pipeline.ffprobe_path,
                job.filepath,
                out_path,
                probe=pipeline.probe,
                **kwargs,
            )
            log.info("%s: %s %s", log_label, job.filepath.name, " ".join(str(c) for c in cmd))
            returncode, stderr = _run_ffmpeg_with_progress(
                cmd, total_duration, job.filepath.name, timeout=config.CODEC_TIMEOUT
            )
            _write_log(temp_dir, log_file, stderr)

        def _retry(mode: str) -> None:
            nonlocal temp_dir, output_path
            if fallback_done[mode]:
                return
            fallback_done[mode] = True
            label = "SW SCALE" if mode == "scale" else "SW DECODE"
            log.warning(
                "QSV %s FALLBACK: %s - re-trying with software %s",
                label,
                job.filepath.name,
                "scale" if mode == "scale" else "decode",
            )
            temp_dir, output_path = _new_temp_dir(old_td=temp_dir)
            kwargs = (
                {"sw_scale_fallback": True} if mode == "scale" else {"sw_decode_fallback": True}
            )
            _exec_ffmpeg(
                f"FFMPEG CMD ({mode} fallback)", f".ffmpeg.log.{mode}", output_path, **kwargs
            )

        log.info(
            "TRANSCODE: %s (%s) -> %s (temp: %s)",
            job.filepath.name,
            _format_size(input_size),
            output_filename,
            temp_dir.name,
        )

        _exec_ffmpeg(
            "FFMPEG CMD",
            ".ffmpeg.log.qsv",
            output_path,
            sw_decode_fallback=fallback_done["decode"],
        )

        # 按 stderr 特征选择降级路径
        if returncode != 0:
            if fallback_done["decode"]:
                pass
            elif "Error reinitializing filters" in stderr or "Error initializing filters" in stderr:
                _retry("scale")
                if returncode != 0:
                    _retry("decode")
            else:
                _retry("decode")

        _write_log(temp_dir, ".ffmpeg.log", stderr)

        if returncode != 0:
            msg = _summarize_error_stderr(stderr) or f"exit code {returncode}"
            return _fail(msg)

        if not output_path.exists():
            return _fail("Output file not found after transcode")

        dist_frame_count = parse_frame_count_from_stderr(stderr)
        transcode_elapsed = _time.monotonic() - start_time
        job.output_path = output_path
        job.dist_frame_count = dist_frame_count
        job.transcode_elapsed = transcode_elapsed
        pipeline.history.mark_transcode_finished(str(job.filepath))
        success = True
        return True

    except subprocess.TimeoutExpired:
        return _fail("Transcode timed out", kind="timeout")
    except Exception as e:
        return _fail(str(e), kind="error")
    finally:
        if not success:
            with _active_temp_dirs_lock:
                _active_temp_dirs.discard(str(temp_dir))

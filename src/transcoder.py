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


def _write_log(temp_dir: Path, name: str, content: str) -> None:
    """Best-effort write of ffmpeg stderr to temp_dir/name."""
    try:
        (temp_dir / name).write_text(content, encoding="utf-8")
    except OSError:
        pass


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
        pipeline.history.mark_pending_transcode(str(job.filepath), str(op))
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
        elapsed = _time.monotonic() - start_time
        if kind == "timeout":
            log.error("TRANSCODE TIMEOUT: %s (%.1fs)", job.filepath.name, elapsed)
        elif kind == "error":
            log.error("TRANSCODE ERROR: %s (%.1fs) - %s", job.filepath.name, elapsed, msg)
        else:
            log.error("TRANSCODE FAILED: %s (%.1fs) - %s", job.filepath.name, elapsed, msg)
        _cleanup_temp(temp_dir)
        pipeline.history.mark_failed(str(job.filepath), msg)
        return False

    try:
        # 软件降级重试状态（防双重重试：每种最多一次）
        fallback_done = {"scale": False, "decode": False}

        codec = get_video_codec(job.filepath, pipeline.ffprobe_path)
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
        elapsed = _time.monotonic() - start_time
        job.output_path = output_path
        job.dist_frame_count = dist_frame_count
        job.elapsed = elapsed
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

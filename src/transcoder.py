import os
import re
import shutil
import string
import random
import subprocess
import threading
from pathlib import Path

from src import config
from src.probe import get_streams_info

log = __import__("logging").getLogger(__name__)


def _random_suffix(length=6):
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=length))


def _build_ffmpeg_cmd(ffmpeg_path, ffprobe_path, input_path, output_path):
    info = get_streams_info(input_path, ffprobe_path)
    if info is None:
        raise RuntimeError(f"Failed to probe streams for {input_path}")

    streams = info["streams"]
    duration = info["duration"]

    audio_codecs = set()
    for s in streams:
        if s.get("codec_type") == "audio":
            codec = s.get("codec_name", "").lower()
            if codec:
                audio_codecs.add(codec)

    cmd = [
        ffmpeg_path, "-y",
        "-hwaccel", "qsv", "-hwaccel_output_format", "qsv",
        "-i", str(input_path),
        "-map", "0:v:0", "-c:v", "hevc_qsv", "-profile:v", "main10",
        "-global_quality", str(config.VIDEO_QUALITY),
        "-preset", config.VIDEO_PRESET,
        "-look_ahead", "1", "-look_ahead_depth", str(config.VIDEO_LOOK_AHEAD_DEPTH),
        "-g", str(config.VIDEO_GOP), "-bf", str(config.VIDEO_B_FRAMES),
        "-extbrc", "1",
        "-adaptive_i", "1", "-adaptive_b", "1",
        "-vf", "vpp_qsv=format=p010le", "-fps_mode", "cfr",
        "-map", "0:a?",
    ]

    if audio_codecs and audio_codecs.issubset(config.AUDIO_PASSTHROUGH_CODECS):
        cmd.extend(["-c:a", "copy"])
    else:
        cmd.extend(["-c:a", config.AUDIO_CODEC, "-b:a", config.AUDIO_BITRATE])

    if config.KEEP_SUBTITLES:
        cmd.extend(["-map", "0:s?", "-c:s", "copy"])

    if config.KEEP_ATTACHMENTS:
        cmd.extend(["-map", "0:t?", "-c:t", "copy"])

    cmd.append(str(output_path))
    return cmd, duration


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")


def _parse_progress_time(line):
    m = _TIME_RE.search(line)
    if m:
        h, min_, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return h * 3600 + min_ * 60 + s
    return None


def _run_ffmpeg_with_progress(cmd, total_duration, file_stem):
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stderr_lines = []
    last_logged_pct = -1
    reader_done = threading.Event()

    def _reader():
        for line in iter(proc.stderr.readline, ""):
            stderr_lines.append(line)
            if total_duration:
                secs = _parse_progress_time(line)
                if secs is not None and secs > 0:
                    pct = int(secs / total_duration * 100)
                    if pct >= last_logged_pct + 10:
                        log.info("PROGRESS: %s - %d%%", file_stem, min(pct, 100))
                        last_logged_pct = pct
        reader_done.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    proc.wait()
    reader_done.wait(timeout=10)

    return proc.returncode, "".join(stderr_lines)


def transcode(filepath, history, ffmpeg_path, ffprobe_path):
    filepath = Path(filepath)
    parent_dir = filepath.parent
    stem = filepath.stem
    suffix = _random_suffix()
    temp_dir_name = f"{config.TEMP_PREFIX}{suffix}"
    temp_dir = parent_dir / temp_dir_name

    output_filename = f"{stem}.mkv"
    output_path = temp_dir / output_filename

    history.mark_started(str(filepath))

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        log.info("TRANSCODE: %s -> %s (temp: %s)", filepath.name, output_filename, temp_dir_name)

        cmd, total_duration = _build_ffmpeg_cmd(ffmpeg_path, ffprobe_path, filepath, output_path)
        log.debug("FFmpeg command: %s", " ".join(str(c) for c in cmd))

        returncode, stderr = _run_ffmpeg_with_progress(cmd, total_duration, filepath.name)

        if returncode != 0:
            error_msg = stderr.strip() or f"exit code {returncode}"
            log.error("TRANSCODE FAILED: %s - %s", filepath.name, error_msg)
            history.mark_failed(str(filepath), error_msg)
            _cleanup_temp(temp_dir)
            return False

        if not output_path.exists():
            log.error("TRANSCODE FAILED: output not found for %s", filepath.name)
            history.mark_failed(str(filepath), "Output file not found after transcode")
            _cleanup_temp(temp_dir)
            return False

        log.info("REPLACE: %s -> %s", filepath.name, output_filename)
        filepath.unlink()
        shutil.move(str(output_path), str(parent_dir / output_filename))
        _cleanup_temp(temp_dir)

        history.mark_done(str(filepath), str(parent_dir / output_filename))
        log.info("DONE: %s", output_filename)
        return True

    except subprocess.TimeoutExpired:
        log.error("TRANSCODE TIMEOUT: %s", filepath.name)
        history.mark_failed(str(filepath), "Transcode timed out")
        _cleanup_temp(temp_dir)
        return False
    except Exception as e:
        log.error("TRANSCODE ERROR: %s - %s", filepath.name, e)
        history.mark_failed(str(filepath), str(e))
        _cleanup_temp(temp_dir)
        return False


def _cleanup_temp(temp_dir):
    try:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
            log.debug("Cleaned up temp dir: %s", temp_dir)
    except OSError as e:
        log.warning("Failed to clean up temp dir %s: %s", temp_dir, e)


def cleanup_stale_temp_dirs(watch_dir, temp_prefix):
    count = 0
    for root, dirs, _ in os.walk(watch_dir):
        for d in list(dirs):
            if d.startswith(temp_prefix):
                full_path = Path(root) / d
                try:
                    shutil.rmtree(full_path)
                    count += 1
                    log.info("Removed stale temp dir: %s", full_path)
                except OSError as e:
                    log.warning("Failed to remove stale temp dir %s: %s", full_path, e)
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith(temp_prefix)]
    if count:
        log.info("Cleaned up %d stale temp director%s", count, "ies" if count != 1 else "y")




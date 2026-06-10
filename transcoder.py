import os
import shutil
import string
import random
import subprocess
from pathlib import Path

log = __import__("logging").getLogger(__name__)


def _random_suffix(length=6):
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=length))


def _build_ffmpeg_cmd(ffmpeg_path, input_path, output_path):
    return [
        ffmpeg_path, "-y",
        "-hwaccel", "qsv", "-hwaccel_output_format", "qsv",
        "-i", str(input_path),
        "-map", "0:0", "-c:v", "hevc_qsv", "-profile:v", "main10",
        "-global_quality", "20", "-preset", "slow",
        "-look_ahead", "1", "-look_ahead_depth", "70",
        "-g", "240", "-bf", "8", "-extbrc", "1",
        "-adaptive_i", "1", "-adaptive_b", "1",
        "-vf", "vpp_qsv=format=p010le", "-fps_mode", "cfr",
        "-map", "0:1", "-c:a", "aac", "-b:a", "256k",
        str(output_path)
    ]


def transcode(filepath, history, ffmpeg_path):
    filepath = Path(filepath)
    parent_dir = filepath.parent
    stem = filepath.stem
    suffix = _random_suffix()
    temp_dir_name = f"{TEMP_PREFIX}{suffix}"
    temp_dir = parent_dir / temp_dir_name

    output_filename = f"{stem}.mkv"
    output_path = temp_dir / output_filename

    history.mark_started(str(filepath))

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        log.info("TRANSCODE: %s -> %s (temp: %s)", filepath.name, output_filename, temp_dir_name)

        cmd = _build_ffmpeg_cmd(ffmpeg_path, filepath, output_path)
        log.debug("FFmpeg command: %s", " ".join(str(c) for c in cmd))

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=86400
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or f"exit code {result.returncode}"
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


TEMP_PREFIX = __import__("config").TEMP_PREFIX

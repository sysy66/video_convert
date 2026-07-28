import json
import subprocess

log = __import__("logging").getLogger(__name__)


def get_video_codec(filepath, ffprobe_path):
    if not ffprobe_path:
        log.error("FFPROBE_PATH not configured")
        return None
    cmd = [
        ffprobe_path, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(filepath)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.error("ffprobe failed for %s: %s", filepath, result.stderr.strip())
            return None
        codec = result.stdout.strip().lower()
        return codec
    except FileNotFoundError:
        log.error("ffprobe not found at %s", ffprobe_path)
        return None
    except subprocess.TimeoutExpired:
        log.error("ffprobe timed out for %s", filepath)
        return None
    except Exception as e:
        log.error("ffprobe error for %s: %s", filepath, e)
        return None


def get_video_pix_fmt(filepath, ffprobe_path):
    if not ffprobe_path:
        log.error("FFPROBE_PATH not configured")
        return None
    cmd = [
        ffprobe_path, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=pix_fmt",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(filepath)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.error("ffprobe failed for %s: %s", filepath, result.stderr.strip())
            return None
        return result.stdout.strip().lower()
    except FileNotFoundError:
        log.error("ffprobe not found at %s", ffprobe_path)
        return None
    except subprocess.TimeoutExpired:
        log.error("ffprobe timed out for %s", filepath)
        return None
    except Exception as e:
        log.error("ffprobe error for %s: %s", filepath, e)
        return None


def get_streams_info(filepath, ffprobe_path):
    if not ffprobe_path:
        log.error("FFPROBE_PATH not configured")
        return None
    cmd = [
        ffprobe_path, "-v", "error",
        "-show_streams",
        "-show_format",
        "-of", "json",
        str(filepath)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.error("ffprobe failed for %s: %s", filepath, result.stderr.strip())
            return None
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format", {})
        duration = None
        if fmt and "duration" in fmt:
            try:
                duration = float(fmt["duration"])
            except (ValueError, TypeError):
                pass
        return {"streams": streams, "duration": duration}
    except FileNotFoundError:
        log.error("ffprobe not found at %s", ffprobe_path)
        return None
    except subprocess.TimeoutExpired:
        log.error("ffprobe timed out for %s", filepath)
        return None
    except json.JSONDecodeError as e:
        log.error("ffprobe JSON decode error for %s: %s", filepath, e)
        return None
    except Exception as e:
        log.error("ffprobe error for %s: %s", filepath, e)
        return None

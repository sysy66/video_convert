import json
import subprocess

log = __import__("logging").getLogger(__name__)


def get_video_codec(filepath, ffprobe_path):
    if not ffprobe_path:
        log.error("FFPROBE_PATH not configured")
        return None
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(filepath),
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
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=pix_fmt",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(filepath),
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


def get_video_frame_rate(filepath, ffprobe_path):
    """
    探测视频流帧率，返回 ffmpeg fps 滤镜可直接使用的分数形式，如 "30000/1001"。
    优先取 avg_frame_rate（VFR 的平均帧率），无效（0/0）时回退 r_frame_rate。
    Probe the frame rate of v:0, returning a fraction usable by the fps filter
    (e.g. "30000/1001"). Prefers avg_frame_rate, falling back to r_frame_rate.
    """
    if not ffprobe_path:
        log.error("FFPROBE_PATH not configured")
        return None
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate:stream=r_frame_rate",
        "-of",
        "default=noprint_wrappers=1:nokey=0",
        str(filepath),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.error("ffprobe failed for %s: %s", filepath, result.stderr.strip())
            return None
    except FileNotFoundError:
        log.error("ffprobe not found at %s", ffprobe_path)
        return None
    except subprocess.TimeoutExpired:
        log.error("ffprobe timed out for %s", filepath)
        return None
    except Exception as e:
        log.error("ffprobe error for %s: %s", filepath, e)
        return None

    def _valid(fr):
        if not fr:
            return None
        fr = fr.strip()
        if "/" in fr:
            num, _, den = fr.partition("/")
            try:
                n, d = int(num), int(den)
            except ValueError:
                return None
            return fr if n > 0 and d > 0 else None
        try:
            return fr if float(fr) > 0 else None
        except ValueError:
            return None

    entries = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    avg = _valid(entries.get("avg_frame_rate", ""))
    if avg:
        return avg
    return _valid(entries.get("r_frame_rate", ""))


def fps_fraction_to_float(fps_str):
    """
    把 ffprobe 返回的帧率字符串（如 "24000/1001" 或 "23.976"）转为 float。
    无效输入返回 None。
    Convert a frame-rate string (e.g. "24000/1001" or "23.976") to float, or None.
    """
    if not fps_str:
        return None
    s = str(fps_str).strip()
    if "/" in s:
        num, _, den = s.partition("/")
        try:
            n, d = int(num), int(den)
        except ValueError:
            return None
        return n / d if d else None
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def get_streams_info(filepath, ffprobe_path):
    if not ffprobe_path:
        log.error("FFPROBE_PATH not configured")
        return None
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(filepath),
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


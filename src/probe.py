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


def get_expected_frame_count(duration, fps_str):
    """
    由时长（秒）与帧率字符串估算应输出的总帧数，供转码后帧数校验使用。
    任一参数无效时返回 None。
    Estimate the expected total frame count from duration (seconds) and a frame-rate
    string, for post-transcode frame-count validation. Returns None if either is invalid.
    """
    fps = fps_fraction_to_float(fps_str)
    if duration is None or fps is None:
        return None
    return int(duration * fps)


def get_video_frame_count(filepath, ffprobe_path):
    """
    统计视频流的实际帧数（全量解码计数，精确但较慢，仅用于异常校验时调用）。
    返回 int 帧数，失败返回 None。
    Count the actual number of video frames via full decode (accurate but slow;
    intended only for anomaly validation). Returns int, or None on failure.
    """
    if not ffprobe_path:
        log.error("FFPROBE_PATH not configured")
        return None
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(filepath),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            log.error("ffprobe frame count failed for %s: %s", filepath, result.stderr.strip())
            return None
        text = result.stdout.strip()
        try:
            return int(text) if text else None
        except ValueError:
            log.error("unexpected frame count output for %s: %r", filepath, text)
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


def get_video_stream_duration(streams_info):
    """
    从 get_streams_info 的结果中取首个视频流的时长（秒）。
    无视频流或时长不可用时返回 None。容器 format.duration 可能被完整复制的
    音频/字幕撑满，掩盖视频轨被截断的事实，因此需要按视频流自身时长判断。
    注意：ffmpeg 写出的 mkv 通常不写流级时长（返回 None），此时需用
    video_stream_covers_container 做廉价的包级探测。
    """
    if not streams_info:
        return None
    for s in streams_info.get("streams", []):
        if s.get("codec_type") == "video":
            duration = s.get("duration")
            if duration is None:
                return None
            try:
                return float(duration)
            except (ValueError, TypeError):
                return None
    return None


def video_stream_covers_container(filepath, ffprobe_path, total_duration, tail_window=30.0):
    """
    廉价探测视频轨是否覆盖到容器末尾：seek 到容器最后 tail_window 秒，读视频包，
    若存在 pts >= total_duration * FRAME_COUNT_MIN_RATIO 的包则视为正常。
    视频轨被截断（vpp_qsv 只编码了几帧就 rc=0 退出）时，容器时长会被完整复制的
    音频/字幕撑满，而末尾区间没有视频包——此探测在 ~1s 内区分两者。
    返回 True/False；探测失败返回 None（调用方保守按可疑处理）。
    """
    if not ffprobe_path:
        log.error("FFPROBE_PATH not configured")
        return None
    if not total_duration or total_duration <= 0:
        return None
    window = min(max(tail_window, 5.0), total_duration * 0.05)
    start = max(0.0, total_duration - window)
    interval = f"{start:.3f}%{total_duration:.3f}"
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time",
        "-read_intervals",
        interval,
        "-of",
        "csv=p=0",
        str(filepath),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        log.error("ffprobe not found at %s", ffprobe_path)
        return None
    except subprocess.TimeoutExpired:
        log.error("ffprobe timed out for %s", filepath)
        return None
    except Exception as e:
        log.error("ffprobe error for %s: %s", filepath, e)
        return None
    if result.returncode != 0:
        log.error("ffprobe failed for %s: %s", filepath, result.stderr.strip())
        return None
    threshold = total_duration * 0.5
    for line in result.stdout.splitlines():
        pts = line.strip()
        if not pts or pts == "N/A":
            continue
        try:
            if float(pts) >= threshold:
                return True
        except ValueError:
            continue
    return False

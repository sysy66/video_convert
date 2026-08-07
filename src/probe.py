import json
import subprocess
from collections import Counter

log = __import__("logging").getLogger(__name__)

_COMMON_FRAME_RATES = (
    ("24000/1001", 24000.0 / 1001.0),
    ("24/1", 24.0),
    ("25/1", 25.0),
    ("30000/1001", 30000.0 / 1001.0),
    ("30/1", 30.0),
    ("50/1", 50.0),
    ("60000/1001", 60000.0 / 1001.0),
    ("60/1", 60.0),
    ("120/1", 120.0),
)

#: avg_frame_rate 与 r_frame_rate 相对差超过该值即视为疑似 VFR，改用帧时间戳探测
_VFR_SUSPECT_RATIO = 0.01
#: 帧间隔与常见帧率表周期的最大相对偏差（0.5%），超过即拒绝投票。
#: 足够吸收封装时间戳量化抖动（90kHz tick ±6us），又不至于让 30 与 29.97 互相污染。
_PTS_MAX_DEVIATION = 0.005
#: 得票最高项的占比低于该值视为帧序不可靠，放弃投票结果
_PTS_VOTE_MIN_RATIO = 0.3
#: 帧时间戳抽样：前/中/尾各取该秒数（总样本 ~15s，分 3 段覆盖帧率中途切换）
_PTS_SAMPLE_SECONDS = 5
#: 抽样的三个相对位置（前/中/尾）
_PTS_SAMPLE_FRACTIONS = ("0%", "50%", "95%")
#: 抽样窗口内至少需要的帧间隔数
_PTS_MIN_INTERVALS = 30


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


def _probe_pts_intervals(filepath, ffprobe_path):
    """
    抽样读取 v:0 流的帧时间戳（前/中/尾各 5 秒），返回帧间隔列表（微秒）。
    分段抽样覆盖帧率中途切换的源；段间的大间隔会被过滤掉。
    抽样失败或有效间隔不足时返回 None。
    """
    intervals = ",".join(f"{p}+{_PTS_SAMPLE_SECONDS}" for p in _PTS_SAMPLE_FRACTIONS)
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "frame=pts_time",
        "-of",
        "csv=p=0",
        "-read_intervals",
        intervals,
        str(filepath),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log.error("ffprobe pts probe failed for %s: %s", filepath, result.stderr.strip())
            return None
    except FileNotFoundError:
        log.error("ffprobe not found at %s", ffprobe_path)
        return None
    except subprocess.TimeoutExpired:
        log.error("ffprobe pts probe timed out for %s", filepath)
        return None
    except Exception as e:
        log.error("ffprobe pts probe error for %s: %s", filepath, e)
        return None

    pts = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pts.append(float(line))
        except ValueError:
            continue
    if len(pts) < _PTS_MIN_INTERVALS + 1:
        log.warning("pts probe for %s got too few frames (%d)", filepath, len(pts))
        return None

    diffs_us = []
    for i in range(1, len(pts)):
        d = (pts[i] - pts[i - 1]) * 1e6
        if 0 < d <= 1_000_000:
            diffs_us.append(round(d))
    if len(diffs_us) < _PTS_MIN_INTERVALS:
        log.warning("pts probe for %s: too few valid intervals (%d)", filepath, len(diffs_us))
        return None
    return diffs_us


def _vote_common_rate(diffs_us):
    """
    对常见帧率表加权投票：每个帧间隔给偏差 ≤0.5% 的表项加分，
    权重 = 1 - 偏差/容差（越接近得分越高）。
    时间戳量化会把同一帧率网格拆成多个接近的间隔（如 90kHz tick 下的
    33317/33333/33350us），逐桶取众数占比可能虚低；加权投票按"接近度"
    聚合，天然免疫拆桶，且接近 29.97 与 30 之间的间隔（33350us 这类中点）
    不会因平局误判。返回 (帧率分数, 得分占比)，占比不足返回 None。
    """
    votes = Counter()
    for d in diffs_us:
        for frac, table_rate in _COMMON_FRAME_RATES:
            dev = abs(d - 1e6 / table_rate) / (1e6 / table_rate)
            if dev <= _PTS_MAX_DEVIATION:
                votes[frac] += 1 - dev / _PTS_MAX_DEVIATION
    if not votes:
        return None
    frac, score = votes.most_common(1)[0]
    ratio = score / len(diffs_us)
    if ratio < _PTS_VOTE_MIN_RATIO:
        log.warning(
            "帧间隔投票不可靠：最高得票 %s 仅占 %.1f%%", frac, 100.0 * ratio
        )
        return None
    return frac, ratio


def _normalize_common_rate(fps_str):
    """
    把与常见帧率表最接近且偏差 ≤0.05% 的帧率归一化为表值
    （如 "73743750/2460583"≈29.97 → "30000/1001"），其余原样返回。
    容差 0.05% 小于 30 与 29.97 的间距（0.1%），不会互相污染。
    """
    f = fps_fraction_to_float(fps_str)
    if f is None:
        return fps_str
    best, best_dev = None, None
    for frac, table_rate in _COMMON_FRAME_RATES:
        dev = abs(f - table_rate) / table_rate
        if best_dev is None or dev < best_dev:
            best, best_dev = frac, dev
    if best_dev is not None and best_dev <= 0.0005:
        return best
    return fps_str


def get_video_frame_rate(filepath, ffprobe_path):
    """
    探测视频流帧率，返回 ffmpeg fps 滤镜可直接使用的分数形式，如 "30000/1001"。
    元数据 avg_frame_rate 与 r_frame_rate 一致时直接信任 avg_frame_rate；
    不一致或缺失（疑似 VFR）时抽样帧时间戳、按间隔众数求真实内容帧率，
    探测失败再回退 avg_frame_rate，最后回退 r_frame_rate。
    Probe the frame rate of v:0, returning a fraction usable by the fps filter
    (e.g. "30000/1001"). Trusts avg_frame_rate when it agrees with
    r_frame_rate; for suspected VFR sources samples frame PTS and uses the
    interval mode, falling back to the metadata rates if probing fails.
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
    r = _valid(entries.get("r_frame_rate", ""))
    if avg and r:
        avg_f = fps_fraction_to_float(avg)
        r_f = fps_fraction_to_float(r)
        if avg_f and r_f and abs(avg_f - r_f) / avg_f <= _VFR_SUSPECT_RATIO:
            return _normalize_common_rate(avg)

    if avg or r:
        diffs_us = _probe_pts_intervals(filepath, ffprobe_path)
        if diffs_us is not None:
            voted = _vote_common_rate(diffs_us)
            if voted is not None:
                frac, ratio = voted
                log.info(
                    "疑似 VFR %s: avg=%s r=%s 帧间隔投票 %s (%.1f%%) → 使用 %s",
                    filepath,
                    avg or "-",
                    r or "-",
                    frac,
                    100.0 * ratio,
                    frac,
                )
                return frac
        log.warning(
            "疑似 VFR %s: 帧间隔投票不可靠，回退元数据 (avg=%s r=%s)",
            filepath,
            avg or "-",
            r or "-",
        )
    return _normalize_common_rate(avg or r)


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


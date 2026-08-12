"""
探测模块。封装 ffprobe 调用，获取视频的编码、像素格式、帧率、帧间隔等元数据，
并对帧率进行归一化/投票以处理 VFR 源。

Probing module. Wraps ffprobe to obtain video metadata (codec, pixel format,
frame rate, frame intervals, etc.) and normalizes/votes the frame rate to handle
VFR sources.
"""

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


class VideoProbe:
    """封装 ffprobe 探测。ffprobe 路径在构造时固定，各探测方法只接受文件路径。

    Probe wrapper class around ffprobe. The ffprobe binary path is fixed at
    construction time; each probe method only takes the file path.
    """

    def __init__(self, ffprobe_path):
        """
        :param ffprobe_path: ffprobe 可执行文件路径 (Path to the ffprobe executable)
        """
        self.ffprobe_path = ffprobe_path

    def _run(self, cmd, timeout=30):
        """
        执行 ffprobe 命令并统一处理错误：仅当命令存在、执行成功且返回码为 0 时
        返回 CompletedProcess，其余情况（路径未配置/未找到/超时/异常/非零退出）返回 None。

        Run an ffprobe command with unified error handling; returns the
        CompletedProcess only when it exists, runs cleanly and exits 0, otherwise
        None (unconfigured/not-found path, timeout, exception, or nonzero exit).
        """
        if not self.ffprobe_path:
            log.error("FFPROBE_PATH not configured")
            return None
        target = str(cmd[-1]) if cmd else ""
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            log.error("ffprobe not found at %s", self.ffprobe_path)
            return None
        except subprocess.TimeoutExpired:
            log.error("ffprobe timed out for %s", target)
            return None
        except Exception as e:
            log.error("ffprobe error for %s: %s", target, e)
            return None
        if result.returncode != 0:
            log.error("ffprobe failed for %s: %s", target, result.stderr.strip())
            return None
        return result

    def get_codec(self, filepath):
        """探测视频流的编码名称（如 h264/hevc/av1）。

        Probe the codec name of the video stream (e.g. h264/hevc/av1).

        :param filepath: 视频文件路径 (Path to the video file)
        :return: 小写的编码名，失败时返回 None (Lowercase codec name, or None on failure)
        """
        cmd = [
            self.ffprobe_path,
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
        result = self._run(cmd)
        if result is None:
            return None
        return result.stdout.strip().lower()

    def get_pix_fmt(self, filepath):
        """探测视频流的像素格式（如 yuv420p / yuv420p10le）。

        Probe the pixel format of the video stream (e.g. yuv420p / yuv420p10le).

        :param filepath: 视频文件路径 (Path to the video file)
        :return: 像素格式字符串，失败时返回 None (Pixel format string, or None on failure)
        """
        cmd = [
            self.ffprobe_path,
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
        result = self._run(cmd)
        if result is None:
            return None
        return result.stdout.strip().lower()

    def get_frame_rate(self, filepath):
        """
        探测视频流帧率，返回 ffmpeg fps 滤镜可直接使用的分数形式，如 "30000/1001"。
        元数据 avg_frame_rate 与 r_frame_rate 一致时直接信任 avg_frame_rate；
        不一致或缺失（疑似 VFR）时抽样帧时间戳、对常见帧率表加权投票求真实内容帧率，
        探测失败再回退 avg_frame_rate，最后回退 r_frame_rate。
        Probe the frame rate of v:0, returning a fraction usable by the fps filter
        (e.g. "30000/1001"). Trusts avg_frame_rate when it agrees with
        r_frame_rate; for suspected VFR sources samples frame PTS and uses a weighted
        vote over the common frame-rate table, falling back to the metadata rates if
        probing fails.
        """
        cmd = [
            self.ffprobe_path,
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
        result = self._run(cmd)
        if result is None:
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
            avg_f = self.fps_fraction_to_float(avg)
            r_f = self.fps_fraction_to_float(r)
            if avg_f and r_f and abs(avg_f - r_f) / avg_f <= _VFR_SUSPECT_RATIO:
                return self._normalize_common_rate(avg)

        if avg or r:
            diffs_us = self._probe_pts_intervals(filepath)
            if diffs_us is not None:
                voted = self._vote_common_rate(diffs_us)
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
        return self._normalize_common_rate(avg or r)

    def get_streams_info(self, filepath):
        """探测文件所有流与容器格式信息，返回 {streams, duration} 结构（duration 可能为 None）。

        Probe all streams and the container format, returning a {streams, duration}
        structure (duration may be None).

        :param filepath: 视频文件路径 (Path to the video file)
        :return: 含 streams 列表与 duration 时长的字典，失败时返回 None
                 (Dict with streams list and duration, or None on failure)
        """
        cmd = [
            self.ffprobe_path,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(filepath),
        ]
        result = self._run(cmd)
        if result is None:
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            log.error("ffprobe JSON decode error for %s: %s", filepath, e)
            return None
        streams = data.get("streams", [])
        fmt = data.get("format", {})
        duration = None
        if fmt and "duration" in fmt:
            try:
                duration = float(fmt["duration"])
            except (ValueError, TypeError):
                pass
        return {"streams": streams, "duration": duration}

    @staticmethod
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

    def _probe_pts_intervals(self, filepath):
        """
        抽样读取 v:0 流的帧时间戳（前/中/尾各取 _PTS_SAMPLE_SECONDS 秒），
        返回相邻帧的时间间隔列表（微秒）。分段抽样覆盖帧率中途切换的源；
        无效间隔（≤0 或 >1 秒，含段间空隙）会被过滤，有效间隔不足则返回 None。

        Sample frame PTS of stream v:0 (at the beginning/middle/end, each for
        _PTS_SAMPLE_SECONDS seconds) and return the list of inter-frame intervals in
        microseconds. Segmented sampling covers sources that change rate mid-stream;
        invalid intervals (<=0 or >1s, including inter-segment gaps) are dropped, and
        None is returned if too few valid intervals remain.

        :param filepath: 视频文件路径 (Path to the video file)
        :return: 帧间隔微秒列表；探测失败或有效间隔不足时返回 None
                 (List of inter-frame intervals in us, or None on failure)
        """
        intervals = ",".join(f"{p}+{_PTS_SAMPLE_SECONDS}" for p in _PTS_SAMPLE_FRACTIONS)
        cmd = [
            self.ffprobe_path,
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
        result = self._run(cmd, timeout=60)
        if result is None:
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

    def _vote_common_rate(self, diffs_us):
        """
        对常见帧率表加权投票：每个帧间隔给偏差 ≤0.5% 的表项加分，
        权重 = 1 - 偏差/容差（越接近得分越高）。
        时间戳量化会把同一帧率网格拆成多个接近的间隔（如 90kHz tick 下的
        33317/33333/33350us），逐桶取众数占比可能虚低；加权投票按"接近度"
        聚合，天然免疫拆桶，且接近 29.97 与 30 之间的间隔（33350us 这类中点）
        不会因平局误判。返回 (帧率分数, 得分占比)，占比不足返回 None。

        Weighed vote over the common frame-rate table: each interval adds score to
        table entries within 0.5% deviation, weighted by 1 - dev/tolerance (closer
        scores higher). Timestamp quantization splits one nominal rate into several
        close intervals (e.g. 33317/33333/33350us on a 90kHz tick), so a per-bucket
        mode would under-report share; the weighted vote aggregates by proximity and
        is robust to such splitting, and midpoints like 33350us between 29.97 and 30
        are not misjudged by a tie. Returns (rate_fraction, score share); None if the
        top share is too low.
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

    def _normalize_common_rate(self, fps_str):
        """
        把与常见帧率表最接近且偏差 ≤0.05% 的帧率归一化为表值
        （如 "73743750/2460583"≈29.97 → "30000/1001"），其余原样返回。
        容差 0.05% 小于 30 与 29.97 的间距（0.1%），不会互相污染。

        Normalize a frame-rate string to the nearest common table value when the
        relative deviation is <=0.05% (e.g. "73743750/2460583"≈29.97 →
        "30000/1001"), otherwise return the original string unchanged. The 0.05%
        tolerance is smaller than the 0.1% gap between 30 and 29.97, so the two
        never contaminate each other.

        :param fps_str: ffprobe 返回的帧率字符串 (Frame-rate string from ffprobe)
        :return: 归一化后的常见帧率分数，或原字符串
                 (Normalized common fraction, or the original string)
        """
        f = self.fps_fraction_to_float(fps_str)
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


# ---------------------------------------------------------------------------
# 兼容层：模块级函数薄包装，签名与旧版一致，测试与既有调用方无需改动。
# fps_fraction_to_float 仅测试使用，已删除，测试改为直接调用
# VideoProbe.fps_fraction_to_float 静态方法。
# Backward-compatibility shims: thin module-level wrappers keeping the legacy
# signatures so existing callers need no changes. fps_fraction_to_float was
# only used by tests and was removed; tests now call the static method
# VideoProbe.fps_fraction_to_float directly.
# ---------------------------------------------------------------------------

def get_video_codec(filepath, ffprobe_path):
    """探测视频流的编码名称（如 h264/hevc/av1）。见 VideoProbe.get_codec。"""
    return VideoProbe(ffprobe_path).get_codec(filepath)


def get_video_pix_fmt(filepath, ffprobe_path):
    """探测视频流的像素格式（如 yuv420p / yuv420p10le）。见 VideoProbe.get_pix_fmt。"""
    return VideoProbe(ffprobe_path).get_pix_fmt(filepath)


def get_video_frame_rate(filepath, ffprobe_path):
    """探测视频流帧率（如 "30000/1001"）。见 VideoProbe.get_frame_rate。"""
    return VideoProbe(ffprobe_path).get_frame_rate(filepath)


def get_streams_info(filepath, ffprobe_path):
    """探测所有流与容器格式信息。见 VideoProbe.get_streams_info。"""
    return VideoProbe(ffprobe_path).get_streams_info(filepath)

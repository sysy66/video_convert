"""
探测模块。封装 ffprobe 调用，获取视频的编码、像素格式、帧率、帧间隔等元数据，
并对帧率进行归一化/投票以处理 VFR 源。

Probing module. Wraps ffprobe to obtain video metadata (codec, pixel format,
frame rate, frame intervals, etc.) and normalizes/votes the frame rate to handle
VFR sources.
"""

import json
import subprocess

from src import config

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

#: avg_frame_rate 与 r_frame_rate 相对差不超过该值即视为一致（0.05%，即相邻
#: 帧率档位间距 0.1% 的一半）：吸收 avg 计算噪声（nb_frames/duration 的毫级
#: 抖动），又不吞 29.97/30、23.976/24 这类真实差异（间距恰 0.1%）。
#: _normalize_common_rate 的归一化容差复用同一值。
_RATE_AGREE_TOLERANCE = 0.0005
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

    def get_frame_count(self, filepath):
        """
        视频流帧数：优先读容器 moov 索引的 nb_frames（秒级，mp4/mov 类），
        并用 duration×avg_frame_rate 交叉校验（偏差 >1% 视为索引虚高，如
        文件尾损坏/流式未 finalize，自动降级兜底计数）；mkv/webm 无
        nb_frames 字段或校验失败时回退 ffprobe -count_packets 读包计数
        （不解码只数 demux 包，mkv 帧级容器 1 包=1 帧，NFS 实测 2.6 GB
        约 12 秒；mp4/mkv/webm 均适用，TS 等切片流不适用）。计数失败返回 None。

        Video-stream frame count: prefer the container moov index nb_frames
        (instant on mp4/mov), cross-checked against duration×avg_frame_rate
        (deviation >1% means a bloated index, e.g. truncated tail or an
        unfinalized stream, and falls back to counting); mkv/webm have no
        nb_frames field, and any check failure falls back to ffprobe
        -count_packets (demux-level packet counting without decode; on
        frame-based containers like mkv/mp4 one packet is one video frame,
        measured ~12 s for a 2.6 GB file on NFS; not applicable to sliced
        streams like MPEG-TS). Returns None on failure.

        :param filepath: 视频文件路径 (Path to the video file)
        :return: 帧数整数，失败时返回 None (int frame count, or None on failure)
        """
        fast = self._nb_frames_checked(filepath)
        if fast is not None:
            return fast
        cmd = [
            self.ffprobe_path,
            "-v",
            "error",
            "-count_packets",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_packets",
            "-of",
            "csv=p=0",
            str(filepath),
        ]
        result = self._run(cmd, timeout=config.FRAME_COUNT_TIMEOUT)
        if result is None:
            return None
        text = result.stdout.strip()
        if not text or text == "N/A":
            return None
        try:
            return int(text)
        except ValueError:
            log.error("count_packets parse error for %s: %s", filepath, text)
            return None

    def _nb_frames_checked(self, filepath):
        """
        nb_frames 快路径：读容器记录的帧索引（秒级，mp4/mov 类；mkv/webm
        无此字段 → N/A），并用 duration×帧率交叉校验：avg_frame_rate 可用时
        偏差 >1% 视为索引虚高（文件尾损坏/流式未 finalize）降级全量计数，
        avg 缺失时用 r_frame_rate 宽松校验（>5% 降级）；校验都缺则直接信任
        nb_frames。任何一步失败返回 None（交给 count_frames 兜底）。

        nb_frames fast path: read the container's recorded frame index
        (instant on mp4/mov; mkv/webm lack the field -> N/A) and cross-check it
        against duration×rate: with avg_frame_rate available a deviation >1%
        means a bloated index (truncated tail / unfinalized stream) and falls
        back to full counting; with only r_frame_rate a looser >5% check is
        used; with no rate at all the index is trusted as-is. Returns None on
        any failure (leaving the count_frames fallback).
        """
        cmd = [
            self.ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_frames,duration,avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            str(filepath),
        ]
        result = self._run(cmd)
        if result is None:
            return None
        # 用 JSON 而非 CSV 位置解析：ffprobe 的多字段输出顺序由内部决定
        # （不同版本实测 r/duration 等位置并不固定），按键名取值才稳定。
        # JSON (not positional CSV) parsing: ffprobe decides the field order
        # internally, so lookups by key are the stable choice.
        try:
            streams = json.loads(result.stdout).get("streams") or []
        except (json.JSONDecodeError, AttributeError):
            return None
        if not streams:
            return None
        entry = streams[0]
        nb_s = str(entry.get("nb_frames") or "").strip()
        dur_s = str(entry.get("duration") or "").strip()
        avg_s = str(entry.get("avg_frame_rate") or "").strip()
        r_s = str(entry.get("r_frame_rate") or "").strip()
        if nb_s in ("", "N/A"):
            return None
        try:
            nb = int(nb_s)
        except ValueError:
            return None
        if nb <= 0:
            return None
        try:
            duration = float(dur_s)
        except ValueError:
            duration = None
        avg_f = self.fps_fraction_to_float(avg_s)
        if duration and avg_f and duration > 0:
            est = duration * avg_f
            if abs(nb - est) / est > 0.01:
                log.warning(
                    "nb_frames 与 duration×avg_frame_rate 偏差过大 %s"
                    " (nb=%d est=%.0f)，回退全量计数",
                    filepath,
                    nb,
                    est,
                )
                return None
        elif duration and not avg_f:
            r_f = self.fps_fraction_to_float(r_s)
            if r_f and duration > 0:
                est = duration * r_f
                if abs(nb - est) / est > 0.05:
                    log.warning(
                        "nb_frames 与 duration×r_frame_rate 偏差过大 %s"
                        " (nb=%d est=%.0f)，回退全量计数",
                        filepath,
                        nb,
                        est,
                    )
                    return None
        log.info("nb_frames fast path %s: %d", filepath, nb)
        return nb

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


def get_streams_info(filepath, ffprobe_path):
    """探测所有流与容器格式信息。见 VideoProbe.get_streams_info。"""
    return VideoProbe(ffprobe_path).get_streams_info(filepath)


def get_frame_count(filepath, ffprobe_path):
    """全量解码计数视频流帧数。见 VideoProbe.get_frame_count。"""
    return VideoProbe(ffprobe_path).get_frame_count(filepath)

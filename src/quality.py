"""
视频质量评估模块。使用 ffmpeg libvmaf 滤镜计算 VMAF 分数。
Video quality assessment module. Uses ffmpeg libvmaf filter to compute VMAF scores.
"""

import json
import os
import re
import subprocess

from src import config

log = __import__("logging").getLogger(__name__)

_VMAF_RE = re.compile(r"VMAF score:\s*([\d.]+)")


def _probe_avg_fps(path, ffprobe_path):
    """
    探测视频第一路视频流的平均帧率（avg_frame_rate），用于 VMAF 帧率对齐。
    Probe the first video stream's average frame rate for VMAF frame alignment.

    :param path: 视频文件路径 (Path to the video file)
    :param ffprobe_path: ffprobe 可执行文件路径 (Path to ffprobe)
    :return: 帧率 float，无法探测时返回 None (Frame rate as float, or None)
    """
    if not ffprobe_path:
        return None
    cmd = [
        ffprobe_path, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
        if "/" in out:
            num, den = out.split("/", 1)
            num, den = float(num), float(den)
            if num > 0 and den > 0:
                return num / den
        elif out:
            v = float(out)
            if v > 0:
                return v
    except Exception:
        return None
    return None


def _read_vmaf_from_json(log_path):
    """
    从 libvmaf 输出的 json 日志中读取聚合平均分（pooled_metrics.vmaf.mean）。
    这比正则抓取 stderr 更稳定。
    Read the aggregate mean score from a libvmaf json log; more reliable than
    scraping stderr.

    :param log_path: json 日志路径 (Path to the json log)
    :return: (mean, minimum) 元组，读取失败时返回 (None, None)
             (mean, minimum) tuple, or (None, None) on failure
    """
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        vmaf = data.get("pooled_metrics", {}).get("vmaf", {})
        mean = vmaf.get("mean")
        minimum = vmaf.get("min")
        return (float(mean) if mean is not None else None,
                float(minimum) if minimum is not None else None)
    except Exception:
        return (None, None)


def compute_vmaf(original_path, transcoded_path, ffmpeg_path=None, ffprobe_path=None, log_path=None):
    """
    计算转码前后视频的 VMAF 分数。
    使用显式 VMAF 模型、scale2ref 分辨率对齐与 fps 帧率对齐，避免参考/失真错帧比较
    导致分数虚低。若传入 log_path，则输出逐帧 json 日志用于事后审计。

    Compute the VMAF score between original and transcoded video.
    Uses an explicit VMAF model, scale2ref resolution alignment, and fps frame-rate
    alignment to avoid mismatched-frame comparisons that spuriously deflate the score.
    If log_path is given, a per-frame json log is written for later auditing.

    :param original_path: 原始视频路径（参考）(Path to the original video, reference)
    :param transcoded_path: 转码后视频路径（失真）(Path to the transcoded video, distorted)
    :param ffmpeg_path: 支持 libvmaf 的 ffmpeg 路径，默认从 config 获取
                        (Path to ffmpeg with libvmaf, defaults to config)
    :param ffprobe_path: ffprobe 路径，用于探测帧率，默认从 config 获取
                         (Path to ffprobe for frame-rate probing, defaults to config)
    :param log_path: json 日志输出路径，None 表示不输出
                     (Path for json log output, None to skip)
    :return: VMAF 分数 (0-100)，失败或不可用时返回 None
             (VMAF score 0-100, or None on failure/unavailability)
    """
    ffmpeg_path = ffmpeg_path or config.VMAF_FFMPEG_PATH
    ffprobe_path = ffprobe_path or config.FFPROBE_PATH

    if not ffmpeg_path:
        log.warning("VMAF not configured, skipping quality check")
        return None

    # 帧率对齐：以源帧率为准，用输入选项 -r 把两路在解复用层重定时到同一 CFR，
    # 保证 libvmaf 逐帧对应。不用 fps 滤镜——该静态 ffmpeg build 在多输入
    # filter_complex 中使用 fps 滤镜会触发调度器 assertion 崩溃。
    # Frame-rate alignment: retime both inputs to the same CFR via the -r input
    # option (demuxer level) so libvmaf compares corresponding frames. The fps
    # filter is avoided—this static ffmpeg build crashes (scheduler assertion)
    # when fps is used inside a multi-input filter_complex.
    fps = _probe_avg_fps(original_path, ffprobe_path)
    rate_opt = ["-r", "{:.6f}".format(fps)] if fps else []

    # 输入 0 = 原始(参考 ref)，输入 1 = 转码(失真 dist)。
    # scale2ref 把失真流对齐到参考分辨率；两路统一到 yuv420p。
    # Input 0 = original (ref), input 1 = transcoded (dist). scale2ref aligns the
    # distorted stream to the reference resolution; both normalised to yuv420p.
    filter_complex = (
        "[1:v]setpts=PTS-STARTPTS[d0];"
        "[0:v]setpts=PTS-STARTPTS[r0];"
        "[d0][r0]scale2ref=flags=bicubic[d1][r1];"
        "[d1]format=yuv420p[dist];"
        "[r1]format=yuv420p[ref];"
        "[dist][ref]libvmaf={opts}"
    )

    opts = []
    # 显式指定 VMAF 模型（存在时），避免不同 ffmpeg 内置模型差异导致分数偏移。
    # Explicit VMAF model (when present) to avoid score drift across builds.
    if config.VMAF_MODEL_PATH and os.path.exists(config.VMAF_MODEL_PATH):
        opts.append("model='path={}'".format(config.VMAF_MODEL_PATH))
    opts.append("n_subsample={}".format(config.VMAF_SUBSAMPLE))

    if log_path is not None:
        try:
            os.makedirs(os.path.dirname(str(log_path)), exist_ok=True)
            opts.append("log_fmt=json")
            opts.append("log_path={}".format(log_path))
        except OSError as e:
            log.warning("VMAF log dir not writable, skipping json log: %s", e)
            log_path = None

    cmd = (
        [ffmpeg_path]
        + rate_opt + ["-i", str(original_path)]
        + rate_opt + ["-i", str(transcoded_path)]
        + ["-filter_complex", filter_complex.format(opts=":".join(opts)),
           "-an", "-f", "null", "-"]
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        # 优先从 json 日志读取聚合均值（更稳定），回退到 stderr 正则。
        # Prefer the aggregate mean from the json log; fall back to stderr regex.
        if log_path is not None:
            mean, _minimum = _read_vmaf_from_json(log_path)
            if mean is not None:
                return mean

        output = result.stderr + result.stdout
        m = _VMAF_RE.search(output)
        if m:
            return float(m.group(1))

        log.warning("VMAF score not found in ffmpeg output for %s", transcoded_path.name)
        return None
    except subprocess.TimeoutExpired:
        log.warning("VMAF timed out for %s", transcoded_path.name)
        return None
    except Exception as e:
        log.warning("VMAF failed for %s: %s", transcoded_path.name, e)
        return None


def should_accept(vmaf_score, ratio):
    """
    判断是否接受转码结果。规则：
      - VMAF 不可用 → 拒绝
      - VMAF ≥ VMAF_ACCEPT_HIGH → ratio ≤ ACCEPT_RATIO_MAX 才接受
      - VMAF < VMAF_ACCEPT_LOW → 拒绝
      - 中间区间 → ratio ≤ ACCEPT_RATIO_LOW（缩小 ≥25%）才接受

    Decide whether to accept the transcoded result. Rules:
      - VMAF unavailable → reject
      - VMAF ≥ VMAF_ACCEPT_HIGH → accept only if ratio ≤ ACCEPT_RATIO_MAX
      - VMAF < VMAF_ACCEPT_LOW → reject
      - Middle range → accept only if ratio ≤ ACCEPT_RATIO_LOW (≥25% smaller)

    :param vmaf_score: VMAF 分数 (VMAF score, 0-100 or None)
    :param ratio: 输出/输入文件大小比 (output/input size ratio)
    :return: True 接受 (accept), False 拒绝 (reject)
    """
    if vmaf_score is None:
        return False
    if vmaf_score >= config.VMAF_ACCEPT_HIGH:
        return ratio <= config.ACCEPT_RATIO_MAX
    if vmaf_score < config.VMAF_ACCEPT_LOW:
        return False
    return ratio <= config.ACCEPT_RATIO_LOW

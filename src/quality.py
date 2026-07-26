"""
视频质量评估模块。使用 ffmpeg libvmaf 滤镜计算 VMAF 分数。
Video quality assessment module. Uses ffmpeg libvmaf filter to compute VMAF scores.
"""

import json
import os
import re
import subprocess
import time

from src import config

log = __import__("logging").getLogger(__name__)

_VMAF_RE = re.compile(r"VMAF score:\s*([\d.]+)")


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


def compute_vmaf(original_path, transcoded_path, ffmpeg_path=None, log_path=None):
    """
    计算转码前后视频的 VMAF 分数。
    使用显式 VMAF 模型、scale2ref 分辨率对齐，避免参考/失真分辨率不匹配导致分数虚低。
    若传入 log_path，则输出逐帧 json 日志用于事后审计。

    Compute the VMAF score between original and transcoded video.
    Uses an explicit VMAF model and scale2ref resolution alignment to avoid
    mismatched-resolution comparisons that spuriously deflate the score.
    If log_path is given, a per-frame json log is written for later auditing.

    :param original_path: 原始视频路径（参考）(Path to the original video, reference)
    :param transcoded_path: 转码后视频路径（失真）(Path to the transcoded video, distorted)
    :param ffmpeg_path: 支持 libvmaf 的 ffmpeg 路径，默认从 config 获取
                        (Path to ffmpeg with libvmaf, defaults to config)
    :param log_path: json 日志输出路径，None 表示不输出
                     (Path for json log output, None to skip)
    :return: VMAF 分数 (0-100)，失败或不可用时返回 None
             (VMAF score 0-100, or None on failure/unavailability)
    """
    ffmpeg_path = ffmpeg_path or config.VMAF_FFMPEG_PATH

    if not ffmpeg_path:
        log.warning("VMAF not configured, skipping quality check")
        return None

    filter_complex = (
        "[1:v]setpts=PTS-STARTPTS[d0];"
        "[0:v]setpts=PTS-STARTPTS[r0];"
        "[d0][r0]scale2ref=flags=bicubic[d1][r1];"
        "[d1]format=yuv420p[dist];"
        "[r1]format=yuv420p[ref];"
        "[dist][ref]libvmaf={opts}"
    )

    opts = []
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
        [ffmpeg_path, "-hwaccel", "qsv", "-hwaccel_output_format", "none"]
        + ["-i", str(original_path)]
        + ["-i", str(transcoded_path)]
        + ["-filter_complex", filter_complex.format(opts=":".join(opts)),
           "-an", "-f", "null", "-"]
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=config.VMAF_TIMEOUT,
        )
        # 优先从 json 日志读取聚合均值（更稳定），回退到 stderr 正则。
        # Prefer the aggregate mean from the json log; fall back to stderr regex.
        if log_path is not None:
            mean, _minimum = _read_vmaf_from_json(log_path)
            if mean is not None:
                return mean

        output = (result.stderr + result.stdout).decode("utf-8", errors="replace")
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


def cleanup_vmaf_logs():
    """
    删除 data/vmaf_logs/ 中超过 VMAF_LOG_RETENTION_DAYS 天的 json 日志。
    Delete VMAF json logs older than VMAF_LOG_RETENTION_DAYS from data/vmaf_logs/.

    :return: 删除的文件数量 (Number of deleted files)
    """
    log_dir = config.VMAF_LOG_DIR
    if not os.path.isdir(log_dir):
        return 0
    cutoff = time.time() - config.VMAF_LOG_RETENTION_DAYS * 86400
    count = 0
    for entry in os.listdir(log_dir):
        if not entry.endswith(".json"):
            continue
        path = os.path.join(log_dir, entry)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                count += 1
        except OSError:
            pass
    if count:
        log.info("Cleaned %d expired VMAF log file(s)", count)
    return count


def should_accept(vmaf_score, ratio):
    """
    判断是否接受转码结果。规则：
      - VMAF 不可用 → 取决于 VMAF_REQUIRED 配置（默认拒绝）
      - VMAF ≥ VMAF_ACCEPT_HIGH → ratio ≤ ACCEPT_RATIO_MAX 才接受
      - VMAF < VMAF_ACCEPT_LOW → 拒绝
      - 中间区间 → ratio ≤ ACCEPT_RATIO_LOW（缩小 ≥25%）才接受

    Decide whether to accept the transcoded result. Rules:
      - VMAF unavailable → depends on VMAF_REQUIRED (default reject)
      - VMAF ≥ VMAF_ACCEPT_HIGH → accept only if ratio ≤ ACCEPT_RATIO_MAX
      - VMAF < VMAF_ACCEPT_LOW → reject
      - Middle range → accept only if ratio ≤ ACCEPT_RATIO_LOW (≥25% smaller)

    :param vmaf_score: VMAF 分数 (VMAF score, 0-100 or None)
    :param ratio: 输出/输入文件大小比 (output/input size ratio)
    :return: True 接受 (accept), False 拒绝 (reject)
    """
    if vmaf_score is None:
        if config.VMAF_REQUIRED:
            return False
        return ratio <= config.ACCEPT_RATIO_MAX
    if vmaf_score >= config.VMAF_ACCEPT_HIGH:
        return ratio <= config.ACCEPT_RATIO_MAX
    if vmaf_score < config.VMAF_ACCEPT_LOW:
        return False
    return ratio <= config.ACCEPT_RATIO_LOW

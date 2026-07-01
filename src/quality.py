"""
视频质量评估模块。使用 ffmpeg libvmaf 滤镜计算 VMAF 分数。
Video quality assessment module. Uses ffmpeg libvmaf filter to compute VMAF scores.
"""

import re
import subprocess

from src import config

log = __import__("logging").getLogger(__name__)

_VMAF_RE = re.compile(r"VMAF score:\s*([\d.]+)")


def compute_vmaf(original_path, transcoded_path, ffmpeg_path=None):
    """
    计算转码前后视频的 VMAF 分数。
    Compute VMAF score between original and transcoded video.

    :param original_path: 原始视频路径 (Path to the original video)
    :param transcoded_path: 转码后视频路径 (Path to the transcoded video)
    :param ffmpeg_path: 支持 libvmaf 的 ffmpeg 路径，默认从 config 获取
                        (Path to ffmpeg with libvmaf, defaults to config)
    :return: VMAF 分数 (0-100)，失败或不可用时返回 None
             (VMAF score 0-100, or None on failure/unavailability)
    """
    ffmpeg_path = ffmpeg_path or config.VMAF_FFMPEG_PATH

    if not ffmpeg_path:
        log.warning("VMAF not configured, skipping quality check")
        return None

    vmaf_opts = ["log_fmt=json", "n_subsample={}".format(config.VMAF_SUBSAMPLE)]

    cmd = [
        ffmpeg_path,
        "-i", str(original_path),
        "-i", str(transcoded_path),
        "-filter_complex",
        "[0:v]setpts=PTS-STARTPTS,format=yuv420p[ref];"
        "[1:v]setpts=PTS-STARTPTS,scale=iw:ih:flags=bicubic,format=yuv420p[dist];"
        "[dist][ref]libvmaf={}".format(":".join(vmaf_opts)),
        "-an", "-f", "null", "-",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        output = result.stderr + result.stdout
        m = _VMAF_RE.search(output)
        if m:
            score = float(m.group(1))
            log.info("VMAF: %s - %.2f", transcoded_path.name, score)
            return score
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
      - VMAF ≥ 93 → 接受
      - VMAF < 85 → 拒绝
      - 85 ≤ VMAF < 93 → ratio ≤ 0.75（缩小 ≥25%）才接受

    Decide whether to accept the transcoded result. Rules:
      - VMAF unavailable → reject
      - VMAF ≥ 93 → accept
      - VMAF < 85 → reject
      - 85 ≤ VMAF < 93 → accept only if ratio ≤ 0.75 (≥25% smaller)

    :param vmaf_score: VMAF 分数 (VMAF score, 0-100 or None)
    :param ratio: 输出/输入文件大小比 (output/input size ratio)
    :return: True 接受 (accept), False 拒绝 (reject)
    """
    if vmaf_score is None:
        return False
    if vmaf_score >= 93:
        return True
    if vmaf_score < 85:
        return False
    return ratio <= 0.75

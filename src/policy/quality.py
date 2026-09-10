"""
视频质量评估模块。使用 ffmpeg libvmaf 滤镜计算 VMAF 分数。
Video quality assessment module. Uses ffmpeg libvmaf filter to compute VMAF scores.
"""

import os
import time

from src import config

log = __import__("logging").getLogger(__name__)


def cleanup_vmaf_logs():
    """
    删除 VMAF_LOG_DIR 中超过 VMAF_LOG_RETENTION_DAYS 天的 json 日志。
    Delete VMAF json logs older than VMAF_LOG_RETENTION_DAYS from VMAF_LOG_DIR.

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


def vmaf_verdict(vmaf_score, ratio):
    """
    统一 sync/async 打分后决策。规则：
      - vmaf_score 为 None → "reject"（打分失败同样拒绝，保留源文件；
        关停中断的打分由调用方单独豁免，不走 verdict）
      - 其余 → 按 should_accept 给出 "accept"/"reject"
        （ratio 超标与极端低分均被 should_accept 覆盖为拒绝）

    Unified post-scoring verdict for sync and async paths. Rules:
      - vmaf_score is None → "reject" (scoring failure is also rejected,
        keeping the source; shutdown-interrupted scoring is exempted by the
        caller, not via the verdict)
      - otherwise → should_accept decides "accept"/"reject"
        (over-ratio and extreme-low scores are covered as rejections)

    :param vmaf_score: VMAF 分数 (VMAF score, 0-100 or None)
    :param ratio: 输出/输入文件大小比 (output/input size ratio)
    :return: "accept" | "reject"
    """
    if vmaf_score is None:
        return "reject"
    return "accept" if should_accept(vmaf_score, ratio) else "reject"


def should_accept(vmaf_score, ratio):
    """
    判断是否接受转码结果（vmaf_verdict 已保证传入的 vmaf_score 非 None）。规则：
      - VMAF ≥ VMAF_ACCEPT_HIGH → ratio ≤ ACCEPT_RATIO_MAX 才接受
      - VMAF < VMAF_ACCEPT_LOW → 拒绝
      - 中间区间 → ratio ≤ ACCEPT_RATIO_LOW（缩小 ≥25%）才接受

    Decide whether to accept the transcoded result (vmaf_score is non-None here). Rules:
      - VMAF ≥ VMAF_ACCEPT_HIGH → accept only if ratio ≤ ACCEPT_RATIO_MAX
      - VMAF < VMAF_ACCEPT_LOW → reject
      - Middle range → accept only if ratio ≤ ACCEPT_RATIO_LOW (≥25% smaller)

    :param vmaf_score: VMAF 分数 (VMAF score, 0-100)
    :param ratio: 输出/输入文件大小比 (output/input size ratio)
    :return: True 接受 (accept), False 拒绝 (reject)
    """
    if vmaf_score >= config.VMAF_ACCEPT_HIGH:
        return ratio <= config.ACCEPT_RATIO_MAX
    if vmaf_score < config.VMAF_ACCEPT_LOW:
        return False
    return ratio <= config.ACCEPT_RATIO_LOW

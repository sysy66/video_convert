"""
视频质量评估模块。使用 ffmpeg libvmaf 滤镜计算 VMAF 分数。
Video quality assessment module. Uses ffmpeg libvmaf filter to compute VMAF scores.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from src import config
from src.probe import get_video_pix_fmt

log = __import__("logging").getLogger(__name__)


def _hw_download_format(filepath, ffprobe_path):
    """
    探测视频真实 pix_fmt，决定 hwdownload 应该按 8bit(nv12) 还是 10bit(p010le) 取帧。
    不能假定输入位深——新番生肉常见 10bit x265，老番/自制转码常见 8bit。
    探测失败时默认按 8bit 处理：猜错的后果只是 hwdownload 报错安全回退到软件路径，
    不会产生错误分数。

    Probe the real pix_fmt to decide whether hwdownload should request 8bit(nv12)
    or 10bit(p010le). Never assume bit depth — 10bit x265 raw releases are common.
    Defaults to 8bit on probe failure: a wrong guess only makes hwdownload fail
    and fall back to software, never a corrupted score.
    """
    pix_fmt = get_video_pix_fmt(filepath, ffprobe_path) or ""
    if "10le" in pix_fmt or "10be" in pix_fmt or pix_fmt == "p010le":
        return "p010le"
    return "nv12"


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
        with open(log_path, encoding="utf-8") as f:
            data = json.load(f)
        vmaf = data.get("pooled_metrics", {}).get("vmaf", {})
        mean = vmaf.get("mean")
        minimum = vmaf.get("min")
        return (
            float(mean) if mean is not None else None,
            float(minimum) if minimum is not None else None,
        )
    except Exception:
        return (None, None)


def _frame_index_align_string():
    """
    构建帧序对齐 setpts 字符串：把每路的帧时间戳按帧序号线性化
    （setpts=N/30/TB，参考帧率 30 仅用于归一化、数值任意），使得两路第 n 帧
    时间戳相等、逐帧配对对比，与源/产物的容器 tbr 声明、真实帧率完全无关。
    2026-08-13 全片实验：v1 产物（帧序保留）用该方式打分 92.09，
    而旧的 fps=30/1 网格投影打分只有 62.37（VFR 帧不落网格导致的假低分）。

    Build the frame-index alignment setpts string: linearizes each stream's
    frame timestamps by frame index (setpts=N/30/TB; the 30fps reference is only
    for normalization and its value is arbitrary), so frame n of both inputs
    gets identical timestamps and is compared pairwise, independent of the
    container tbr declaration and true frame rates. From the 2026-08-13 full
    full-movie experiment the v1 output (frame order preserved) scores 92.09 with
    this alignment while the old fps=30/1 grid projection scored only 62.37
    (VFR frames not on the grid were falsely downgraded).
    """
    return "setpts=N/30/TB"


def _frame_counts_equal(ref_frame_count, dist_frame_count):
    """
    比较两路已知帧数是否可视为相等（相对差 ≤1%）。帧序对齐按帧序号
    逐帧配对，产物是源逐帧重编码时只要帧序一致，前 min(ref,dist) 帧
    永远逐帧对应，尾部多出帧被截断（fqm 同款语义），因此小差异放行
    帧序对齐无损；阈值与 probe 侧"索引虚高 >1% 判损坏"一致，差异
    过大（如流式未 finalize 的 20% 虚高）按不相等处理，回退 fps 网格
    投影作为源异常预警。任一路帧数不可得（None）按不相等处理。

    Compare whether the two known frame counts are close enough for
    frame-index alignment (relative difference <=1%). Since the distorted
    clip is a per-frame re-encode of the reference, pairing frames by index
    stays correct for the first min(ref,dist) frames regardless of a small
    tail difference (the tail is truncated, same semantics as fqm);
    the 1% threshold matches the "index bloated >1%" corruption check on the
    probe side, and a large gap (e.g. a 20% bloated index from an unfinalized
    stream) is treated as unequal, falling back to fps-grid projection as an
    anomaly warning. Missing counts (None) are treated as unequal.
    """
    if ref_frame_count is None or dist_frame_count is None:
        log.warning(
            "VMAF frame count unavailable (ref=%s dist=%s), falling back to fps grid projection",
            ref_frame_count,
            dist_frame_count,
        )
        return False
    larger = max(ref_frame_count, dist_frame_count)
    if larger and abs(ref_frame_count - dist_frame_count) / larger > 0.01:
        log.warning(
            "VMAF frame count mismatch ref=%d dist=%d, falling back to fps grid projection",
            ref_frame_count,
            dist_frame_count,
        )
        return False
    log.info(
        "VMAF frame counts equal (ref=%d dist=%d), using frame-index alignment",
        ref_frame_count,
        dist_frame_count,
    )
    return True


def _run_vmaf(
    original_path,
    transcoded_path,
    ffmpeg_path,
    log_path,
    hwaccel=False,
    ffprobe_path=None,
    ref_frame_count=None,
    dist_frame_count=None,
):
    """
    内部：构建并执行单次 ffmpeg VMAF 命令。
    对齐策略：两路帧数相对差 ≤1%（_frame_counts_equal）时用 setpts=N/30/TB
    把帧时间戳按帧序号线性化，第 n 帧与第 n 帧逐帧配对——不依赖任何 tbr
    声明/帧率网格，VFR 源不再产生假低分（实测 62.37→92.09）。
    帧数不可得或差异过大时视为内容不可信，直接返回 None 不下分（不再做
    fps 网格投影兜底——投影重采样改帧，异常源的分数无意义）。
    HW 模式下 hwdownload 按每路各自探测到的真实原生位深取帧（8bit → nv12，
    10bit → p010le），全程不经过 vpp_qsv 等有损转换，分数与纯软件路径逐位一致。
    若某路源编码 QSV 无法硬解（如老番 avi 的 mpeg4），hwdownload 会直接失败
    （不会静默降级成软解再上传），从而安全回退到软件路径。

    :param hwaccel: True 时启用 QSV 硬件解码（hwdownload 后接软件 libvmaf）
    :param ref_frame_count: 参考帧数（调用方探测，None 视为不可得不下分）
    :param dist_frame_count: 失真帧数（转码 stderr 解析/探测，None 不下分）
    """
    # 对齐链：帧数相对差 ≤1% → 帧序对齐（setpts=N/30/TB，与 tbr 无关）；
    # 否则内容不可信，不下分。
    # Alignment: frame counts within 1% -> frame-index alignment
    # (setpts=N/30/TB, independent of tbr); otherwise the content is
    # untrustworthy and no score is produced.
    _align = _frame_index_align_string()
    if not _frame_counts_equal(ref_frame_count, dist_frame_count):
        log.warning(
            "VMAF SKIPPED: frame alignment unavailable for %s, no score produced",
            os.path.basename(str(transcoded_path)),
        )
        return None

    if hwaccel:
        ffprobe_path = ffprobe_path or config.FFPROBE_PATH
        ref_fmt = _hw_download_format(original_path, ffprobe_path)
        dist_fmt = _hw_download_format(transcoded_path, ffprobe_path)
        hwaccel_flags = ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
        filter_complex = (
            "[0:v]hwdownload,format=" + ref_fmt + "[ref_dl];"
            "[ref_dl]" + _align + "[r0];"
            "[1:v]hwdownload,format=" + dist_fmt + "[dist_dl];"
            "[dist_dl]" + _align + "[d0];"
            "[d0][r0]scale2ref=flags=bicubic[d1][r1];"
            "[d1]format=yuv420p[dist];"
            "[r1]format=yuv420p[ref];"
            "[dist][ref]libvmaf={opts}"
        )
        input_parts = (
            hwaccel_flags
            + ["-i", str(original_path)]
            + hwaccel_flags
            + ["-i", str(transcoded_path)]
        )
    else:
        filter_complex = (
            "[1:v]" + _align + "[d0];"
            "[0:v]" + _align + "[r0];"
            "[d0][r0]scale2ref=flags=bicubic[d1][r1];"
            "[d1]format=yuv420p[dist];"
            "[r1]format=yuv420p[ref];"
            "[dist][ref]libvmaf={opts}"
        )
        input_parts = ["-i", str(original_path)] + ["-i", str(transcoded_path)]

    opts = []
    if config.VMAF_MODEL_PATH and os.path.exists(config.VMAF_MODEL_PATH):
        opts.append(f"model='path={config.VMAF_MODEL_PATH}'")
    opts.append(f"n_subsample={config.VMAF_SUBSAMPLE}")
    opts.append(f"n_threads={config.VMAF_N_THREADS or max(1, ((os.cpu_count() or 1) + 1) // 2)}")
    # framesync 语义与 fqm 对齐：shortest=1 以短流为准截断尾部、repeatlast=0
    # 尾部帧不足时不重复最后一帧（帧序对齐放行 ≤1% 差异时即依赖此截断配对）。
    # framesync semantics aligned with fqm: shortest=1 truncates at the shorter
    # stream, repeatlast=0 does not pad with the last frame (the frame-index
    # path relies on this when it permits a <=1% tail difference).
    opts.append("shortest=1:repeatlast=0")

    if log_path is not None:
        try:
            os.makedirs(os.path.dirname(str(log_path)), exist_ok=True)
            opts.append("log_fmt=json")
            opts.append(f"log_path={log_path}")
        except OSError as e:
            log.warning("VMAF log dir not writable, skipping json log: %s", e)
            log_path = None

    filter_complex_str = filter_complex.format(opts=":".join(opts))
    cmd = (
        [ffmpeg_path]
        + list(input_parts)
        + ["-filter_complex", filter_complex_str, "-an", "-f", "null", "-"]
    )

    tag = "HW" if hwaccel else "SW"

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=config.VMAF_TIMEOUT)
        if result.returncode != 0:
            log.warning(
                "VMAF [%s] failed (rc=%d) for %s",
                tag,
                result.returncode,
                os.path.basename(str(transcoded_path)),
            )
            return None

        # 分数唯一来源是 libvmaf json 日志的 pooled_metrics.vmaf.mean。
        # 该值由 libvmaf 聚合（libvmaf 3.2+ 在 subsample 模式下聚合正确，
        # 实测与逐帧均值一致），不再解析 stderr。
        # The only score source is pooled_metrics.vmaf.mean from the libvmaf
        # json log. Aggregation is done inside libvmaf (3.2+ aggregates correctly
        # under n_subsample; verified identical to a per-frame average), so
        # stderr is no longer parsed for scoring.
        if log_path is not None:
            mean, _minimum = _read_vmaf_from_json(log_path)
            if mean is not None:
                return mean

        log.warning(
            "VMAF [%s] score not found in json log for %s",
            tag,
            os.path.basename(str(transcoded_path)),
        )
        return None
    except subprocess.TimeoutExpired:
        log.warning("VMAF [%s] timed out for %s", tag, os.path.basename(str(transcoded_path)))
    except Exception as e:
        log.warning("VMAF [%s] failed for %s: %s", tag, os.path.basename(str(transcoded_path)), e)

    return None


def compute_vmaf(
    original_path,
    transcoded_path,
    ffmpeg_path=None,
    log_path=None,
    ref_frame_count=None,
    dist_frame_count=None,
):
    """
    计算转码前后视频的 VMAF 分数。
    使用 scale2ref 进行分辨率对齐，避免参考/失真分辨率不匹配导致分数虚低；
    配置了 VMAF_MODEL_PATH 时附加以显式 VMAF 模型打分。
    json 日志尽量落盘：未传 log_path 时自动写入 VMAF_LOG_DIR（供事后审计与取分）；
    日志目录不可写时跳过落盘（仅警告）。
    VMAF_HWACCEL=True 时优先使用 QSV 硬解加速；失败后按 VMAF_HW_FALLBACK 决定是否回退到软解。
    帧数不可得或差异过大（相对差 >1%，内容不可信）时不下分，返回 None。

    Compute the VMAF score between original and transcoded video.
    Uses scale2ref for resolution alignment to avoid mismatched-resolution
    comparisons that spuriously deflate the score; an explicit VMAF model is
    used when VMAF_MODEL_PATH is configured.
    A json log is written when possible: when log_path is omitted it defaults
    into VMAF_LOG_DIR (for auditing and scoring); it is skipped with a warning
    if the log directory is not writable.
    When VMAF_HWACCEL is enabled, tries QSV hardware decode first; falls back
    to software decode on failure based on VMAF_HW_FALLBACK.
    No score (None) is produced when the frame counts are missing or diverge
    by more than 1% (untrustworthy content).

    :param original_path: 原始视频路径（参考）(Path to the original video, reference)
    :param transcoded_path: 转码后视频路径（失真）(Path to the transcoded video, distorted)
    :param ffmpeg_path: 支持 libvmaf 的 ffmpeg 路径，默认从 config 获取
                        (Path to ffmpeg with libvmaf, defaults to config)
    :param log_path: json 日志输出路径，None 时自动生成到 VMAF_LOG_DIR
                     (Path for json log output, defaults to VMAF_LOG_DIR when None)
    :param ref_frame_count: 参考帧数（帧序对齐前提，None 时不下分）
                            (Reference frame count; None disables scoring)
    :param dist_frame_count: 失真帧数（帧序对齐前提，None 时不下分）
                             (Distorted frame count; None disables scoring)
    :return: VMAF 分数 (0-100)，失败或不可用时返回 None
             (VMAF score 0-100, or None on failure/unavailability)
    """
    ffmpeg_path = ffmpeg_path or config.VMAF_FFMPEG_PATH

    if not ffmpeg_path:
        log.warning("VMAF not configured, skipping quality check")
        return None

    if log_path is None:
        safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(original_path).stem)[:80]
        log_path = Path(config.VMAF_LOG_DIR) / f"{safe_stem}.{time.strftime('%Y%m%d-%H%M%S')}.json"

    if config.VMAF_HWACCEL:
        score = _run_vmaf(
            original_path,
            transcoded_path,
            ffmpeg_path,
            log_path,
            hwaccel=True,
            ffprobe_path=config.FFPROBE_PATH,
            ref_frame_count=ref_frame_count,
            dist_frame_count=dist_frame_count,
        )
        if score is not None:
            return score
        if config.VMAF_HW_FALLBACK:
            log.info("VMAF HW attempt failed, falling back to software decode")
        else:
            return None

    return _run_vmaf(
        original_path,
        transcoded_path,
        ffmpeg_path,
        log_path,
        hwaccel=False,
        ffprobe_path=config.FFPROBE_PATH,
        ref_frame_count=ref_frame_count,
        dist_frame_count=dist_frame_count,
    )


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
      - ratio > ACCEPT_RATIO_MAX → "reject"（超标必拒，两端一致）
      - vmaf_score 为 None → "retry"（打分失败，记 failed 永久排除，仅 --retry-failed 重试）
      - vmaf_score < VMAF_EXTREME_LOW → "retry"（极端低分视为输入/产物异常，同样处理）
      - 其余 → 按 should_accept 给出 "accept"/"reject"

    Unified post-scoring verdict for sync and async paths. Rules:
      - ratio > ACCEPT_RATIO_MAX → "reject" (must reject, same for both modes)
      - vmaf_score is None → "retry" (scoring failed; mark failed, retried only via --retry-failed)
      - vmaf_score < VMAF_EXTREME_LOW → "retry" (extreme low suggests incomplete input)
      - otherwise → should_accept decides "accept"/"reject"

    :param vmaf_score: VMAF 分数 (VMAF score, 0-100 or None)
    :param ratio: 输出/输入文件大小比 (output/input size ratio)
    :return: "accept" | "reject" | "retry"
    """
    if ratio > config.ACCEPT_RATIO_MAX:
        return "reject"
    if vmaf_score is None:
        return "retry"
    if vmaf_score < config.VMAF_EXTREME_LOW:
        return "retry"
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

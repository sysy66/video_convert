import os

# 多库支持：名称 → {input, output, cache} 映射。
# Multi-library support: name → {input, output, cache} mapping.
#
# 简单字符串格式（input=output=cache 相同）：
#   LIBRARIES = {"test1": "/mnt/test1"}
# 字典格式（可分别指定 output/cache，省略的字段默认同 input）：
#   LIBRARIES = {"test1": {"input": "/mnt/test1", "output": "/mnt/out", "cache": "/mnt/tmp"}}
_LIBRARIES_RAW = {
    "test1": {"input": "/mnt/NFS-5070-tmp1", "output": "/mnt/tmp0", "cache": "/mnt/transcode_cache"}
}


def _normalize_libraries(raw):
    normalized = {}
    for name, value in raw.items():
        if isinstance(value, str):
            normalized[name] = {"input": value, "output": value, "cache": value}
        elif isinstance(value, dict):
            inp = value["input"]
            normalized[name] = {
                "input": inp,
                "output": value.get("output", inp),
                "cache": value.get("cache", inp),
            }
        else:
            raise TypeError(f"Invalid LIBRARIES entry for {name}: {value!r}")
    return normalized


LIBRARIES = _normalize_libraries(_LIBRARIES_RAW)


_MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "models")

FFMPEG_PATH = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE_PATH = "/usr/lib/jellyfin-ffmpeg/ffprobe"
# VMAF 打分必须使用新版本 ffmpeg：fps 帧率对齐 + libvmaf 组合在旧构建会崩溃
# （Assertion best_input >= 0 at fftools/ffmpeg_filter.c），
# 自编译统一版 /opt/ffmpeg-qsv-vmaf（git-2026-07-30）无此问题。
# VMAF scoring requires a recent ffmpeg: combining the fps filter with libvmaf
# crashes on older builds (Assertion best_input >= 0 at fftools/ffmpeg_filter.c).
VMAF_FFMPEG_PATH = "/opt/ffmpeg-qsv-vmaf/bin/ffmpeg"
VMAF_MODEL_PATH = os.path.join(_MODEL_DIR, "vmaf_v0.6.1neg.json")

SCAN_INTERVAL = 1200
# 文件稳定窗口：mtime 距今超过该秒数才认为写入完成（防止残缺文件入队转码）。
# File stability window: only submit files whose mtime is older than this (seconds).
FILE_STABLE_WINDOW = 300
FAILED_RETRY_BASE = 3600
FAILED_RETRY_MAX = 259200
FAILED_RETRY_MAX_RETRIES = 3
MAX_WORKERS = 2
CODEC_TIMEOUT = 18000

TEMP_PREFIX = ".work_"

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".ts",
    ".mts",
    ".m2ts",
    ".wmv",
    ".flv",
    ".webm",
    ".rmvb",
}

SKIP_CODECS = set()

# 编码目标 / Encoding target
# "hevc" → hevc_qsv (HEVC Main10, Intel Iris Xe)
# "av1"  → av1_qsv  (AV1, Intel Arc)
VIDEO_ENCODER = "av1"

# 编码器独立参数 / Per-encoder settings
ENCODER_PARAMS = {
    "hevc": {
        "codec": "hevc_qsv",
        "profile": "main10",
        "quality": 20,
        "preset": "slow",
        "b_frames": 8,
        "extbrc": True,
        "adaptive_i": True,
        "adaptive_b": True,
    },
    "av1": {
        "codec": "av1_qsv",
        "profile": "main",
        "quality": 24,
        "preset": "veryslow",
        "b_frames": 7,
        "extbrc": True,
        "adaptive_i": True,
        "adaptive_b": True,
        "b_strategy": 1,
    },
}

VIDEO_GOP = 240
VIDEO_LOOK_AHEAD_DEPTH = 70

# 音频编码参数
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "256k"
AUDIO_PASSTHROUGH_CODECS = {"aac"}

# 流处理策略
KEEP_SUBTITLES = True
KEEP_ATTACHMENTS = True
# 软解回退编解码器（QSV 硬解崩溃时回退到纯软件解码 + hwupload）
# Software decode fallback codecs (fall back to software decode + hwupload when QSV crashes)
SW_DECODE_FALLBACK_CODECS = {"vc1"}

LOG_FILE = "data/app.log"
# 转码历史数据库（SQLite）/ Transcoding history database (SQLite)
HISTORY_FILE = "data/history.db"

# VMAF 采样间隔：每 N 帧只评估 1 帧。全片解码但大幅减少计算量。
# VMAF subsample: evaluate 1 frame every N frames. Covers the whole video with reduced cost.
VMAF_SUBSAMPLE = 30

# VMAF 打分前统一帧率对齐。VFR 源被 QSV/软件编码器规整为 CFR 后，
# 产物与源的帧序会产生累计漂移，导致 libvmaf 逐帧对比错位、分数虚低。
# 打分时给两个输入都加 fps 滤镜归一到同一时间网格，可消除该系统性误差。
# 目标帧率优先从源视频实际帧率自动推导（适配任意帧率），VMAF_ALIGN_FPS_RATE
# 仅在源探测失败（如 ffprobe 不可用）时作为缺省值。
# 实测（Arc A310 + av1_qsv）：同一产物不对齐 63.19，对齐后 94.83 且三次一致。
# Rate-alignment before VMAF: a VFR source normalized to CFR by the encoder drifts
# out of per-frame sync with its own source, spuriously deflating libvmaf scores.
# Both inputs get a fps filter so they land on the same time grid. The target rate
# is auto-derived from the source for generality; VMAF_ALIGN_FPS_RATE is only the
# fallback when probing (ffprobe) fails. Measured: 63.19 -> 94.83 (3x reproducible).
VMAF_ALIGN_FPS = True
VMAF_ALIGN_FPS_RATE = "30000/1001"

# VMAF 逐帧 json 日志目录：即使转码结果被拒绝也持久保留，用于事后审计判定是否可信。
# Directory for per-run VMAF json logs; kept even when the result is rejected, for auditing.
VMAF_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "vmaf_logs")
# VMAF 日志保留天数，超出此期限的逐帧 json 日志将被自动清理。
# VMAF log retention in days; older per-frame json logs are auto-purged.
VMAF_LOG_RETENTION_DAYS = 7
VMAF_TIMEOUT = 36000
# VMAF 开启 QSV 硬件加速解码（HW VMAF 失败时自动回退到纯软件）
# Enable QSV hardware-accelerated decoding for VMAF (falls back to software on failure)
VMAF_HWACCEL = True
VMAF_HW_FALLBACK = True
# VMAF 是否为转码的硬性要求：True=不可用时拒绝，False=不可用时仍接受（仅按大小比判断）。
# 默认关闭（VMAF 需通过 --vmaf sync|async 显式启用）。
# Whether VMAF is required: True=reject if unavailable, False=accept anyway (ratio check only).
# Disabled by default; enable via --vmaf sync|async.
VMAF_REQUIRED = False

# 异步 VMAF 打分：True=转码成功后先保留源文件并移到输出区，后台线程打分后决定去留。
# 打分 worker 线程数（VMAF 极耗解码器资源，默认 1 即可）。
# Async VMAF scoring: True=move output into place without deleting the source; a background
# worker scores VMAF later and keeps exactly one of the two files.
VMAF_ASYNC = False
VMAF_WORKERS = 1

# 质量接受规则阈值 / Quality acceptance thresholds
# 极端低分阈值：VMAF 低于此值视为输入/产物异常（如写入中残缺文件），按 failed 重试而非永久拒绝。
# Extreme-low threshold: VMAF below this suggests an incomplete input/output; mark failed for retry.
VMAF_EXTREME_LOW = 60
VMAF_ACCEPT_HIGH = 93  # VMAF ≥ 此值时检查 ACCEPT_RATIO_MAX
VMAF_ACCEPT_LOW = 80  # VMAF < 此值时直接拒绝
ACCEPT_RATIO_MAX = 1.00  # VMAF ≥ VMAF_ACCEPT_HIGH 时允许的最大 ratio
ACCEPT_RATIO_LOW = 0.75  # VMAF 在中间区间时允许的最大 ratio

# 转码后帧数校验阈值：输出帧数低于 源时长×源帧率 × 此系数 时判定为静默失败
# （vpp_qsv 偶发只编码十几帧就 rc=0 退出，需靠帧数校验兜底并触发软件缩放重试）。
# Post-transcode frame-count check threshold: output frames below
# source_duration * source_fps * FRAME_COUNT_MIN_RATIO means the encode silently
# stalled (vpp_qsv sometimes exits rc=0 after a handful of frames), so we fall
# back to software scaling.
FRAME_COUNT_MIN_RATIO = 0.5

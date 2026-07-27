import os

# 多库支持：名称 → {input, output, cache} 映射。
# Multi-library support: name → {input, output, cache} mapping.
#
# 简单字符串格式（input=output=cache 相同）：
#   LIBRARIES = {"test1": "/mnt/test1"}
# 字典格式（可分别指定 output/cache，省略的字段默认同 input）：
#   LIBRARIES = {"test1": {"input": "/mnt/test1", "output": "/mnt/out", "cache": "/mnt/tmp"}}
_LIBRARIES_RAW = {"test1": {"input": "/mnt/NFS-5070-tmp1", "output": "/mnt/tmp0", "cache": "/mnt/transcode_cache"}}


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
FFMPEG_PATH = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE_PATH = "/usr/lib/jellyfin-ffmpeg/ffprobe"

SCAN_INTERVAL = 1200
FAILED_RETRY_BASE = 3600
FAILED_RETRY_MAX = 259200
FAILED_RETRY_MAX_RETRIES = 3
MAX_WORKERS = 2
PER_FILE_TIMEOUT = 14400

TEMP_PREFIX = ".work_"

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".mts", ".m2ts", ".wmv", ".flv", ".webm", ".rmvb"}

SKIP_CODECS = {"hevc", "av1"}

# 编码目标 / Encoding target
# "hevc" → hevc_qsv (HEVC Main10, Intel Iris Xe)
# "av1"  → av1_qsv  (AV1, Intel Arc)
VIDEO_ENCODER = "av1"

# 编码器独立参数 / Per-encoder settings
ENCODER_PARAMS = {
    "hevc": {
        "codec":        "hevc_qsv",
        "profile":      "main10",
        "quality":      20,
        "preset":       "slow",
        "b_frames":     8,
        "extbrc":       True,
        "adaptive_i":   True,
        "adaptive_b":   True,
    },
    "av1": {
        "codec":        "av1_qsv",
        "profile":      "main",
        "quality":      24,
        "preset":       "veryslow",
        "b_frames":     7,
        "extbrc":       True,
        "adaptive_i":   True,
        "adaptive_b":   True,
        "b_strategy":   1,
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

# VMAF 质量评估 / VMAF quality assessment
VMAF_FFMPEG_PATH = "/usr/lib/johnvansickle-ffmpeg/ffmpeg"
VMAF_MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "models", "vmaf_v0.6.1.json")
# VMAF 启用条件：需要支持 libvmaf 的 ffmpeg 二进制。模型文件可选（不提供则使用内置模型）
# Requires ffmpeg with libvmaf. Model file is optional (built-in model used if missing).
VMAF_ENABLED = os.path.exists(VMAF_FFMPEG_PATH)
# VMAF 采样间隔：每 N 帧只评估 1 帧。全片解码但大幅减少计算量。
# VMAF subsample: evaluate 1 frame every N frames. Covers the whole video with reduced cost.
VMAF_SUBSAMPLE = 30

# VMAF 逐帧 json 日志目录：即使转码结果被拒绝也持久保留，用于事后审计判定是否可信。
# Directory for per-run VMAF json logs; kept even when the result is rejected, for auditing.
VMAF_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "vmaf_logs")
# VMAF 日志保留天数，超出此期限的逐帧 json 日志将被自动清理。
# VMAF log retention in days; older per-frame json logs are auto-purged.
VMAF_LOG_RETENTION_DAYS = 7
VMAF_TIMEOUT = 7200
# VMAF 是否为转码的硬性要求：True=不可用时拒绝，False=不可用时仍接受（仅按大小比判断）。
# Whether VMAF is required: True=reject if unavailable, False=accept anyway (ratio check only).
VMAF_REQUIRED = True

# 质量接受规则阈值 / Quality acceptance thresholds
VMAF_ACCEPT_HIGH = 93     # VMAF ≥ 此值时检查 ACCEPT_RATIO_MAX
VMAF_ACCEPT_LOW = 80      # VMAF < 此值时直接拒绝
ACCEPT_RATIO_MAX = 1.00   # VMAF ≥ VMAF_ACCEPT_HIGH 时允许的最大 ratio
ACCEPT_RATIO_LOW = 0.75   # VMAF 在中间区间时允许的最大 ratio


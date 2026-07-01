import os
import logging

WATCH_DIR = "/mnt/test1"
# 多库支持：名称 → 路径映射。至少保留一个入口。
# Multi-library support: name → path mapping.
LIBRARIES = {
    "test1": "/mnt/test1",
}
FFMPEG_PATH = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE_PATH = "/usr/lib/jellyfin-ffmpeg/ffprobe"

SCAN_INTERVAL = 3600
FULL_SCAN_INTERVAL = 604800
MAX_WORKERS = 2

TEMP_PREFIX = ".work_"

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".mts", ".m2ts", ".wmv", ".flv", ".webm", ".rmvb"}

SKIP_CODECS = {"hevc", "av1"}

# 视频编码参数
VIDEO_QUALITY = 20
VIDEO_PRESET = "slow"
VIDEO_GOP = 240
VIDEO_B_FRAMES = 8
VIDEO_LOOK_AHEAD_DEPTH = 70

# 音频编码参数
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "256k"
AUDIO_PASSTHROUGH_CODECS = {"aac"}

# 流处理策略
KEEP_SUBTITLES = True
KEEP_ATTACHMENTS = True

LOG_FILE = "data/app.log"
HISTORY_FILE = "data/history.json"

# VMAF 质量评估 / VMAF quality assessment
VMAF_FFMPEG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bin", "ffmpeg")
VMAF_MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "models", "vmaf_v0.6.1.json")
# VMAF 启用条件：需要支持 libvmaf 的 ffmpeg 二进制。模型文件可选（不提供则使用内置模型）
# Requires ffmpeg with libvmaf. Model file is optional (built-in model used if missing).
VMAF_ENABLED = os.path.exists(VMAF_FFMPEG_PATH)
# VMAF 取样时长（秒），取前 N 秒进行对比。太长耗时，太短可能不具代表性。
# VMAF sample duration (seconds). Longer is more accurate but slower.
VMAF_SAMPLE_DURATION = 60

_log_dir = os.path.dirname(LOG_FILE)
if _log_dir:
    os.makedirs(_log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

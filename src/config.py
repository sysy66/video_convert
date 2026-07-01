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

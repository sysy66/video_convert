import logging

WATCH_DIR = "/mnt/test1"
FFMPEG_PATH = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE_PATH = "/usr/lib/jellyfin-ffmpeg/ffprobe"

SCAN_INTERVAL = 3600
FULL_SCAN_INTERVAL = 604800
MAX_WORKERS = 2

TEMP_PREFIX = ".work_"

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".mts", ".m2ts", ".wmv", ".flv", ".webm"}

SKIP_CODECS = {"hevc", "av1"}

LOG_FILE = "app.log"
HISTORY_FILE = "history.json"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

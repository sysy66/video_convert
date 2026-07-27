import os
from pathlib import Path

from src import config

log = __import__("logging").getLogger(__name__)


def walk_directory(watch_dir, video_extensions):
    watch_dir = Path(watch_dir)
    if not watch_dir.exists():
        log.error("Watch directory does not exist: %s", watch_dir)
        return

    for root, dirs, files in os.walk(watch_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith(config.TEMP_PREFIX)]
        for f in files:
            if f.startswith("."):
                continue
            ext = Path(f).suffix.lower()
            if ext not in video_extensions:
                continue
            yield Path(root) / f

"""
目录遍历模块。递归遍历监控目录，按扩展名过滤视频文件，并排除隐藏文件/目录与临时目录。

Directory-walk module. Recursively walks the watched directory, filtering video
files by extension and excluding hidden files/dirs and temp directories.
"""

import os
from pathlib import Path

from src import config

log = __import__("logging").getLogger(__name__)


def walk_directory(watch_dir, video_extensions):
    """递归遍历目录，生成符合扩展名、非隐藏文件且不在隐藏/临时目录中的视频文件路径。

    Recursively walk a directory, yielding paths of video files whose extension is
    in `video_extensions`, that are not hidden files, and that are not under
    hidden or temp directories.

    :param watch_dir: 监控根目录 (Root directory to watch)
    :param video_extensions: 可接受的扩展名集合（含点，如 {'.mp4', '.mkv'}）
                             (Accepted extension set, with dots, e.g. {'.mp4', '.mkv'})
    :return: 视频文件路径的生成器 (A generator of video file paths)
    """
    watch_dir = Path(watch_dir)
    if not watch_dir.exists():
        log.error("Watch directory does not exist: %s", watch_dir)
        return

    for root, dirs, files in os.walk(watch_dir):
        dirs[:] = [
            d for d in dirs if not d.startswith(".") and not d.startswith(config.TEMP_PREFIX)
        ]
        for f in files:
            if f.startswith("."):
                continue
            ext = Path(f).suffix.lower()
            if ext not in video_extensions:
                continue
            yield Path(root) / f

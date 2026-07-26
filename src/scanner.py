"""
视频扫描模块。负责遍历指定目录以查找视频文件，并根据后缀、历史记录和编码格式进行过滤。
Video scanning module. Responsible for traversing specified directories to find video files,
filtering based on extensions, history records, and codec formats.
"""

import os
import time
from pathlib import Path

from src import config
from src.probe import get_video_codec

log = __import__("logging").getLogger(__name__)


def scan(watch_dir, video_extensions, skip_codecs, ffprobe_path, history):
    """
    扫描指定的目录以查找可处理的视频文件。
    Scan the specified directory to find processable video files.

    :param watch_dir: 需要扫描的根目录路径 (Root directory to scan)
    :param video_extensions: 允许的视频文件扩展名列表 (List of allowed video extensions)
    :param skip_codecs: 需要跳过的编码格式列表 (List of codecs to skip)
    :param ffprobe_path: ffprobe 可执行文件的路径 (Path to ffprobe executable)
    :param history: 记录已处理文件的历史对象 (History object for processed files)
    :return: 过滤后的视频文件路径列表 (List of filtered video file paths)
    """
    watch_dir = Path(watch_dir)
    if not watch_dir.exists():
        log.error("Watch directory does not exist: %s", watch_dir)
        return []

    candidates = []

    for root, dirs, files in os.walk(watch_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith(config.TEMP_PREFIX)]

        for f in files:
            if f.startswith("."):
                continue

            ext = Path(f).suffix.lower()
            if ext not in video_extensions:
                continue

            full_path = Path(root) / f
            entry = history.get_entry(str(full_path))

            if entry:
                status = entry.get("status")

                if status in ("success", "rejected", "not required", "started"):
                    continue

                if status == "failed":
                    retry_count = entry.get("retry_count", 0)
                    delay = min(config.FAILED_RETRY_BASE * (2 ** retry_count), config.FAILED_RETRY_MAX)
                    if delay >= config.FAILED_RETRY_MAX:
                        log.warning("GAVE UP (exhausted retries): %s", full_path)
                        history.mark_exhausted(str(full_path))
                        continue
                    completed = entry.get("completed_at", 0)
                    if time.time() - completed < delay:
                        log.debug("SKIP (cooldown %.1fh): %s", delay / 3600, f)
                        continue

                elif status == "ready":
                    codec = entry.get("codec")
                    if codec is None or codec not in skip_codecs:
                        candidates.append(full_path)
                    continue

            codec = get_video_codec(full_path, ffprobe_path)

            if codec is None:
                log.warning("PROBE FAILED (queued anyway): %s", full_path)
                history.mark_ready(str(full_path), codec=None)
                candidates.append(full_path)
            elif codec in skip_codecs:
                history.mark_skipped(str(full_path), codec)
                log.debug("SKIP (codec=%s): %s", codec, full_path)
            else:
                history.mark_ready(str(full_path), codec)
                candidates.append(full_path)

    candidates.sort(key=lambda p: p.stat().st_mtime)
    return candidates

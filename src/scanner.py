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
                    completed = entry.get("completed_at", 0)
                    retry_count = entry.get("retry_count", 0)
                    delay = min(config.FAILED_RETRY_BASE * (2 ** retry_count), config.FAILED_RETRY_MAX)
                    if time.time() - completed < delay:
                        log.debug("SKIP (cooldown %.1fh): %s", delay / 3600, f)
                        continue

            candidates.append(full_path)

    if not candidates:
        return []

    codec_filtered = []
    for p in candidates:
        codec = history.get_cached_codec(str(p))
        if codec is None:
            codec = get_video_codec(p, ffprobe_path)
            if codec is None:
                log.warning("SKIP (probe failed): %s", p)
                history.mark_probe_failed(str(p))
                continue

        if codec in skip_codecs:
            history.mark_skipped(str(p), codec)
            log.debug("SKIP (codec=%s): %s", codec, p)
            continue

        codec_filtered.append(p)

    # 根据修改时间对结果进行排序（按时间先后）
    # Sort results by modification time (chronological order)
    codec_filtered.sort(key=lambda p: p.stat().st_mtime)
    return codec_filtered

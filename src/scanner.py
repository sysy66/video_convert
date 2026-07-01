"""
视频扫描模块。负责遍历指定目录以查找视频文件，并根据后缀、历史记录和编码格式进行过滤。
Video scanning module. Responsible for traversing specified directories to find video files,
filtering based on extensions, history records, and codec formats.
"""

import os
from pathlib import Path

from src.config import TEMP_PREFIX
from src.probe import get_video_codec

log = __import__("logging").getLogger(__name__)


def scan(watch_dir, video_extensions, skip_codecs, ffprobe_path, history, is_full_scan):
    """
    扫描指定的目录以查找可处理的视频文件。
    Scan the specified directory to find processable video files.

    :param watch_dir: 需要扫描的根目录路径 (Root directory to scan)
    :param video_extensions: 允许的视频文件扩展名列表 (List of allowed video extensions)
    :param skip_codecs: 需要跳过的编码格式列表 (List of codecs to skip)
    :param ffprobe_path: ffprobe 可执行文件的路径 (Path to ffprobe executable)
    :param history: 记录已处理文件的历史对象 (History object for processed files)
    :param is_full_scan: 是否进行全量扫描 (Whether to perform a full scan)
    :return: 过滤后的视频文件路径列表 (List of filtered video file paths)
    """
    watch_dir = Path(watch_dir)
    # 检查目录是否存在，如果不存在则记录错误并返回空列表
    # Check if directory exists; log error and return empty list if not
    if not watch_dir.exists():
        log.error("Watch directory does not exist: %s", watch_dir)
        return []

    candidates = []

    # 递归遍历目录
    # Recursively traverse the directory
    for root, dirs, files in os.walk(watch_dir):
        # 过滤掉以 "." 开头的隐藏目录和临时目录
        # Filter out hidden directories and temporary directories
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith(TEMP_PREFIX)]

        for f in files:
            # 跳过隐藏文件
            # Skip hidden files
            if f.startswith("."):
                continue

            # 根据后缀名过滤文件
            # Filter files based on extensions
            ext = Path(f).suffix.lower()
            if ext not in video_extensions:
                continue

            full_path = Path(root) / f

            # 根据全量扫描标志和历史记录过滤已处理的文件
            # Filter processed files based on full_scan flag and history records
            if not is_full_scan and history.exists(str(full_path)):
                continue
            if is_full_scan:
                if history.is_done(str(full_path)) or history.is_started(str(full_path)):
                    continue

            candidates.append(full_path)

    if not candidates:
        return []

    codec_filtered = []
    for p in candidates:
        # 获取视频编码
        # Get the video codec
        codec = get_video_codec(p, ffprobe_path)
        if codec is None:
            # 如果探测失败，记录警告并跳过
            # Log warning and skip if probing fails
            log.warning("SKIP (probe failed): %s", p)
            continue

        # 如果编码在跳过列表中，则忽略
        # Skip if the codec is in the skip list
        if codec in skip_codecs:
            log.info("SKIP (codec=%s): %s", codec, p)
            continue

        codec_filtered.append(p)

    # 根据修改时间对结果进行排序（按时间先后）
    # Sort results by modification time (chronological order)
    codec_filtered.sort(key=lambda p: p.stat().st_mtime)
    return codec_filtered

"""
过滤模块。决定一个文件是否应该提交（should_submit）或进一步处理
（should_process），结合历史状态、文件稳定性与编码类型进行过滤。

Filtering module. Decides whether a file should be submitted (should_submit) or
further processed (should_process), based on history state, file stability and
video codec.
"""

import time
from pathlib import Path

from src import config
from src.probe import get_video_codec

log = __import__("logging").getLogger(__name__)


def _history_allows(filepath, history):
    """仅基于 history 状态判断文件是否可处理（不探测编解码器，供提交前快速过滤）。

    History-only gate: whether this file may be processed, without probing codecs.
    """
    filepath = Path(filepath)

    entry = history.get_entry(str(filepath))

    if entry:
        status = entry.get("status")

        if status in ("success", "rejected", "not required"):
            return False

        if status in ("pending_transcode", "pending_postprocess", "failed"):
            return False

    return True


def _file_is_stable(filepath):
    """mtime 距今超过稳定窗口才认为写入完成（防止写入中的残缺文件入队）。

    A file whose mtime is older than FILE_STABLE_WINDOW is considered fully
    written. stat 失败（瞬态/网络错误）不阻塞提交，worker 的 probe 会兜底。
    """
    try:
        mtime = filepath.stat().st_mtime
    except OSError:
        return True
    return time.time() - mtime >= config.FILE_STABLE_WINDOW


def should_submit(filepath, history):
    """提交前轻量过滤：只查 history 状态与文件稳定度，不做 ffprobe 探测（probe 留给 worker）。

    Cheap pre-submit gate used by the scan loop: history status and file
    stability only, no probing.
    """
    if not _history_allows(filepath, history):
        return False
    if not _file_is_stable(filepath):
        log.debug(
            "SKIP (unstable, mtime < %ds): %s",
            config.FILE_STABLE_WINDOW,
            Path(filepath).name,
        )
        return False
    return True


def should_process(filepath, history, skip_codecs, ffprobe_path):
    """判断文件是否应进入转码队列：历史允许、文件存在且编码不在跳过列表中。

    Decide whether a file should enter the transcode queue: history permits it,
    the file exists, and its codec is not in the skip list.

    :param filepath: 源文件路径 (Path to the source file)
    :param history: 历史记录对象 (History tracking object)
    :param skip_codecs: 需要跳过的编码集合 (Set of codecs to skip)
    :param ffprobe_path: ffprobe 可执行文件路径 (Path to the ffprobe executable)
    :return: True 表示应处理，否则 False (True if it should be processed)
    """
    if not _history_allows(filepath, history):
        return False

    filepath = Path(filepath)
    if not filepath.exists():
        log.debug("SKIP (missing): %s", filepath.name)
        return False

    codec = get_video_codec(filepath, ffprobe_path)
    if codec is None:
        log.warning("PROBE FAILED (queued anyway): %s", filepath)
        return True

    if codec in skip_codecs:
        history.mark_skipped(str(filepath), codec)
        log.debug("SKIP (codec=%s): %s", codec, filepath.name)
        return False

    return True

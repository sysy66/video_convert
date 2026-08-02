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

        if status in ("started", "pending_vmaf"):
            return False

        if status == "failed":
            retry_count = entry.get("retry_count", 0)
            delay = min(config.FAILED_RETRY_BASE * (2**retry_count), config.FAILED_RETRY_MAX)
            if retry_count >= config.FAILED_RETRY_MAX_RETRIES:
                log.warning("GAVE UP (exhausted retries): %s", filepath)
                history.mark_exhausted(str(filepath))
                return False
            completed = entry.get("completed_at", 0)
            if time.time() - completed < delay:
                log.debug("SKIP (cooldown %.1fh): %s", delay / 3600, filepath.name)
                return False

    return True


def should_submit(filepath, history):
    """提交前轻量过滤：只查 history 状态，不做 ffprobe 探测（probe 留给 worker）。

    Cheap pre-submit gate used by the scan loop: history status only, no probing.
    """
    return _history_allows(filepath, history)


def should_process(filepath, history, skip_codecs, ffprobe_path):
    if not _history_allows(filepath, history):
        return False

    filepath = Path(filepath)
    codec = get_video_codec(filepath, ffprobe_path)
    if codec is None:
        log.warning("PROBE FAILED (queued anyway): %s", filepath)
        return True

    if codec in skip_codecs:
        history.mark_skipped(str(filepath), codec)
        log.debug("SKIP (codec=%s): %s", codec, filepath.name)
        return False

    return True

import time
from pathlib import Path

from src import config
from src.probe import get_video_codec

log = __import__("logging").getLogger(__name__)


def should_process(filepath, history, skip_codecs, ffprobe_path):
    filepath = Path(filepath)

    entry = history.get_entry(str(filepath))

    if entry:
        status = entry.get("status")

        if status in ("success", "rejected", "not required"):
            return False

        if status == "started":
            return False

        if status == "failed":
            retry_count = entry.get("retry_count", 0)
            delay = min(config.FAILED_RETRY_BASE * (2 ** retry_count), config.FAILED_RETRY_MAX)
            if delay >= config.FAILED_RETRY_MAX:
                log.warning("GAVE UP (exhausted retries): %s", filepath)
                history.mark_exhausted(str(filepath))
                return False
            completed = entry.get("completed_at", 0)
            if time.time() - completed < delay:
                log.debug("SKIP (cooldown %.1fh): %s", delay / 3600, filepath.name)
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

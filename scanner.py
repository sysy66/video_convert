import os
from pathlib import Path

from probe import get_video_codec

log = __import__("logging").getLogger(__name__)


def scan(watch_dir, video_extensions, skip_codecs, ffprobe_path, history, is_full_scan):
    watch_dir = Path(watch_dir)
    if not watch_dir.exists():
        log.error("Watch directory does not exist: %s", watch_dir)
        return []

    candidates = []

    for root, dirs, files in os.walk(watch_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith(TEMP_PREFIX)]

        for f in files:
            if f.startswith("."):
                continue

            ext = Path(f).suffix.lower()
            if ext not in video_extensions:
                continue

            full_path = Path(root) / f

            if not is_full_scan and history.exists(str(full_path)):
                continue
            if is_full_scan and history.is_done(str(full_path)):
                continue

            candidates.append(full_path)

    if not candidates:
        return []

    codec_filtered = []
    for p in candidates:
        codec = get_video_codec(p, ffprobe_path)
        if codec is None:
            log.warning("SKIP (probe failed): %s", p)
            continue
        if codec in skip_codecs:
            log.info("SKIP (codec=%s): %s", codec, p)
            continue
        codec_filtered.append(p)

    codec_filtered.sort(key=lambda p: p.stat().st_mtime)
    return codec_filtered


TEMP_PREFIX = __import__("config").TEMP_PREFIX

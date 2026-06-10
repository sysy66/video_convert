import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from history import History
from scanner import scan
from transcoder import transcode, cleanup_stale_temp_dirs

log = logging.getLogger(__name__)

_running = True


def signal_handler(signum, frame):
    global _running
    log.info("Received signal %s, shutting down gracefully...", signum)
    _running = False


def main_loop():
    global _running

    history = History(config.HISTORY_FILE)
    last_full_scan = history.get_last_full_scan()

    log.info("=" * 50)
    log.info("Video Transcoder Started")
    log.info("Watch directory: %s", config.WATCH_DIR)
    log.info("Scan interval: %ds | Full scan interval: %ds", config.SCAN_INTERVAL, config.FULL_SCAN_INTERVAL)
    log.info("Max workers: %d", config.MAX_WORKERS)
    log.info("=" * 50)

    cleanup_stale_temp_dirs(config.WATCH_DIR, config.TEMP_PREFIX)

    while _running:
        now = time.time()
        is_full_scan = (now - last_full_scan >= config.FULL_SCAN_INTERVAL)

        if is_full_scan:
            log.info("FULL SCAN: re-scanning entire library")
        else:
            log.info("INCREMENTAL SCAN: checking for new files")

        files = scan(
            config.WATCH_DIR,
            config.VIDEO_EXTENSIONS,
            config.SKIP_CODECS,
            config.FFPROBE_PATH,
            history,
            is_full_scan
        )

        if files:
            log.info("SCAN: found %d file(s) to process", len(files))
            for f in files:
                log.info("  QUEUED: %s", f)

            with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
                fut_map = {executor.submit(transcode, f, history, config.FFMPEG_PATH): f for f in files}
                for future in as_completed(fut_map):
                    f = fut_map[future]
                    try:
                        ok = future.result()
                        if ok:
                            log.info("RESULT: %s -> done", f.name)
                        else:
                            log.warning("RESULT: %s -> failed", f.name)
                    except Exception as e:
                        log.error("RESULT: %s -> unhandled exception: %s", f.name, e)
        else:
            log.info("SCAN: no files to process")

        if is_full_scan:
            last_full_scan = time.time()
            history.set_last_full_scan(last_full_scan)

        if not _running:
            break

        log.info("Sleeping for %d seconds...", config.SCAN_INTERVAL)
        for _ in range(config.SCAN_INTERVAL):
            if not _running:
                break
            time.sleep(1)

    log.info("Shutdown complete")


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        main_loop()
    except Exception as e:
        log.critical("Fatal error: %s", e, exc_info=True)
        raise


if __name__ == "__main__":
    main()

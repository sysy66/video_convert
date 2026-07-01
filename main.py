import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from src import config
from src.history import History
from src.scanner import scan
from src.transcoder import transcode, cleanup_stale_temp_dirs, kill_active_ffmpeg, mark_stale_started_as_failed

log = logging.getLogger(__name__)

_running = True


def signal_handler(signum, frame):
    global _running
    log.info("Received signal %s, shutting down gracefully...", signum)
    _running = False
    kill_active_ffmpeg()


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

    # 首次循环会执行一次清理和状态修复
    # First iteration will perform cleanup and stale-state repair

    while _running:
        # 每个扫描周期前清理残留临时目录，并修复因停电等导致 stuck 的 started 状态
        # Clean up stale temp dirs and repair stuck "started" entries before each scan
        cleanup_stale_temp_dirs(config.WATCH_DIR, config.TEMP_PREFIX)
        mark_stale_started_as_failed(history)

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
                fut_map = {executor.submit(transcode, f, history, config.FFMPEG_PATH, config.FFPROBE_PATH): f for f in files}
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

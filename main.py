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
    log.info("Libraries: %s", ", ".join(f"{k}={v}" for k, v in config.LIBRARIES.items()))
    log.info("Scan interval: %ds | Full scan interval: %ds", config.SCAN_INTERVAL, config.FULL_SCAN_INTERVAL)
    log.info("Max workers: %d", config.MAX_WORKERS)
    log.info("=" * 50)

    while _running:
        # 每个扫描周期前清理所有库的残留临时目录
        # Clean up stale temp dirs across all libraries before each scan
        for lib_path in config.LIBRARIES.values():
            cleanup_stale_temp_dirs(lib_path, config.TEMP_PREFIX)
        # 修复因停电等导致 stuck 的 started 状态
        # Repair stuck "started" entries left by power outages
        mark_stale_started_as_failed(history)

        now = time.time()
        is_full_scan = (now - last_full_scan >= config.FULL_SCAN_INTERVAL)

        if is_full_scan:
            log.info("FULL SCAN: re-scanning all libraries")
        else:
            log.info("INCREMENTAL SCAN: checking for new files")

        all_files = []
        for lib_name, lib_path in config.LIBRARIES.items():
            lib_files = scan(
                lib_path,
                config.VIDEO_EXTENSIONS,
                config.SKIP_CODECS,
                config.FFPROBE_PATH,
                history,
                is_full_scan,
            )
            if lib_files:
                log.info("  [%s] found %d file(s)", lib_name, len(lib_files))
            all_files.extend(lib_files)

        if all_files:
            log.info("SCAN: %d file(s) to process across %d librar%s",
                      len(all_files), len(config.LIBRARIES),
                      "y" if len(config.LIBRARIES) == 1 else "ies")
            for f in all_files:
                log.info("  QUEUED: %s", f)

            with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
                fut_map = {executor.submit(transcode, f, history, config.FFMPEG_PATH, config.FFPROBE_PATH): f for f in all_files}
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

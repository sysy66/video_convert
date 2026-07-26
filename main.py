import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait

from src import config
from src.history import History
from src.scanner import scan
from src.transcoder import transcode, cleanup_stale_temp_dirs, kill_active_ffmpeg, mark_stale_started_as_failed
from src.quality import cleanup_vmaf_logs
from src.validation import validate_config

log = logging.getLogger(__name__)

_running = True


_DRAIN_TIMEOUT = 120


def signal_handler(signum, frame):
    global _running
    log.info("Received signal %s, shutting down gracefully...", signum)
    _running = False


def main_loop():
    global _running

    history = History(config.HISTORY_FILE)

    errors, warnings = validate_config()
    for w in warnings:
        log.warning("VALIDATION: %s", w)
    if errors:
        for e in errors:
            log.critical("VALIDATION FAILED: %s", e)
        history.close()
        sys.exit(1)

    log.info("=" * 50)
    log.info("Video Transcoder Started")
    for name, info in config.LIBRARIES.items():
        parts = [f"input={info['input']}"]
        if info["output"] != info["input"]:
            parts.append(f"output={info['output']}")
        if info["cache"] != info["input"]:
            parts.append(f"cache={info['cache']}")
        log.info("  [%s] %s", name, ", ".join(parts))
    log.info("Scan interval: %ds", config.SCAN_INTERVAL)
    log.info("Max workers: %d", config.MAX_WORKERS)
    log.info("=" * 50)

    while _running:
        for lib_info in config.LIBRARIES.values():
            cleanup_stale_temp_dirs(lib_info["cache"], config.TEMP_PREFIX)
            if lib_info["cache"] != lib_info["input"]:
                cleanup_stale_temp_dirs(lib_info["input"], config.TEMP_PREFIX)
        mark_stale_started_as_failed(history)
        cleanup_vmaf_logs()

        log.info("SCAN: checking all libraries")

        all_files = []
        for lib_name, lib_info in config.LIBRARIES.items():
            lib_files = scan(
                lib_info["input"],
                config.VIDEO_EXTENSIONS,
                config.SKIP_CODECS,
                config.FFPROBE_PATH,
                history,
            )
            if lib_files:
                log.info("  [%s] found %d file(s)", lib_name, len(lib_files))
            for f in lib_files:
                all_files.append((f, lib_info["input"], lib_info.get("output"), lib_info.get("cache")))

        if all_files:
            log.info("SCAN: %d file(s) to process across %d librar%s",
                      len(all_files), len(config.LIBRARIES),
                      "y" if len(config.LIBRARIES) == 1 else "ies")
            for f, _, _, _ in all_files:
                log.info("  QUEUED: %s", f)

            executor = ThreadPoolExecutor(max_workers=config.MAX_WORKERS)
            try:
                fut_map = {}
                for f, input_dir, output_dir, cache_dir in all_files:
                    future = executor.submit(transcode, f, history, config.FFMPEG_PATH, config.FFPROBE_PATH, input_dir, output_dir, cache_dir)
                    fut_map[future] = f
                pending = set(fut_map.keys())
                while pending and _running:
                    done, pending = wait(pending, timeout=60)
                    for future in done:
                        f = fut_map[future]
                        try:
                            ok = future.result()
                            if ok:
                                log.info("RESULT: %s -> done", f.name)
                            else:
                                log.warning("RESULT: %s -> failed", f.name)
                        except Exception as e:
                            log.error("RESULT: %s -> unhandled exception: %s", f.name, e)
                    if not pending:
                        break
                # Graceful shutdown: if signal received, drain in-flight transcodes
                # before proceeding. A safety-net kill still happens in main().
                if not _running and pending:
                    drain_deadline = time.time() + _DRAIN_TIMEOUT
                    while pending and time.time() < drain_deadline:
                        done, pending = wait(pending, timeout=min(15, drain_deadline - time.time()))
                        for future in done:
                            f = fut_map[future]
                            try:
                                ok = future.result()
                                if ok:
                                    log.info("DRAIN DONE: %s", f.name)
                                else:
                                    log.warning("DRAIN FAILED: %s", f.name)
                            except Exception as e:
                                log.error("DRAIN ERROR: %s -> %s", f.name, e)
            finally:
                executor.shutdown(wait=False)
        else:
            log.info("SCAN: no files to process")

        if not _running:
            break

        log.info("Sleeping for %d seconds...", config.SCAN_INTERVAL)
        for _ in range(config.SCAN_INTERVAL):
            if not _running:
                break
            time.sleep(1)

    log.info("Shutdown complete")
    return history


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    history = None
    try:
        history = main_loop()
    except Exception as e:
        log.critical("Fatal error: %s", e, exc_info=True)
        raise
    finally:
        if history is not None:
            history.close()
        kill_active_ffmpeg()


if __name__ == "__main__":
    main()

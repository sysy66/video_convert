import argparse
import logging
import os
import queue
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from src import config
from src.history import History
from src.walker import walk_directory
from src.filter import should_process
from src.transcoder import transcode, cleanup_stale_temp_dirs, kill_active_ffmpeg, mark_stale_started_as_failed
from src.quality import cleanup_vmaf_logs
from src.validation import validate_config

log = logging.getLogger(__name__)

_running = True


def signal_handler(signum, frame):
    global _running
    log.info("Received signal %s, shutting down gracefully...", signum)
    _running = False


def _setup_logging():
    _log_dir = os.path.dirname(config.LOG_FILE)
    if _log_dir:
        os.makedirs(_log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(config.LOG_FILE),
            logging.StreamHandler()
        ]
    )


def _apply_cli_overrides():
    parser = argparse.ArgumentParser(description="Video Transcoder")
    parser.add_argument("-L", "--library", action="append", dest="libraries",
                        metavar="NAME=PATH",
                        help="Library definition: name=input_path or name=input:output:cache")
    parser.add_argument("--scan-interval", type=int, metavar="SECONDS",
                        help="Scan interval in seconds")
    parser.add_argument("--max-workers", type=int, metavar="N",
                        help="Maximum concurrent transcodes")
    parser.add_argument("--log", metavar="FILE", help="Log file path")
    parser.add_argument("--history", metavar="FILE", help="History database path")
    parser.add_argument("--ffmpeg", metavar="PATH", help="FFmpeg binary path")
    parser.add_argument("--ffprobe", metavar="PATH", help="FFprobe binary path")
    parser.add_argument("--encoder", choices=["hevc", "av1"],
                        help="Video encoder (hevc or av1)")
    parser.add_argument("--ffmpeg-backend", choices=["prebuilt", "custom"],
                        help="FFmpeg backend: prebuilt (jellyfin+vansickle) or custom (qsv+vmaf)")
    parser.add_argument("--no-vmaf", action="store_true",
                        help="Disable VMAF quality check")
    parser.add_argument("--vmaf", action="store_true",
                        help="Enable VMAF quality check")

    args = parser.parse_args()

    if args.log:
        config.LOG_FILE = args.log
    if args.history:
        config.HISTORY_FILE = args.history
    if args.ffmpeg:
        config.FFMPEG_PATH = args.ffmpeg
    if args.ffprobe:
        config.FFPROBE_PATH = args.ffprobe
    if args.scan_interval is not None:
        config.SCAN_INTERVAL = args.scan_interval
    if args.max_workers is not None:
        config.MAX_WORKERS = args.max_workers
    if args.encoder:
        config.VIDEO_ENCODER = args.encoder
    if args.ffmpeg_backend:
        (config.FFMPEG_PATH, config.FFPROBE_PATH,
         config.VMAF_FFMPEG_PATH, config.VMAF_ENABLED) = config._resolve_backend(args.ffmpeg_backend)
    if args.no_vmaf:
        config.VMAF_ENABLED = False
    if args.vmaf:
        config.VMAF_ENABLED = True
    if args.libraries:
        raw = {}
        for lib_def in args.libraries:
            if "=" not in lib_def:
                print(f"error: --library must be NAME=PATH or NAME=input:output:cache, got {lib_def!r}")
                sys.exit(1)
            name, rest = lib_def.split("=", 1)
            parts = rest.split(":")
            if len(parts) == 1:
                raw[name] = parts[0]
            elif len(parts) == 3:
                raw[name] = {"input": parts[0], "output": parts[1], "cache": parts[2]}
            else:
                print(f"error: invalid library format {lib_def!r}; use NAME=PATH or NAME=input:output:cache")
                sys.exit(1)
        config._LIBRARIES_RAW = raw
        config.LIBRARIES = config._normalize_libraries(raw)

    return args


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

    _SENTINEL = object()

    while _running:
        for lib_info in config.LIBRARIES.values():
            cleanup_stale_temp_dirs(lib_info["cache"], config.TEMP_PREFIX)
            if lib_info["cache"] != lib_info["input"]:
                cleanup_stale_temp_dirs(lib_info["input"], config.TEMP_PREFIX)
        mark_stale_started_as_failed(history)
        cleanup_vmaf_logs()

        log.info("SCAN: checking all libraries")

        work_queue = queue.Queue()
        lib_counts = {}

        def produce():
            for lib_name, lib_info in config.LIBRARIES.items():
                count = 0
                for path in walk_directory(lib_info["input"], config.VIDEO_EXTENSIONS):
                    if not _running:
                        return
                    work_queue.put((path, lib_info))
                    count += 1
                if count:
                    log.info("  [%s] found %d file(s)", lib_name, count)
                lib_counts[lib_name] = count

        def consume():
            while True:
                try:
                    item = work_queue.get(timeout=1)
                except queue.Empty:
                    if not _running:
                        return
                    continue
                if item is _SENTINEL:
                    work_queue.put(_SENTINEL)
                    return
                path, lib_info = item
                if not _running:
                    continue
                if not should_process(path, history, config.SKIP_CODECS, config.FFPROBE_PATH):
                    continue
                transcode(path, history, config.FFMPEG_PATH, config.FFPROBE_PATH,
                          lib_info["input"], lib_info.get("output"), lib_info.get("cache"))

        producer = threading.Thread(target=produce)
        producer.start()

        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
            futures = [executor.submit(consume) for _ in range(config.MAX_WORKERS)]
            producer.join()
            for _ in range(config.MAX_WORKERS):
                work_queue.put(_SENTINEL)

        total = sum(lib_counts.values())
        if total:
            log.info("SCAN: %d file(s) processed this cycle", total)
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
    _apply_cli_overrides()
    _setup_logging()

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

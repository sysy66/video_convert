import argparse
import fcntl
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from src import config
from src.filter import should_process, should_submit
from src.history import History
from src.quality import cleanup_vmaf_logs
from src.transcoder import (
    active_temp_dirs_snapshot,
    cleanup_stale_temp_dirs,
    drain_vmaf_tasks,
    kill_active_ffmpeg,
    mark_stale_started_as_failed,
    recover_pending_vmaf,
    start_vmaf_workers,
    transcode,
)
from src.validation import validate_config
from src.walker import walk_directory

log = logging.getLogger(__name__)

_running = True


def purge_orphan_records(history):
    """清理源文件已不存在的终态记录。

    success 记录的源缺失是 ACCEPT 删源后的正常状态（用于同名去重），永不清理；
    started/pending_vmaf 由转码线程/启动 recover 收尾。仅处理源文件已消失的
    rejected/failed/not required 记录——记录失去意义（无文件要重试或保护）。
    """
    for filepath in history.iter_final_states():
        if not os.path.exists(filepath):
            log.debug("PURGE (orphan record, file missing): %s", filepath)
            history.delete(filepath)


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
        handlers=[logging.FileHandler(config.LOG_FILE), logging.StreamHandler()],
    )


def _apply_cli_overrides():
    parser = argparse.ArgumentParser(description="Video Transcoder")
    parser.add_argument(
        "-L",
        "--library",
        action="append",
        dest="libraries",
        metavar="NAME=PATH",
        help="Library definition: name=input_path or name=input:output:cache",
    )
    parser.add_argument(
        "--scan-interval", type=int, metavar="SECONDS", help="Scan interval in seconds"
    )
    parser.add_argument(
        "--max-workers", type=int, metavar="N", help="Maximum concurrent transcodes"
    )
    parser.add_argument(
        "--codec-timeout", type=int, metavar="SECONDS", help="Per-file transcode timeout"
    )
    parser.add_argument(
        "--vmaf-timeout", type=int, metavar="SECONDS", help="VMAF scoring timeout per file"
    )
    parser.add_argument("--log", metavar="FILE", help="Log file path")
    parser.add_argument("--history", metavar="FILE", help="History database path")
    parser.add_argument("--ffmpeg", metavar="PATH", help="FFmpeg binary path")
    parser.add_argument("--ffprobe", metavar="PATH", help="FFprobe binary path")
    parser.add_argument("--encoder", choices=["hevc", "av1"], help="Video encoder (hevc or av1)")
    parser.add_argument(
        "--skip-codecs",
        nargs="+",
        default=[],
        metavar="CODEC",
        help="Additional codecs to skip (space-separated)",
    )
    parser.add_argument(
        "--vmaf-ffmpeg",
        metavar="PATH",
        help="FFmpeg path for VMAF scoring (default: config.VMAF_FFMPEG_PATH)",
    )
    parser.add_argument(
        "--vmaf-model",
        metavar="PATH",
        help="VMAF model file path (default: data/models/vmaf_v0.6.1neg.json)",
    )
    parser.add_argument(
        "--vmaf-hwaccel", action="store_true", help="Enable VMAF QSV hardware decode acceleration"
    )
    parser.add_argument(
        "--vmaf-hw-fallback",
        action="store_true",
        help="Enable VMAF HW→SW fallback when HW path fails",
    )
    parser.add_argument("--no-vmaf", action="store_true", help="Disable VMAF quality check")
    parser.add_argument("--vmaf", action="store_true", help="Enable VMAF quality check")
    parser.add_argument(
        "--vmaf-async",
        action="store_true",
        help="Async VMAF scoring: keep source, score in background, keep one file",
    )

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
    if args.codec_timeout is not None:
        config.CODEC_TIMEOUT = args.codec_timeout
    if args.vmaf_timeout is not None:
        config.VMAF_TIMEOUT = args.vmaf_timeout
    if args.encoder:
        config.VIDEO_ENCODER = args.encoder
    config.SKIP_CODECS.add(config.VIDEO_ENCODER)
    if args.skip_codecs:
        config.SKIP_CODECS.update(args.skip_codecs)
    if args.vmaf_ffmpeg:
        config.VMAF_FFMPEG_PATH = args.vmaf_ffmpeg
    if args.vmaf_model:
        config.VMAF_MODEL_PATH = args.vmaf_model
    if args.vmaf_hwaccel:
        config.VMAF_HWACCEL = True
    if args.vmaf_hw_fallback:
        config.VMAF_HW_FALLBACK = True
    if args.no_vmaf:
        config.VMAF_REQUIRED = False
    if args.vmaf:
        config.VMAF_REQUIRED = True
    if args.vmaf_async:
        config.VMAF_ASYNC = True
    if args.libraries:
        raw = {}
        for lib_def in args.libraries:
            if "=" not in lib_def:
                print(
                    f"error: --library must be NAME=PATH or NAME=input:output:cache, got {lib_def!r}"  # noqa: E501  # 用户可见错误信息，保留单行便于阅读
                )
                sys.exit(1)
            name, rest = lib_def.split("=", 1)
            parts = rest.split(":")
            if len(parts) == 1:
                raw[name] = parts[0]
            elif len(parts) == 3:
                raw[name] = {"input": parts[0], "output": parts[1], "cache": parts[2]}
            else:
                print(
                    f"error: invalid library format {lib_def!r}; use NAME=PATH or NAME=input:output:cache"  # noqa: E501  # 用户可见错误信息，保留单行便于阅读
                )
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
    if config.VMAF_ASYNC:
        log.info("VMAF scoring: async (%d worker(s))", config.VMAF_WORKERS)
        start_vmaf_workers()
        recover_pending_vmaf(history)
    log.info("=" * 50)

    # 转码池常驻整个进程生命周期：扫描周期独立于转码进度，
    # 新入库文件最多等待一个扫描间隔即可入队，不再排队等待批次转码完成。
    # Transcode pool lives for the whole process; scan cadence is decoupled from
    # transcode progress so newly added files queue up within one scan interval.
    executor = ThreadPoolExecutor(max_workers=config.MAX_WORKERS)
    _pending = set()

    def process_one(path, lib_info):
        if not _running:
            return
        try:
            if not should_process(path, history, config.SKIP_CODECS, config.FFPROBE_PATH):
                return
            transcode(
                path,
                history,
                config.FFMPEG_PATH,
                config.FFPROBE_PATH,
                lib_info["input"],
                lib_info.get("output"),
                lib_info.get("cache"),
            )
        except Exception:
            log.exception("UNHANDLED ERROR: %s", path)
        finally:
            _pending.discard(str(path))

    try:
        while _running:
            for lib_info in config.LIBRARIES.values():
                cleanup_stale_temp_dirs(
                    lib_info["cache"], config.TEMP_PREFIX, active_temp_dirs_snapshot()
                )
                if lib_info["cache"] != lib_info["input"]:
                    cleanup_stale_temp_dirs(
                        lib_info["input"], config.TEMP_PREFIX, active_temp_dirs_snapshot()
                    )
            mark_stale_started_as_failed(history)
            purge_orphan_records(history)
            cleanup_vmaf_logs()

            log.info("SCAN: checking all libraries")

            total = 0
            for lib_name, lib_info in config.LIBRARIES.items():
                found = 0
                submitted = 0
                for path in walk_directory(lib_info["input"], config.VIDEO_EXTENSIONS):
                    if not _running:
                        break
                    found += 1
                    key = str(path)
                    if key in _pending:
                        continue
                    if not should_submit(path, history):
                        continue
                    _pending.add(key)
                    executor.submit(process_one, path, lib_info)
                    submitted += 1
                if found:
                    log.info("  [%s] found %d, submitted %d file(s)", lib_name, found, submitted)
                total += submitted
            if total:
                log.info("SCAN: %d file(s) submitted this cycle", total)
            else:
                log.info("SCAN: no files to process")

            if not _running:
                break

            log.info("Sleeping for %d seconds...", config.SCAN_INTERVAL)
            for _ in range(config.SCAN_INTERVAL):
                if not _running:
                    break
                time.sleep(1)
    finally:
        # 优雅关闭：等待运行中的转码完成，取消排队未启动的任务（下次启动重新扫描）。
        # Graceful shutdown: let running transcodes finish, cancel queued ones
        # (they are re-discovered on next startup).
        executor.shutdown(wait=True, cancel_futures=True)

    if config.VMAF_ASYNC:
        drain_vmaf_tasks()

    log.info("Shutdown complete")
    return history


def main():
    _apply_cli_overrides()
    _setup_logging()

    lock_file = os.path.join(os.path.dirname(config.LOG_FILE), ".transcoder.lock")
    try:
        lock_fd = open(lock_file, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as e:
        log.error("Another instance is already running (%s locked): %s", lock_file, e)
        sys.exit(1)

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

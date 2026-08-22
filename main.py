"""
程序入口模块。负责命令行参数解析、日志初始化、单实例锁、
目录监控循环与转码任务调度，并管理优雅关闭流程。

Main entry point. Handles CLI argument parsing, logging setup, single-instance
locking, the directory-scan loop, transcode scheduling, and graceful shutdown.
"""

import argparse
import fcntl
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src import config, vmaf_async
from src.ffmpeg_vmaf_scorer import FfmpegVMAFScorer
from src.filter import should_process, should_submit
from src.history import History
from src.quality import cleanup_vmaf_logs
from src.transcoder import (
    active_temp_dirs_snapshot,
    cleanup_stale_temp_dirs,
    drain_vmaf_tasks,
    kill_active_ffmpeg,
    recover_pending_vmaf,
    start_vmaf_workers,
    transcode,
)
from src.validation import validate_config
from src.walker import walk_directory

log = logging.getLogger(__name__)

_running = True


def signal_handler(signum, frame):
    """将全局运行标志置为 False，触发主循环优雅退出。

    Set the global running flag to False to trigger a graceful shutdown of the main loop.
    """
    global _running
    log.info("Received signal %s, shutting down gracefully...", signum)
    _running = False
    vmaf_async.signal_shutdown()


def _setup_logging():
    """初始化日志：同时输出到日志文件与终端。

    Initialize logging to both the log file and the console.
    """
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
    """解析命令行参数并用其覆盖配置中的对应项。

    Parse CLI arguments and apply them as overrides to the runtime configuration.
    """
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
        help="VMAF model file path (default: /opt/ffmpeg-qsv-vmaf/models/vmaf_v0.6.1neg.json)",
    )
    parser.add_argument(
        "--vmaf",
        nargs="?",
        const="sync",
        default=None,
        choices=["off", "sync", "async"],
        help="VMAF quality check: off (default) | sync | async",
    )
    parser.add_argument(
        "--no-vmaf-hwaccel",
        action="store_true",
        help="Disable VMAF QSV hardware decode acceleration",
    )
    parser.add_argument(
        "--no-vmaf-hw-fallback",
        action="store_true",
        help="Disable VMAF HW→SW fallback when HW path fails",
    )
    parser.add_argument(
        "--keep-source",
        action="store_true",
        help="Keep the source file after a successful transcode",
    )
    parser.add_argument(
        "--vmaf-threads",
        type=int,
        metavar="N",
        help="libvmaf threads: 0=auto (default, leave 1 core for transcoding) | >0 explicit",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry failed transcodes entries",
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
    if args.vmaf is not None:
        config.VMAF_REQUIRED = args.vmaf != "off"
        config.VMAF_ASYNC = args.vmaf == "async"
    if args.no_vmaf_hwaccel:
        config.VMAF_HWACCEL = False
    if args.no_vmaf_hw_fallback:
        config.VMAF_HW_FALLBACK = False
    if args.keep_source:
        config.KEEP_SOURCE = True
    if args.vmaf_threads is not None:
        config.VMAF_N_THREADS = args.vmaf_threads
    if args.retry_failed:
        config.RETRY_FAILED = True
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
    """运行主监控循环：校验配置、启动异步 VMAF、扫描库目录并调度转码。

    循环内定期清理临时目录/过期记录，遍历各库目录，把应处理的文件提交到
    转码线程池。收到退出信号后优雅关闭并返回历史对象。

    Run the main monitoring loop: validate config, start async VMAF, scan the
    library directories and schedule transcodes. Cleans up temp dirs/stale records
    each cycle, submits processable files to the transcode pool, then shuts down
    gracefully on exit signal and returns the History object.
    """
    global _running

    history = History(config.HISTORY_FILE)
    ffscorer = FfmpegVMAFScorer(
        ffprobe_path=config.FFPROBE_PATH,
        vmaf_ffmpeg_path=config.VMAF_FFMPEG_PATH,
        vmaf_model_path=config.VMAF_MODEL_PATH,
        vmaf_log_dir=config.VMAF_LOG_DIR,
        n_threads=config.VMAF_N_THREADS,
        n_subsample=config.VMAF_N_SUBSAMPLE,
        vmaf_timeout=config.VMAF_TIMEOUT,
        vmaf_hwaccel=config.VMAF_HWACCEL,
        vmaf_hw_fallback=config.VMAF_HW_FALLBACK,
    )
    compute_vmaf = ffscorer.compute_vmaf

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
        start_vmaf_workers(compute_vmaf=compute_vmaf)
        recover_pending_vmaf(history, ffprobe_path=config.FFPROBE_PATH)
    log.info("=" * 50)

    # 转码池常驻整个进程生命周期：扫描周期独立于转码进度，
    # 新入库文件最多等待一个扫描间隔即可入队，不再排队等待批次转码完成。
    # Transcode pool lives for the whole process; scan cadence is decoupled from
    # transcode progress so newly added files queue up within one scan interval.
    executor = ThreadPoolExecutor(max_workers=config.MAX_WORKERS)
    _pending = set()

    def process_one(path, lib_info, force=False):
        if not _running:
            return
        try:
            if not force and not should_process(
                path, history, config.SKIP_CODECS, config.FFPROBE_PATH
            ):
                return
            transcode(
                path,
                history,
                config.FFMPEG_PATH,
                config.FFPROBE_PATH,
                lib_info["input"],
                lib_info.get("output"),
                lib_info.get("cache"),
                keep_source=config.KEEP_SOURCE,
                compute_vmaf=compute_vmaf,
            )
        except Exception:
            log.exception("UNHANDLED ERROR: %s", path)
        finally:
            _pending.discard(str(path))

    try:

        def requeue_entry(entries) -> None:
            """启动时把残留记录按 force=True 重新提交转码。

            供 iter_started()（无条件）与 iter_failed()（--retry-failed）使用：
            文件不属于任何库则跳过；否则入队去重集后提交，避免与扫描同名重复。
            依赖均在下方 process_one/_find_lib_for 定义处之后才调用，故定义于此。

            Requeue leftover history entries for transcoding with force=True. Used by
            iter_started() (unconditional) and iter_failed() (with --retry-failed):
            entries not owned by any library are skipped; others are added to the
            dedup set before submission to avoid clashing with the scan loop.
            """

            def _find_lib_for(path):
                """返回包含该文件路径的库配置，找不到返回 None。

                Return the library config whose input dir contains `path`, or None.
                """
                path = Path(path)
                for lib in config.LIBRARIES.values():
                    try:
                        path.relative_to(lib["input"])
                        return lib
                    except ValueError:
                        continue
                return None

            for entry in entries:
                path = Path(entry)
                lib_info = _find_lib_for(path)
                if lib_info is None:
                    log.warning("REQUEUE ENTRY: no library owns %s, skipping", path)
                    continue
                if str(path) in _pending:
                    continue
                _pending.add(str(path))
                executor.submit(process_one, path, lib_info, force=True)
                log.info("REQUEUE ENTRY (queued): %s", path)

        requeue_entry(history.iter_started())
        if config.RETRY_FAILED:
            requeue_entry(history.iter_failed())

        while _running:
            for lib_info in config.LIBRARIES.values():
                cleanup_stale_temp_dirs(
                    lib_info["cache"], config.TEMP_PREFIX, active_temp_dirs_snapshot()
                )
                if lib_info["cache"] != lib_info["input"]:
                    cleanup_stale_temp_dirs(
                        lib_info["input"], config.TEMP_PREFIX, active_temp_dirs_snapshot()
                    )

            history.purge_orphan_records()
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
    """程序入口：应用 CLI 覆盖、初始化日志、获取单实例锁并进入主循环。

    Entry point: apply CLI overrides, set up logging, acquire the single-instance
    lock, then enter the main loop.
    """
    _apply_cli_overrides()
    _setup_logging()

    config.ensure_libraries_conf()
    if not config.LIBRARIES:
        log.warning(
            "No libraries configured: edit %s (a sample template was created) "
            "or pass --library NAME=PATH",
            config.LIBRARIES_CONF_FILE,
        )

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

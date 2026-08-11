"""
异步 VMAF 打分模块。转码成功后不阻塞转码线程，而是把打分任务交给后台
工作线程执行，由结果决定产物的去留（ACCEPT/REJECT）。

Async VMAF scoring module. After a successful transcode the scoring task is
delegated to a background worker thread instead of blocking the transcode
thread; its result decides whether to keep or reject the output.
"""

import queue
import re
import threading
import time as _time
from pathlib import Path

from src import config
from src.quality import compute_vmaf, vmaf_verdict

log = __import__("logging").getLogger(__name__)

# 异步 VMAF 打分任务队列：任务元组 (source, output, ratio, safe_stem, history, codec_elapsed)。
# 无界队列，任务仅含路径元组不占内存；打分并发由 worker 数控制。
# Async VMAF scoring task queue; tasks are
# (source, output, ratio, safe_stem, history, codec_elapsed) tuples. Unbounded:
# entries are lightweight path tuples, concurrency is bounded by workers.
vmaf_tasks = queue.Queue()
_VMAF_STOP = object()
_vmaf_worker_threads = []
_vmaf_workers_lock = threading.Lock()
_vmaf_workers_started = False


def _handle_vmaf_task(task):
    """
    处理单个异步 VMAF 打分任务：打分后按 vmaf_verdict 返回的 verdict
    （retry/accept/reject）决策产物去留，并更新历史记录。

    Handle one async VMAF scoring task: score the output, then decide its fate
    based on vmaf_verdict's verdict (retry/accept/reject) and update history.
    """
    # 兼容缺少 codec_elapsed/keep_source 的短任务元组，缺失时补 None。
    # Tolerate short task tuples that omit codec_elapsed/keep_source (padded with None).
    source, output, ratio, safe_stem, history, codec_elapsed, keep_source = (
        task + (None, None)
    )[:7]
    source = Path(source)
    output = Path(output)

    vmaf_log = Path(config.VMAF_LOG_DIR) / f"{safe_stem}.{_time.strftime('%Y%m%d-%H%M%S')}.json"
    vmaf_start = _time.monotonic()
    vmaf_score = compute_vmaf(source, output, log_path=vmaf_log)
    vmaf_elapsed = _time.monotonic() - vmaf_start

    verdict = vmaf_verdict(vmaf_score, ratio)

    if verdict == "retry":
        # 打分本身失败（vmaf_score is None，如打分进程被打断/解码错误）或极端低分
        # （视为输入/产物异常，如写入中的残缺文件被误转码）：
        # 均记 failed 永久排除，仅 --retry-failed 启动时手动重试。
        # Scoring failures (None) and extreme-low scores both mark failed (permanently
        # excluded; retried only via --retry-failed).
        try:
            output.unlink()
        except OSError as e:
            log.error("VMAF REJECT CLEANUP FAILED: %s — %s", output.name, e)
            history.mark_failed(str(source), f"VMAF reject cleanup failed: {e}")
            return
        if vmaf_score is None:
            history.mark_failed(str(source), "VMAF scoring failed")
            log.warning(
                "VMAF SCORING FAILED (async): %s (ratio %.1f%%, marking failed)",
                output.name,
                ratio * 100,
            )
        else:
            history.mark_failed(
                str(source),
                f"VMAF {vmaf_score:.2f} below {config.VMAF_EXTREME_LOW}, input may be incomplete",
            )
            log.warning(
                "VMAF EXTREME LOW (async): %s (VMAF %.2f, ratio %.1f%%, marking failed)",
                output.name,
                vmaf_score,
                ratio * 100,
            )
        return

    if verdict == "accept":
        if not output.exists():
            log.error("VMAF ACCEPT FAILED: output missing for %s, marking failed", source.name)
            history.mark_failed(str(source), "VMAF accept failed: output missing")
            return
        try:
            if not keep_source:
                source.unlink()
        except OSError as e:
            log.error("VMAF ACCEPT CLEANUP FAILED: %s — %s", source.name, e)
            history.mark_failed(str(source), f"VMAF accept cleanup failed: {e}")
            return
        history.mark_done(str(source), str(output), vmaf_score=vmaf_score, rejected=False)
        log.info(
            "ACCEPT (async): %s (VMAF %.2f, ratio %.1f%%, codec %s + vmaf %.1fs)",
            output.name,
            vmaf_score or 0,
            ratio * 100,
            f"{codec_elapsed:.1f}" if codec_elapsed is not None else "N/A",
            vmaf_elapsed,
        )
        return

    # verdict == "reject"
    try:
        output.unlink()
    except OSError as e:
        log.error("VMAF REJECT CLEANUP FAILED: %s — %s", output.name, e)
        history.mark_failed(str(source), f"VMAF reject cleanup failed: {e}")
        return
    history.mark_done(str(source), None, vmaf_score=vmaf_score, rejected=True)
    log.warning(
        "REJECT (async): %s (VMAF %s, ratio %.1f%%, codec %s + vmaf %.1fs, keeping original)",
        output.name,
        f"{vmaf_score:.2f}" if vmaf_score is not None else "N/A",
        ratio * 100,
        f"{codec_elapsed:.1f}" if codec_elapsed is not None else "N/A",
        vmaf_elapsed,
    )


def _vmaf_worker():
    """
    后台 VMAF 打分 worker 循环：不断消费队列任务，直到收到停止哨兵。

    Background VMAF scoring worker loop: consumes tasks from the queue until it
    sees the stop sentinel.
    """
    while True:
        task = vmaf_tasks.get()
        if task is _VMAF_STOP:
            return
        try:
            _handle_vmaf_task(task)
        except Exception as e:
            log.error("VMAF WORKER ERROR: %s — %s", task[0], e, exc_info=True)


def start_vmaf_workers():
    """
    启动后台 VMAF 打分 worker（幂等：重复调用不会重复创建线程）。

    Start the background VMAF scoring workers (idempotent: repeated calls do not
    create additional threads).
    """
    global _vmaf_workers_started
    with _vmaf_workers_lock:
        if _vmaf_workers_started:
            return
        for _ in range(max(1, config.VMAF_WORKERS)):
            thread = threading.Thread(target=_vmaf_worker, name="vmaf-worker", daemon=True)
            thread.start()
            _vmaf_worker_threads.append(thread)
        _vmaf_workers_started = True
        log.info("Started %d async VMAF worker thread(s)", len(_vmaf_worker_threads))


def drain_vmaf_tasks():
    """
    优雅关闭：向每个 worker 投递停止哨兵并等待其退出，从而完成队列中剩余任务。

    Graceful shutdown: enqueue a stop sentinel for each worker and wait for them
    to exit, letting queued tasks finish first.
    """
    global _vmaf_workers_started
    with _vmaf_workers_lock:
        if not _vmaf_workers_started:
            return
        workers = list(_vmaf_worker_threads)
    for _ in workers:
        vmaf_tasks.put(_VMAF_STOP)
    for thread in workers:
        thread.join()
    with _vmaf_workers_lock:
        _vmaf_worker_threads.clear()
        _vmaf_workers_started = False
    log.info("Drained %d async VMAF worker thread(s)", len(workers))


def recover_pending_vmaf(history):
    """
    启动恢复：把历史中残留的 pending_vmaf 记录重新入队异步打分，处理 kill -9
    后遗留的双份（源与产物同时存在）情况。

    Startup recovery: re-enqueue pending_vmaf records left in history for async
    scoring, handling the duplicate-file case left behind by kill -9.
    """
    count = 0
    for filepath, output_path in history.iter_pending_vmaf():
        if not Path(output_path).exists():
            log.error("VMAF RECOVER: output missing for %s, marking failed", filepath)
            history.mark_failed(filepath, "Pending VMAF output missing on recovery")
            continue
        if not Path(filepath).exists():
            log.warning("VMAF RECOVER: source missing for %s, removing orphan output", filepath)
            try:
                Path(output_path).unlink()
            except OSError:
                pass
            history.mark_done(filepath, None, rejected=True)
            continue
        stem = Path(filepath).stem
        safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80]
        try:
            ratio = Path(output_path).stat().st_size / Path(filepath).stat().st_size
        except OSError:
            ratio = 0
        vmaf_tasks.put((filepath, output_path, ratio, safe_stem, history, None, config.KEEP_SOURCE))
        count += 1
    if count:
        log.info("Recovered %d pending VMAF task(s)", count)

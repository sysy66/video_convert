import queue
import re
import threading
import time as _time
from pathlib import Path

from src import config
from src.quality import compute_vmaf, should_accept

log = __import__("logging").getLogger(__name__)

# 异步 VMAF 打分任务队列：任务元组 (source, output, ratio, safe_stem, history)。
# 无界队列，任务仅含路径元组不占内存；打分并发由 worker 数控制。
# Async VMAF scoring task queue; tasks are (source, output, ratio, safe_stem, history)
# tuples. Unbounded: entries are lightweight path tuples, concurrency is bounded by workers.
vmaf_tasks = queue.Queue()
_VMAF_STOP = object()
_vmaf_worker_threads = []
_vmaf_workers_lock = threading.Lock()
_vmaf_workers_started = False


def _handle_vmaf_task(task):
    """处理单个异步 VMAF 打分任务：打分后按 should_accept 决策文件去留并标记历史。"""
    source, output, ratio, safe_stem, history = task
    source = Path(source)
    output = Path(output)

    vmaf_log = Path(config.VMAF_LOG_DIR) / f"{safe_stem}.{_time.strftime('%Y%m%d-%H%M%S')}.json"
    vmaf_start = _time.monotonic()
    vmaf_score = compute_vmaf(source, output, log_path=vmaf_log)
    vmaf_elapsed = _time.monotonic() - vmaf_start

    if should_accept(vmaf_score, ratio):
        if not output.exists():
            log.error("VMAF ACCEPT FAILED: output missing for %s, marking failed", source.name)
            history.mark_failed(str(source), "VMAF accept failed: output missing")
            return
        try:
            source.unlink()
        except OSError as e:
            log.error("VMAF ACCEPT CLEANUP FAILED: %s — %s", source.name, e)
            history.mark_failed(str(source), f"VMAF accept cleanup failed: {e}")
            return
        history.mark_done(str(source), str(output), vmaf_score=vmaf_score, rejected=False)
        log.info(
            "ACCEPT (async): %s (VMAF %.2f, ratio %.1f%%, %.1fs)",
            output.name,
            vmaf_score or 0,
            ratio * 100,
            vmaf_elapsed,
        )
    else:
        try:
            output.unlink()
        except OSError as e:
            log.error("VMAF REJECT CLEANUP FAILED: %s — %s", output.name, e)
            history.mark_failed(str(source), f"VMAF reject cleanup failed: {e}")
            return
        # 极端低分视为输入/产物异常（如写入中的残缺文件被误转码），
        # 记 failed 触发退避重试，而不是永久 rejected 封锁该文件。
        if vmaf_score is not None and vmaf_score < config.VMAF_EXTREME_LOW:
            history.mark_failed(
                str(source),
                f"VMAF {vmaf_score:.2f} below {config.VMAF_EXTREME_LOW}, input may be incomplete",
            )
            log.warning(
                "VMAF EXTREME LOW (async): %s (VMAF %.2f, ratio %.1f%%, marking failed for retry)",
                output.name,
                vmaf_score,
                ratio * 100,
            )
            return
        history.mark_done(str(source), None, vmaf_score=vmaf_score, rejected=True)
        log.warning(
            "REJECT (async): %s (VMAF %s, ratio %.1f%%, keeping original, %.1fs)",
            output.name,
            f"{vmaf_score:.2f}" if vmaf_score is not None else "N/A",
            ratio * 100,
            vmaf_elapsed,
        )


def _vmaf_worker():
    """后台 VMAF 打分 worker 循环：消费队列直到收到停止哨兵。"""
    while True:
        task = vmaf_tasks.get()
        if task is _VMAF_STOP:
            return
        try:
            _handle_vmaf_task(task)
        except Exception as e:
            log.error("VMAF WORKER ERROR: %s — %s", task[0], e, exc_info=True)


def start_vmaf_workers():
    """启动后台 VMAF 打分 worker（幂等，重复调用不产生新线程）。"""
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
    """优雅关闭：等待队列中所有打分任务完成并停止所有 worker 线程。"""
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
    """启动恢复：将残留的 pending_vmaf 记录重新入队打分（kill -9 后的双份文件）。"""
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
        vmaf_tasks.put((filepath, output_path, ratio, safe_stem, history))
        count += 1
    if count:
        log.info("Recovered %d pending VMAF task(s)", count)

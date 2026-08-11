"""
进程守卫模块。注册/注销活跃的 ffmpeg 子进程 PID，支持优雅关闭时终止全部
活跃 ffmpeg，并把上次启动残留的 started 状态标记为 failed。

Process guard module. Registers/unregisters active ffmpeg child PIDs, kills all
active ffmpeg processes on graceful shutdown, and marks leftover started states
from a previous run as failed.
"""

import os
import signal
import threading
import time as _time

log = __import__("logging").getLogger(__name__)

# 追踪所有活跃的 ffmpeg 子进程 PID，用于优雅关闭时清理孤儿进程
# Track all active ffmpeg child process PIDs for orphan cleanup on graceful shutdown
_active_ffmpeg_pids = set()
_active_ffmpeg_lock = threading.Lock()

# 追踪当前正在转码的文件路径，用于避免将本进程的 started 条目误判为残留
# Track filepaths currently being transcoded to avoid falsely flagging them as stale
_active_transcodes = set()
_active_transcodes_lock = threading.Lock()


def register_ffmpeg_pid(pid):
    """注册 ffmpeg 子进程 PID。Register an ffmpeg child process PID."""
    with _active_ffmpeg_lock:
        _active_ffmpeg_pids.add(pid)


def unregister_ffmpeg_pid(pid):
    """注销 ffmpeg 子进程 PID。Unregister an ffmpeg child process PID."""
    with _active_ffmpeg_lock:
        _active_ffmpeg_pids.discard(pid)


def kill_active_ffmpeg():
    """
    终止所有活跃的 ffmpeg 子进程（关闭流程调用，防止孤儿进程残留）。
    先 SIGTERM，2 秒后仍未退出则 SIGKILL，应对 NFS D-state 阻塞场景。

    Terminate all active ffmpeg child processes (called during shutdown to
    prevent orphan processes from lingering). Sends SIGTERM first, then
    SIGKILL after 2 seconds for stuck (D-state) processes.
    """
    with _active_ffmpeg_lock:
        pids = list(_active_ffmpeg_pids)
    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            log.info("Sent SIGTERM to ffmpeg PID %d", pid)
        except ProcessLookupError:
            pass
        except OSError as e:
            log.warning("Failed to kill ffmpeg PID %d: %s", pid, e)
    _time.sleep(2)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            log.info("Sent SIGKILL to ffmpeg PID %d", pid)
        except ProcessLookupError:
            pass
        except OSError as e:
            log.warning("Failed to SIGKILL ffmpeg PID %d: %s", pid, e)


def mark_stale_started_as_failed(history):
    """
    将 history 中所有 status == "started" 但本进程未在转码的条目标记为 failed。
    用于启动 / 每个扫描周期前清理停电残留，避免 stuck started 被 FULL SCAN 跳过。

    Mark all history entries with status "started" that are NOT currently being
    transcoded by this process as "failed". Handles stale entries left by power
    outages so they can be retried later via --retry-failed.

    :param history: 历史记录对象 (History object)
    """
    count = 0
    for fp in history.iter_started():
        with _active_transcodes_lock:
            if fp in _active_transcodes:
                continue
        history.mark_failed(fp, "Stale: process interrupted")
        count += 1
    if count:
        log.info("Marked %d stale 'started' entr%s as failed", count, "y" if count == 1 else "ies")

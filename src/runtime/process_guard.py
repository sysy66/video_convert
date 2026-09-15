"""
进程守卫模块。注册/注销活跃的 ffmpeg 子进程 PID，支持优雅关闭时终止全部活跃进程。

Process guard module. Registers/unregisters active ffmpeg child PIDs, kills all
active ffmpeg processes on graceful shutdown.
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

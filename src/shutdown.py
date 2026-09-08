"""
进程优雅关闭标志。置位后，被关停打断的后处理不再误标 failed，
记录保持原状态待下次启动重入队。

Graceful-shutdown flag. Once set, post-processing interrupted by shutdown
is not marked failed; its record keeps its state for requeue on next startup.
"""

_shutting_down = False


def signal_shutdown():
    """标记进程进入优雅关闭：此后被打断的后处理不记终态。"""
    global _shutting_down
    _shutting_down = True


def is_shutting_down():
    """是否处于优雅关闭中。"""
    return _shutting_down

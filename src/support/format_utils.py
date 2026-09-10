"""
显示格式工具。提供日志中使用的人类可读数值格式化函数。

Display-format helpers. Provides human-readable value formatting for logs.
"""


def _format_size(bytes_val):
    """
    将字节数格式化为人类可读的字符串（KB/MB/GB）。
    Format bytes into a human-readable string (KB/MB/GB).

    :param bytes_val: 字节数 (Number of bytes)
    :return: 格式化后的字符串，如 "1.23 GB" (Formatted string, e.g. "1.23 GB")
    """
    for unit in ("B", "KB", "MB", "GB"):
        if abs(bytes_val) < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"

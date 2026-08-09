"""
路径工具模块。提供路径唯一化（避免覆盖）、文件大小格式化与随机后缀生成等
通用工具函数。

Path utility module. Provides generic helpers for making paths unique (avoiding
overwrites), formatting file sizes, and generating random suffixes.
"""

import random
import string


def _unique_path(path):
    """
    如果路径已存在，自动追加编号（如 foo.mkv → foo.1.mkv），防止覆盖。
    If the path already exists, append a counter (e.g. foo.mkv → foo.1.mkv)
    to prevent overwriting.

    :param path: 期望的路径 (Desired path)
    :return: 不重复的路径 (A non-conflicting path)
    """
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        new = parent / f"{stem}.{counter}{suffix}"
        if not new.exists():
            return new
        counter += 1


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


def _random_suffix(length=6):
    """
    生成随机后缀，用于临时目录名称。
    Generate a random suffix for temporary directory names.

    :param length: 后缀长度 (Length of the suffix)
    :return: 随机字符串 (Random string)
    """
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=length))

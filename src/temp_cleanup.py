import os
import shutil
import subprocess
import threading
from pathlib import Path

log = __import__("logging").getLogger(__name__)

# 活跃临时工作目录（转码进行中），cleanup 必须跳过，防止删除正在使用的目录。
# Active temp work dirs; the stale-cleanup pass must skip these.
_active_temp_dirs = set()
_active_temp_dirs_lock = threading.Lock()


def active_temp_dirs_snapshot():
    """返回当前活跃临时目录的副本（供 cleanup 传入，避免持锁遍历）。"""
    with _active_temp_dirs_lock:
        return set(_active_temp_dirs)


def _cleanup_temp(temp_dir):
    """
    安全地删除临时工作目录（忽略不存在的情况）。
    Safely remove a temporary working directory (no-op if it does not exist).

    :param temp_dir: 临时目录的 Path 对象 (Path object of the temp directory)
    """
    try:
        if temp_dir.exists() and not _rmtree_robust(temp_dir):
            log.warning("Failed to clean up temp dir %s completely", temp_dir)
        else:
            log.debug("Cleaned up temp dir: %s", temp_dir)
    except OSError as e:
        log.warning("Failed to clean up temp dir %s: %s", temp_dir, e)


def _rmtree_robust(path):
    """
    尝试多种方式删除目录树，比 shutil.rmtree 更健壮（处理 NFS .nfs* 残留等情形）。
    Try multiple strategies to remove a directory tree, more robust than plain
    shutil.rmtree (handles NFS .nfs* leftovers, etc.).

    :param path: 要删除的目录路径 (Path to the directory to remove)
    :return: True 表示最终删除成功 (True if eventually successful)
    """
    # 策略 1：shutil.rmtree
    # Strategy 1: shutil.rmtree
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        pass

    # 策略 2：先逐个删除目录内文件，再重试 rmtree
    # Strategy 2: remove individual files first, then retry rmtree
    try:
        for entry in os.listdir(path):
            entry_path = path / entry
            try:
                if entry_path.is_file() or entry_path.is_symlink():
                    entry_path.unlink()
                elif entry_path.is_dir():
                    shutil.rmtree(entry_path, ignore_errors=True)
            except OSError:
                pass
        shutil.rmtree(path)
        return True
    except OSError:
        pass

    # 策略 3：subprocess rm -rf（处理某些内核级锁的情况）
    # Strategy 3: subprocess rm -rf (handles some kernel-level locks)
    try:
        subprocess.run(["rm", "-rf", str(path)], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        pass

    return False


def cleanup_stale_temp_dirs(watch_dir, temp_prefix, active_dirs=None):
    """
    递归扫描监视目录，清理所有残留的临时工作目录（通常因程序崩溃未清理）。
    使用 _rmtree_robust 增强对 NFS .nfs 残留、权限问题等容错。
    active_dirs 中的目录（当前正在转码）会被跳过。

    Recursively scan the watch directory and remove any stale temporary
    working directories (usually left behind by crashes). Directories listed
    in active_dirs (currently being transcoded) are skipped.
    Uses _rmtree_robust for better tolerance of NFS .nfs* leftovers,
    permission issues, etc.

    :param watch_dir: 监视目录的根路径 (Root path of the watched directory)
    :param temp_prefix: 临时目录的前缀 (Prefix of temporary directory names)
    :param active_dirs: 活跃临时目录集合（跳过不删）(Set of active temp dirs to skip)
    """
    count = 0
    active = set(active_dirs or ())
    for root, dirs, _ in os.walk(watch_dir):
        for d in list(dirs):
            if d.startswith(temp_prefix):
                full_path = Path(root) / d
                if str(full_path) in active:
                    continue
                if _rmtree_robust(full_path):
                    count += 1
                    log.info("Removed stale temp dir: %s", full_path)
                else:
                    log.warning(
                        "Failed to remove stale temp dir %s — "
                        "directory may contain NFS .nfs* locked files. "
                        "Try: lsof +D %s  to find the holding process.",
                        full_path,
                        full_path,
                    )
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith(temp_prefix)]
    if count:
        log.info("Cleaned up %d stale temp director%s", count, "ies" if count != 1 else "y")

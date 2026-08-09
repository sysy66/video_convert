"""
配置校验模块。启动前检查配置中的路径、可执行文件、目录权限等是否有效，
返回错误与警告列表。

Config validation module. Checks before startup whether configured paths,
executables and directory permissions are valid, returning lists of errors and
warnings.
"""

import os
import shutil

from src import config

log = __import__("logging").getLogger(__name__)


def _check_path(label, path, must_exist=True, must_be_writable=False, must_be_executable=False):
    """校验单个路径：存在性、可写性、可执行性，返回错误消息列表。

    Validate a single path for existence, writability and executability,
    returning a list of error messages.

    :param label: 路径用途描述 (Description of the path)
    :param path: 待校验的路径 (Path to validate)
    :param must_exist: 是否必须存在 (Whether it must exist)
    :param must_be_writable: 是否必须可写 (Whether it must be writable)
    :param must_be_executable: 是否必须可执行 (Whether it must be executable)
    :return: (errors, warnings) 元组 (A tuple of (errors, warnings))
    """
    errors = []
    warnings = []
    if must_exist and not os.path.exists(path):
        errors.append(f"{label} does not exist: {path}")
        return errors, warnings
    if must_exist and not os.path.isfile(path):
        errors.append(f"{label} is not a file: {path}")
        return errors, warnings
    if must_be_executable and not os.access(path, os.X_OK):
        errors.append(f"{label} not executable: {path}")
    if must_be_writable and not os.access(path, os.W_OK):
        errors.append(f"{label} not writable: {path}")
    return errors, warnings


def validate_config():
    """校验全部配置项：库路径、ffmpeg/ffprobe、VMAF 依赖、数据目录与磁盘空间。

    Validate all configuration: library paths, ffmpeg/ffprobe, VMAF dependencies,
    data directories and disk space.

    :return: (errors, warnings) 元组，errors 非空时主程序将退出 (A (errors, warnings)
             tuple; the program exits if errors is non-empty)
    """
    errors = []
    warnings = []

    # 1. Library paths
    for name, info in config.LIBRARIES.items():
        for key in ("input", "output", "cache"):
            p = info[key]
            if not os.path.exists(p):
                if key == "cache":
                    warnings.append(
                        f"Library [{name}] cache does not exist (will be created on demand): {p}"
                    )
                else:
                    errors.append(f"Library [{name}] {key} does not exist: {p}")
            elif not os.access(p, os.R_OK):
                errors.append(f"Library [{name}] {key} not readable: {p}")
            elif key in ("output", "cache") and not os.access(p, os.W_OK):
                errors.append(f"Library [{name}] {key} not writable: {p}")

    # 2. FFmpeg / FFprobe executables
    for label, path in [("FFMPEG_PATH", config.FFMPEG_PATH), ("FFPROBE_PATH", config.FFPROBE_PATH)]:
        e, w = _check_path(label, path, must_exist=True, must_be_executable=True)
        errors.extend(e)
        warnings.extend(w)

    # 3. VMAF dependencies (error if VMAF_REQUIRED or VMAF_ASYNC)
    if config.VMAF_REQUIRED or config.VMAF_ASYNC:
        if not os.path.isfile(config.VMAF_FFMPEG_PATH):
            errors.append(
                f"VMAF_REQUIRED is True but VMAF_FFMPEG_PATH not found: {config.VMAF_FFMPEG_PATH}"
            )
        elif not os.access(config.VMAF_FFMPEG_PATH, os.X_OK):
            errors.append(f"VMAF_FFMPEG_PATH not executable: {config.VMAF_FFMPEG_PATH}")
        if config.VMAF_MODEL_PATH and not os.path.isfile(config.VMAF_MODEL_PATH):
            errors.append(
                f"VMAF_MODEL_PATH not found (VMAF will be unavailable): {config.VMAF_MODEL_PATH}"
            )

    # 4. Data directories — create if needed
    for label, path in [("LOG_FILE", config.LOG_FILE), ("HISTORY_FILE", config.HISTORY_FILE)]:
        d = os.path.dirname(path)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            errors.append(f"Cannot create directory for {label} ({d}): {e}")
    try:
        os.makedirs(config.VMAF_LOG_DIR, exist_ok=True)
    except OSError as e:
        warnings.append(f"Cannot create VMAF_LOG_DIR ({config.VMAF_LOG_DIR}): {e}")

    # 5. Disk space check on output + cache partitions
    min_space_error = 1 * 1024**3
    min_space_warn = 5 * 1024**3
    checked = set()
    for lib_info in config.LIBRARIES.values():
        for key in ("output", "cache"):
            p = lib_info[key]
            if p in checked:
                continue
            checked.add(p)
            try:
                usage = shutil.disk_usage(p)
                free_gb = usage.free / 1024**3
                if usage.free < min_space_error:
                    errors.append(f"Critical disk space on {p}: {free_gb:.1f} GiB free (< 1 GiB)")
                elif usage.free < min_space_warn:
                    warnings.append(f"Low disk space on {p}: {free_gb:.1f} GiB free (< 5 GiB)")
            except OSError:
                warnings.append(f"Cannot check disk space for {p}")

    return errors, warnings

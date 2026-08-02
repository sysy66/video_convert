import os
import shutil

from src import config

log = __import__("logging").getLogger(__name__)


def _check_path(label, path, must_exist=True, must_be_writable=False, must_be_executable=False):
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
                f"VMAF_MODEL_PATH not found (built-in model will be used): {config.VMAF_MODEL_PATH}"
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

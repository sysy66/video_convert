"""
路径工具模块。提供路径唯一化（避免覆盖）与输出目标路径计算。

Path utility module. Provides helpers for making paths unique (avoiding
overwrites) and computing output destination paths.
"""

from pathlib import Path


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


def _final_dest_path(filepath, output_filename, input_dir, output_dir):
    """
    计算输出区目标路径：按需保持 input_dir 下的相对目录结构，目标重名时自动编号。
    Compute the destination path in the output area: mirror the relative structure
    under input_dir when applicable, and auto-number if the name collides.
    """
    if output_dir:
        output_dir = Path(output_dir)
        if input_dir:
            input_dir = Path(input_dir)
            try:
                rel = filepath.parent.relative_to(input_dir)
            except ValueError:
                rel = Path()
            dest_dir = output_dir / rel
        else:
            dest_dir = output_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        final_path = _unique_path(dest_dir / output_filename)
    else:
        final_path = _unique_path(filepath.parent / output_filename)
    return final_path

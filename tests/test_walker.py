"""walker 模块单元测试：目录遍历与扩展名过滤。

Unit tests for the walker module: directory walking and extension filtering.
"""

import pytest

from src.ingest.walker import walk_directory

pytestmark = pytest.mark.unit


def _make_tree(root, files):
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("content")


def test_empty_directory(tmp_path):
    assert list(walk_directory(str(tmp_path), {".mp4"})) == []


def test_nonexistent_directory(tmp_path):
    result = list(walk_directory(str(tmp_path / "nonexistent"), {".mp4"}))
    assert result == []


def test_extension_filtering(tmp_path):
    _make_tree(tmp_path, {"a.mp4": "", "b.mkv": "", "c.txt": "", "d.jpg": ""})
    result = list(walk_directory(str(tmp_path), {".mp4", ".mkv"}))
    assert len(result) == 2
    names = {p.name for p in result}
    assert names == {"a.mp4", "b.mkv"}


def test_single_extension(tmp_path):
    _make_tree(tmp_path, {"a.mp4": "", "b.mkv": ""})
    result = list(walk_directory(str(tmp_path), {".mp4"}))
    assert len(result) == 1
    assert result[0].name == "a.mp4"


def test_skips_hidden_files(tmp_path):
    _make_tree(tmp_path, {".hidden.mp4": "", "normal.mp4": ""})
    result = list(walk_directory(str(tmp_path), {".mp4"}))
    assert len(result) == 1
    assert result[0].name == "normal.mp4"


def test_skips_hidden_directories(tmp_path):
    _make_tree(tmp_path, {".hidden/v.mp4": "", "visible/v.mp4": ""})
    result = list(walk_directory(str(tmp_path), {".mp4"}))
    assert len(result) == 1
    assert "visible" in str(result[0])


def test_skips_temp_directories(tmp_path):
    _make_tree(tmp_path, {".work_abc/v.mp4": "", "normal/v.mp4": ""})
    result = list(walk_directory(str(tmp_path), {".mp4"}))
    assert len(result) == 1
    assert "normal" in str(result[0])


def test_recursive_discovery(tmp_path):
    _make_tree(tmp_path, {"a.mp4": "", "sub/b.mp4": "", "sub/sub/c.mp4": ""})
    result = list(walk_directory(str(tmp_path), {".mp4"}))
    assert len(result) == 3

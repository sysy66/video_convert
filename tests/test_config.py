"""config 模块单元测试：库映射解析与 libraries.conf 加载。

Unit tests for the config module: library mapping parsing and conf loading.
"""

import pytest

from src.config import _normalize_libraries, load_libraries_from_conf

pytestmark = pytest.mark.unit


class TestNormalizeLibraries:
    def test_string_shortcut_uses_same_path(self):
        raw = {"lib": "/mnt/lib1"}
        assert _normalize_libraries(raw) == {
            "lib": {"input": "/mnt/lib1", "output": "/mnt/lib1", "cache": "/mnt/lib1"}
        }

    def test_dict_full_fields(self):
        raw = {"lib": {"input": "/mnt/in", "output": "/mnt/out", "cache": "/mnt/cache"}}
        assert _normalize_libraries(raw) == {
            "lib": {"input": "/mnt/in", "output": "/mnt/out", "cache": "/mnt/cache"}
        }

    def test_dict_missing_fields_default_to_input(self):
        raw = {"lib": {"input": "/mnt/in"}}
        assert _normalize_libraries(raw) == {
            "lib": {"input": "/mnt/in", "output": "/mnt/in", "cache": "/mnt/in"}
        }

    def test_mixed_entry_types(self):
        raw = {"a": "/mnt/a", "b": {"input": "/mnt/b", "output": "/mnt/b_out"}}
        assert _normalize_libraries(raw) == {
            "a": {"input": "/mnt/a", "output": "/mnt/a", "cache": "/mnt/a"},
            "b": {"input": "/mnt/b", "output": "/mnt/b_out", "cache": "/mnt/b"},
        }

    def test_invalid_entry_type_raises(self):
        with pytest.raises(TypeError, match="Invalid LIBRARIES entry"):
            _normalize_libraries({"bad": 42})

    def test_missing_input_in_dict_raises(self):
        with pytest.raises(KeyError):
            _normalize_libraries({"bad": {"output": "/mnt/out"}})

    def test_empty_dict(self):
        assert _normalize_libraries({}) == {}


class TestLoadLibrariesFromConf:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_libraries_from_conf(str(tmp_path / "nope.conf")) == {}

    def test_single_path(self, tmp_path):
        p = tmp_path / "libs.conf"
        p.write_text("movies = /mnt/movies\n")
        assert load_libraries_from_conf(str(p)) == {"movies": "/mnt/movies"}

    def test_three_part_path(self, tmp_path):
        p = tmp_path / "libs.conf"
        p.write_text("shows = /mnt/shows, /mnt/out, /mnt/cache\n")
        assert load_libraries_from_conf(str(p)) == {
            "shows": {"input": "/mnt/shows", "output": "/mnt/out", "cache": "/mnt/cache"}
        }

    def test_comments_and_blank_lines_ignored(self, tmp_path):
        p = tmp_path / "libs.conf"
        p.write_text(
            "# comment\n\n"
            "movies = /mnt/movies\n"
            "# another\n"
            "shorts = /mnt/shorts, /mnt/out, /mnt/cache\n"
        )
        assert load_libraries_from_conf(str(p)) == {
            "movies": "/mnt/movies",
            "shorts": {"input": "/mnt/shorts", "output": "/mnt/out", "cache": "/mnt/cache"},
        }

    def test_malformed_line_raises(self, tmp_path):
        p = tmp_path / "libs.conf"
        p.write_text("bad = /a, /b\n")
        with pytest.raises(ValueError, match="invalid value"):
            load_libraries_from_conf(str(p))

    def test_missing_separator_raises(self, tmp_path):
        p = tmp_path / "libs.conf"
        p.write_text("noeq\n")
        with pytest.raises(ValueError, match="malformed line"):
            load_libraries_from_conf(str(p))
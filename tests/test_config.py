import pytest

from src.config import _normalize_libraries

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

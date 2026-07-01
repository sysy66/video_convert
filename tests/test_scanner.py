from pathlib import Path
from unittest.mock import patch, MagicMock

from src.scanner import scan


def _make_tree(tmp_path, files):
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("content")


def _no_history():
    h = MagicMock()
    h.exists.return_value = False
    return h


class TestScannerBasic:
    def test_empty_directory(self, tmp_path):
        result = scan(str(tmp_path), {".mp4"}, set(), "/ffprobe", _no_history(), False)
        assert result == []

    def test_nonexistent_directory(self, tmp_path):
        result = scan(str(tmp_path / "nonexistent"), {".mp4"}, set(), "/ffprobe", _no_history(), False)
        assert result == []

    def test_extension_filtering(self, tmp_path):
        _make_tree(tmp_path, {"a.mp4": "", "b.mkv": "", "c.txt": "", "d.jpg": ""})
        with patch("src.scanner.get_video_codec", return_value="h264"):
            result = scan(str(tmp_path), {".mp4", ".mkv"}, set(), "/ffprobe", _no_history(), False)
        assert len(result) == 2
        names = {p.name for p in result}
        assert names == {"a.mp4", "b.mkv"}


class TestScannerHidden:
    def test_skips_hidden_files(self, tmp_path):
        _make_tree(tmp_path, {".hidden.mp4": "", "visible.mp4": ""})
        with patch("src.scanner.get_video_codec", return_value="h264"):
            result = scan(str(tmp_path), {".mp4"}, set(), "/ffprobe", _no_history(), False)
        assert len(result) == 1
        assert result[0].name == "visible.mp4"

    def test_skips_hidden_directories(self, tmp_path):
        _make_tree(tmp_path, {".hidden/v.mp4": "", "normal/v.mp4": ""})
        with patch("src.scanner.get_video_codec", return_value="h264"):
            result = scan(str(tmp_path), {".mp4"}, set(), "/ffprobe", _no_history(), False)
        assert len(result) == 1
        assert "normal" in str(result[0])

    def test_skips_temp_directories(self, tmp_path):
        _make_tree(tmp_path, {".work_abc/v.mp4": "", "normal/v.mp4": ""})
        with patch("src.scanner.get_video_codec", return_value="h264"):
            result = scan(str(tmp_path), {".mp4"}, set(), "/ffprobe", _no_history(), False)
        assert len(result) == 1
        assert "normal" in str(result[0])


class TestScannerHistoryFiltering:
    def test_incremental_skips_existing_files(self, tmp_path):
        _make_tree(tmp_path, {"a.mp4": "", "b.mp4": ""})
        history = MagicMock()
        history.exists.return_value = True
        with patch("src.scanner.get_video_codec", return_value="h264"):
            result = scan(str(tmp_path), {".mp4"}, set(), "/ffprobe", history, False)
        assert result == []

    def test_incremental_skips_existing_but_picks_new(self, tmp_path):
        _make_tree(tmp_path, {"a.mp4": "", "b.mp4": ""})
        history = MagicMock()
        history.exists.side_effect = lambda p: p.endswith("a.mp4")
        with patch("src.scanner.get_video_codec", return_value="h264"):
            result = scan(str(tmp_path), {".mp4"}, set(), "/ffprobe", history, False)
        assert len(result) == 1
        assert result[0].name == "b.mp4"

    def test_full_scan_retries_failed_entries(self, tmp_path):
        _make_tree(tmp_path, {"a.mp4": ""})
        history = MagicMock()
        history.is_done.return_value = False
        history.is_started.return_value = False
        with patch("src.scanner.get_video_codec", return_value="h264"):
            result = scan(str(tmp_path), {".mp4"}, set(), "/ffprobe", history, True)
        assert len(result) == 1

    def test_full_scan_skips_done_entries(self, tmp_path):
        _make_tree(tmp_path, {"a.mp4": ""})
        history = MagicMock()
        history.is_done.return_value = True
        history.is_started.return_value = False
        with patch("src.scanner.get_video_codec", return_value="h264"):
            result = scan(str(tmp_path), {".mp4"}, set(), "/ffprobe", history, True)
        assert result == []

    def test_full_scan_skips_started_entries(self, tmp_path):
        _make_tree(tmp_path, {"a.mp4": ""})
        history = MagicMock()
        history.is_done.return_value = False
        history.is_started.return_value = True
        with patch("src.scanner.get_video_codec", return_value="h264"):
            result = scan(str(tmp_path), {".mp4"}, set(), "/ffprobe", history, True)
        assert result == []


class TestScannerCodecFilter:
    def test_skips_hevc(self, tmp_path):
        _make_tree(tmp_path, {"v.mp4": ""})
        with patch("src.scanner.get_video_codec", return_value="hevc"):
            result = scan(str(tmp_path), {".mp4"}, {"hevc", "av1"}, "/ffprobe", _no_history(), False)
        assert result == []

    def test_skips_av1(self, tmp_path):
        _make_tree(tmp_path, {"v.mp4": ""})
        with patch("src.scanner.get_video_codec", return_value="av1"):
            result = scan(str(tmp_path), {".mp4"}, {"hevc", "av1"}, "/ffprobe", _no_history(), False)
        assert result == []

    def test_allows_h264(self, tmp_path):
        _make_tree(tmp_path, {"v.mp4": ""})
        with patch("src.scanner.get_video_codec", return_value="h264"):
            result = scan(str(tmp_path), {".mp4"}, {"hevc", "av1"}, "/ffprobe", _no_history(), False)
        assert len(result) == 1

    def test_skip_on_probe_failure(self, tmp_path):
        _make_tree(tmp_path, {"v.mp4": ""})
        with patch("src.scanner.get_video_codec", return_value=None):
            result = scan(str(tmp_path), {".mp4"}, {"hevc", "av1"}, "/ffprobe", _no_history(), False)
        assert result == []


class TestScannerSorting:
    def test_returns_sorted_by_mtime(self, tmp_path):
        _make_tree(tmp_path, {"a.mp4": "", "b.mp4": "", "c.mp4": ""})
        with patch("src.scanner.get_video_codec", return_value="h264"):
            result = scan(str(tmp_path), {".mp4"}, set(), "/ffprobe", _no_history(), False)
        mtimes = [p.stat().st_mtime for p in result]
        assert mtimes == sorted(mtimes)

    def test_recursive_discovery(self, tmp_path):
        _make_tree(tmp_path, {"a.mp4": "", "sub/b.mp4": "", "sub/sub/c.mp4": ""})
        with patch("src.scanner.get_video_codec", return_value="h264"):
            result = scan(str(tmp_path), {".mp4"}, set(), "/ffprobe", _no_history(), False)
        assert len(result) == 3

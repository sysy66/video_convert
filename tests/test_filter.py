"""filter 模块单元测试：文件提交与处理过滤逻辑。

Unit tests for the filter module: file submission and processing filtering.
"""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.filter import should_process, should_submit

pytestmark = pytest.mark.unit


def _mock_history(entry=None):
    h = MagicMock()
    h.get_entry.return_value = entry
    return h


def _real_file(tmp_path, name="v.mp4"):
    p = tmp_path / name
    p.write_bytes(b"x" * 100)
    return p


def test_new_file_passes(tmp_path):
    h = _mock_history(None)
    with patch("src.filter.get_video_codec", return_value="h264"):
        assert should_process(_real_file(tmp_path), h, {"hevc"}, "/fp")


def test_probe_failure_queues_anyway(tmp_path):
    h = _mock_history(None)
    with patch("src.filter.get_video_codec", return_value=None):
        assert should_process(_real_file(tmp_path), h, {"hevc"}, "/fp")


def test_missing_file_skips_without_probe(tmp_path):
    h = _mock_history(None)
    with patch("src.filter.get_video_codec") as mock_probe:
        assert not should_process(tmp_path / "gone.mp4", h, {"hevc"}, "/fp")
    mock_probe.assert_not_called()


def test_skip_hevc(tmp_path):
    h = _mock_history(None)
    with patch("src.filter.get_video_codec", return_value="hevc"):
        assert not should_process(_real_file(tmp_path), h, {"hevc", "av1"}, "/fp")
        h.mark_skipped.assert_called_once()


def test_skip_av1(tmp_path):
    h = _mock_history(None)
    with patch("src.filter.get_video_codec", return_value="av1"):
        assert not should_process(_real_file(tmp_path), h, {"hevc", "av1"}, "/fp")
        h.mark_skipped.assert_called_once()


def test_allows_h264(tmp_path):
    h = _mock_history(None)
    with patch("src.filter.get_video_codec", return_value="h264"):
        assert should_process(_real_file(tmp_path), h, {"hevc", "av1"}, "/fp")


def test_skips_success():
    assert not should_process(Path("/v.mp4"), _mock_history({"status": "success"}), {"hevc"}, "/fp")


def test_skips_rejected():
    assert not should_process(
        Path("/v.mp4"), _mock_history({"status": "rejected"}), {"hevc"}, "/fp"
    )


def test_skips_not_required():
    assert not should_process(
        Path("/v.mp4"), _mock_history({"status": "not required"}), {"hevc"}, "/fp"
    )


def test_skips_started():
    assert not should_process(Path("/v.mp4"), _mock_history({"status": "started"}), {"hevc"}, "/fp")


def test_failed_skips():
    assert not should_process(Path("/v.mp4"), _mock_history({"status": "failed"}), {"hevc"}, "/fp")


def test_ready_with_hevc_skips(tmp_path):
    entry = {"status": "ready", "codec": "hevc"}
    with patch("src.filter.get_video_codec", return_value="hevc"):
        assert not should_process(
            _real_file(tmp_path), _mock_history(entry), {"hevc", "av1"}, "/fp"
        )


def test_ready_with_h264_passes(tmp_path):
    entry = {"status": "ready", "codec": "h264"}
    with patch("src.filter.get_video_codec", return_value="h264"):
        assert should_process(_real_file(tmp_path), _mock_history(entry), {"hevc", "av1"}, "/fp")


def test_ready_with_none_codec_passes(tmp_path):
    entry = {"status": "ready", "codec": None}
    with patch("src.filter.get_video_codec", return_value=None):
        assert should_process(_real_file(tmp_path), _mock_history(entry), {"hevc", "av1"}, "/fp")


def test_should_submit_new_file():
    assert should_submit(Path("/v.mp4"), _mock_history(None))


def test_should_submit_skips_success():
    assert not should_submit(Path("/v.mp4"), _mock_history({"status": "success"}))


def test_should_submit_skips_rejected():
    assert not should_submit(Path("/v.mp4"), _mock_history({"status": "rejected"}))


def test_should_submit_skips_not_required():
    assert not should_submit(Path("/v.mp4"), _mock_history({"status": "not required"}))


def test_should_submit_skips_started():
    assert not should_submit(Path("/v.mp4"), _mock_history({"status": "started"}))


def test_should_submit_skips_pending_vmaf():
    assert not should_submit(Path("/v.mp4"), _mock_history({"status": "pending_vmaf"}))


def test_should_submit_skips_failed():
    assert not should_submit(Path("/v.mp4"), _mock_history({"status": "failed"}))


def _stable_file(tmp_path, age_seconds):
    p = tmp_path / "movie.mp4"
    p.write_bytes(b"x" * 100)
    os.utime(p, (time.time() - age_seconds, time.time() - age_seconds))
    return p


def test_should_submit_skips_recently_modified(tmp_path):
    p = _stable_file(tmp_path, 10)
    with patch("src.filter.config.FILE_STABLE_WINDOW", 300):
        assert not should_submit(p, _mock_history(None))


def test_should_submit_passes_stable_file(tmp_path):
    p = _stable_file(tmp_path, 3600)
    with patch("src.filter.config.FILE_STABLE_WINDOW", 300):
        assert should_submit(p, _mock_history(None))


def test_should_submit_stat_failure_does_not_block(tmp_path):
    p = tmp_path / "gone.mp4"
    with patch("src.filter.config.FILE_STABLE_WINDOW", 300):
        assert should_submit(p, _mock_history(None))


def test_should_submit_stability_checked_after_history(tmp_path):
    p = _stable_file(tmp_path, 10)
    with patch("src.filter.config.FILE_STABLE_WINDOW", 300):
        assert not should_submit(p, _mock_history({"status": "success"}))

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.filter import should_process, should_submit

pytestmark = pytest.mark.unit


def _mock_history(entry=None):
    h = MagicMock()
    h.get_entry.return_value = entry
    return h


def test_new_file_passes():
    h = _mock_history(None)
    with patch("src.filter.get_video_codec", return_value="h264"):
        assert should_process(Path("/v.mp4"), h, {"hevc"}, "/fp")


def test_probe_failure_queues_anyway():
    h = _mock_history(None)
    with patch("src.filter.get_video_codec", return_value=None):
        assert should_process(Path("/v.mp4"), h, {"hevc"}, "/fp")


def test_skip_hevc():
    h = _mock_history(None)
    with patch("src.filter.get_video_codec", return_value="hevc"):
        assert not should_process(Path("/v.mp4"), h, {"hevc", "av1"}, "/fp")
        h.mark_skipped.assert_called_once()


def test_skip_av1():
    h = _mock_history(None)
    with patch("src.filter.get_video_codec", return_value="av1"):
        assert not should_process(Path("/v.mp4"), h, {"hevc", "av1"}, "/fp")
        h.mark_skipped.assert_called_once()


def test_allows_h264():
    h = _mock_history(None)
    with patch("src.filter.get_video_codec", return_value="h264"):
        assert should_process(Path("/v.mp4"), h, {"hevc", "av1"}, "/fp")


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


def test_failed_during_cooldown():
    entry = {"status": "failed", "completed_at": 0, "retry_count": 0}
    with (
        patch("src.filter.config.FAILED_RETRY_BASE", 3600),
        patch("src.filter.time.time", return_value=10),
    ):
        assert not should_process(Path("/v.mp4"), _mock_history(entry), {"hevc"}, "/fp")


def test_failed_after_cooldown():
    entry = {"status": "failed", "completed_at": 0, "retry_count": 0}
    with (
        patch("src.filter.config.FAILED_RETRY_BASE", 3600),
        patch("src.filter.time.time", return_value=4000),
    ):
        assert should_process(Path("/v.mp4"), _mock_history(entry), {"hevc"}, "/fp")


def test_failed_exhausted_retries():
    entry = {"status": "failed", "completed_at": 0, "retry_count": 0}
    h = _mock_history(entry)
    with (
        patch("src.filter.config.FAILED_RETRY_BASE", 3600),
        patch("src.filter.config.FAILED_RETRY_MAX_RETRIES", 0),
    ):
        assert not should_process(Path("/v.mp4"), h, {"hevc"}, "/fp")
        h.mark_exhausted.assert_called_once()


def test_ready_with_hevc_skips():
    entry = {"status": "ready", "codec": "hevc"}
    with patch("src.filter.get_video_codec", return_value="hevc"):
        assert not should_process(Path("/v.mp4"), _mock_history(entry), {"hevc", "av1"}, "/fp")


def test_ready_with_h264_passes():
    entry = {"status": "ready", "codec": "h264"}
    with patch("src.filter.get_video_codec", return_value="h264"):
        assert should_process(Path("/v.mp4"), _mock_history(entry), {"hevc", "av1"}, "/fp")


def test_ready_with_none_codec_passes():
    entry = {"status": "ready", "codec": None}
    with patch("src.filter.get_video_codec", return_value=None):
        assert should_process(Path("/v.mp4"), _mock_history(entry), {"hevc", "av1"}, "/fp")


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


def test_should_submit_failed_during_cooldown():
    entry = {"status": "failed", "completed_at": 0, "retry_count": 0}
    with (
        patch("src.filter.config.FAILED_RETRY_BASE", 3600),
        patch("src.filter.time.time", return_value=10),
    ):
        assert not should_submit(Path("/v.mp4"), _mock_history(entry))


def test_should_submit_failed_after_cooldown():
    entry = {"status": "failed", "completed_at": 0, "retry_count": 0}
    with (
        patch("src.filter.config.FAILED_RETRY_BASE", 3600),
        patch("src.filter.time.time", return_value=4000),
    ):
        assert should_submit(Path("/v.mp4"), _mock_history(entry))


def test_should_submit_exhausted_marks_history():
    entry = {"status": "failed", "completed_at": 0, "retry_count": 0}
    h = _mock_history(entry)
    with (
        patch("src.filter.config.FAILED_RETRY_BASE", 3600),
        patch("src.filter.config.FAILED_RETRY_MAX_RETRIES", 0),
    ):
        assert not should_submit(Path("/v.mp4"), h)
        h.mark_exhausted.assert_called_once()

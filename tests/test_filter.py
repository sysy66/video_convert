import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.filter import should_process


def _mock_history(entry=None):
    h = MagicMock()
    h.get_entry.return_value = entry
    return h


class TestFilterNewFiles(unittest.TestCase):
    def test_new_file_passes(self):
        h = _mock_history(None)
        with patch("src.filter.get_video_codec", return_value="h264"):
            self.assertTrue(should_process(Path("/v.mp4"), h, {"hevc"}, "/fp"))

    def test_probe_failure_queues_anyway(self):
        h = _mock_history(None)
        with patch("src.filter.get_video_codec", return_value=None):
            self.assertTrue(should_process(Path("/v.mp4"), h, {"hevc"}, "/fp"))

    def test_skip_hevc(self):
        h = _mock_history(None)
        with patch("src.filter.get_video_codec", return_value="hevc"):
            self.assertFalse(should_process(Path("/v.mp4"), h, {"hevc", "av1"}, "/fp"))
            h.mark_skipped.assert_called_once()

    def test_skip_av1(self):
        h = _mock_history(None)
        with patch("src.filter.get_video_codec", return_value="av1"):
            self.assertFalse(should_process(Path("/v.mp4"), h, {"hevc", "av1"}, "/fp"))
            h.mark_skipped.assert_called_once()

    def test_allows_h264(self):
        h = _mock_history(None)
        with patch("src.filter.get_video_codec", return_value="h264"):
            self.assertTrue(should_process(Path("/v.mp4"), h, {"hevc", "av1"}, "/fp"))


class TestFilterHistoryTerminal(unittest.TestCase):
    def test_skips_success(self):
        self.assertFalse(
            should_process(Path("/v.mp4"), _mock_history({"status": "success"}), {"hevc"}, "/fp"),
        )

    def test_skips_rejected(self):
        self.assertFalse(
            should_process(Path("/v.mp4"), _mock_history({"status": "rejected"}), {"hevc"}, "/fp"),
        )

    def test_skips_not_required(self):
        self.assertFalse(
            should_process(Path("/v.mp4"), _mock_history({"status": "not required"}), {"hevc"}, "/fp"),
        )

    def test_skips_started(self):
        self.assertFalse(
            should_process(Path("/v.mp4"), _mock_history({"status": "started"}), {"hevc"}, "/fp"),
        )


class TestFilterFailed(unittest.TestCase):
    def test_during_cooldown(self):
        entry = {"status": "failed", "completed_at": 0, "retry_count": 0}
        with (
            patch("src.filter.config.FAILED_RETRY_BASE", 3600),
            patch("src.filter.time.time", return_value=10),
        ):
            self.assertFalse(
                should_process(Path("/v.mp4"), _mock_history(entry), {"hevc"}, "/fp"),
            )

    def test_after_cooldown(self):
        entry = {"status": "failed", "completed_at": 0, "retry_count": 0}
        with (
            patch("src.filter.config.FAILED_RETRY_BASE", 3600),
            patch("src.filter.time.time", return_value=4000),
        ):
            self.assertTrue(
                should_process(Path("/v.mp4"), _mock_history(entry), {"hevc"}, "/fp"),
            )

    def test_exhausted_retries(self):
        entry = {"status": "failed", "completed_at": 0, "retry_count": 99}
        h = _mock_history(entry)
        with (
            patch("src.filter.config.FAILED_RETRY_BASE", 3600),
            patch("src.filter.config.FAILED_RETRY_MAX", 7200),
        ):
            self.assertFalse(should_process(Path("/v.mp4"), h, {"hevc"}, "/fp"))
            h.mark_exhausted.assert_called_once()


class TestFilterReady(unittest.TestCase):
    @patch("src.filter.get_video_codec", return_value="hevc")
    def test_ready_with_hevc_skips(self, mock_probe):
        entry = {"status": "ready", "codec": "hevc"}
        self.assertFalse(
            should_process(Path("/v.mp4"), _mock_history(entry), {"hevc", "av1"}, "/fp"),
        )

    @patch("src.filter.get_video_codec", return_value="h264")
    def test_ready_with_h264_passes(self, mock_probe):
        entry = {"status": "ready", "codec": "h264"}
        self.assertTrue(
            should_process(Path("/v.mp4"), _mock_history(entry), {"hevc", "av1"}, "/fp"),
        )

    @patch("src.filter.get_video_codec", return_value=None)
    def test_ready_with_none_codec_passes(self, mock_probe):
        entry = {"status": "ready", "codec": None}
        self.assertTrue(
            should_process(Path("/v.mp4"), _mock_history(entry), {"hevc", "av1"}, "/fp"),
        )


if __name__ == "__main__":
    unittest.main()

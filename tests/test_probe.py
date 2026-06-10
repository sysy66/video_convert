import subprocess
from unittest.mock import patch

from probe import get_video_codec


class TestProbeSuccess:
    def test_h264(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "h264\n"
            mock_run.return_value.stderr = ""
            result = get_video_codec("/test.mp4", "/usr/bin/ffprobe")
        assert result == "h264"

    def test_hevc(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "hevc\n"
            mock_run.return_value.stderr = ""
            result = get_video_codec("/test.mp4", "/usr/bin/ffprobe")
        assert result == "hevc"

    def test_av1(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "av1\n"
            mock_run.return_value.stderr = ""
            result = get_video_codec("/test.mp4", "/usr/bin/ffprobe")
        assert result == "av1"

    def test_strips_whitespace_and_lowercases(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "  H264 \n"
            mock_run.return_value.stderr = ""
            result = get_video_codec("/test.mp4", "/usr/bin/ffprobe")
        assert result == "h264"


class TestProbeErrors:
    def test_empty_ffprobe_path(self):
        result = get_video_codec("/test.mp4", "")
        assert result is None

    def test_none_ffprobe_path(self):
        result = get_video_codec("/test.mp4", None)
        assert result is None

    def test_nonzero_exit_code(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "error message"
            mock_run.return_value.stdout = ""
            result = get_video_codec("/test.mp4", "/usr/bin/ffprobe")
        assert result is None

    def test_file_not_found(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            result = get_video_codec("/test.mp4", "/usr/bin/ffprobe")
        assert result is None

    def test_timeout(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)
            result = get_video_codec("/test.mp4", "/usr/bin/ffprobe")
        assert result is None

    def test_generic_exception(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = PermissionError()
            result = get_video_codec("/test.mp4", "/usr/bin/ffprobe")
        assert result is None

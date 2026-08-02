import json
import subprocess
from unittest.mock import patch

import pytest

from src.probe import get_streams_info, get_video_codec

pytestmark = pytest.mark.unit


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


class TestGetStreamsInfo:
    _MOCK_JSON = json.dumps(
        {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "profile": "High"},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2},
                {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"},
            ],
            "format": {"filename": "/test.mp4", "duration": "123.456"},
        }
    )

    def test_returns_streams_and_duration(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = self._MOCK_JSON
            mock_run.return_value.stderr = ""
            info = get_streams_info("/test.mp4", "/usr/bin/ffprobe")
        assert info is not None
        assert len(info["streams"]) == 3
        assert info["streams"][0]["codec_type"] == "video"
        assert info["streams"][1]["codec_type"] == "audio"
        assert info["streams"][2]["codec_type"] == "subtitle"
        assert info["duration"] == 123.456

    def test_empty_ffprobe_path(self):
        result = get_streams_info("/test.mp4", "")
        assert result is None

    def test_none_ffprobe_path(self):
        result = get_streams_info("/test.mp4", None)
        assert result is None

    def test_nonzero_exit_code(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "error"
            mock_run.return_value.stdout = ""
            result = get_streams_info("/test.mp4", "/usr/bin/ffprobe")
        assert result is None

    def test_file_not_found(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            result = get_streams_info("/test.mp4", "/usr/bin/ffprobe")
        assert result is None

    def test_timeout(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)
            result = get_streams_info("/test.mp4", "/usr/bin/ffprobe")
        assert result is None

    def test_no_duration_in_format(self):
        data = {
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}],
            "format": {},
        }
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = json.dumps(data)
            mock_run.return_value.stderr = ""
            info = get_streams_info("/test.mp4", "/usr/bin/ffprobe")
        assert info["duration"] is None

    def test_bad_json(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "not json"
            mock_run.return_value.stderr = ""
            result = get_streams_info("/test.mp4", "/usr/bin/ffprobe")
        assert result is None

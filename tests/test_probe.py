import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.probe import (
    fps_fraction_to_float,
    get_expected_frame_count,
    get_streams_info,
    get_video_codec,
    get_video_frame_count,
    get_video_stream_duration,
    video_stream_covers_container,
)

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


class TestFpsFractionToFloat:
    def test_fraction(self):
        assert fps_fraction_to_float("24000/1001") == pytest.approx(23.976, rel=1e-4)
        assert fps_fraction_to_float("30000/1001") == pytest.approx(29.97, rel=1e-4)

    def test_decimal(self):
        assert fps_fraction_to_float("23.976") == pytest.approx(23.976)

    def test_whitespace(self):
        assert fps_fraction_to_float(" 30 ") == 30.0

    def test_int_string(self):
        assert fps_fraction_to_float("60") == 60.0

    def test_zero_denominator(self):
        assert fps_fraction_to_float("30/0") is None

    def test_non_numeric_fraction(self):
        assert fps_fraction_to_float("abc/def") is None

    def test_invalid(self):
        assert fps_fraction_to_float("") is None
        assert fps_fraction_to_float(None) is None
        assert fps_fraction_to_float("abc") is None

    def test_negative_or_zero_decimal(self):
        assert fps_fraction_to_float("0") is None
        assert fps_fraction_to_float("-30") is None


class TestGetExpectedFrameCount:
    def test_computes_frames(self):
        assert get_expected_frame_count(120, "30000/1001") == 3596

    def test_decimal_fps(self):
        assert get_expected_frame_count(10, "29.97") == 299

    def test_missing_duration(self):
        assert get_expected_frame_count(None, "30000/1001") is None

    def test_invalid_fps(self):
        assert get_expected_frame_count(120, "junk") is None
        assert get_expected_frame_count(120, None) is None

    def test_zero_duration(self):
        assert get_expected_frame_count(0, "30") == 0


class TestGetVideoFrameCount:
    def test_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "12345\n"
            mock_run.return_value.stderr = ""
            result = get_video_frame_count("/out.mkv", "/usr/bin/ffprobe")
        assert result == 12345

    def test_empty_ffprobe_path(self):
        assert get_video_frame_count("/out.mkv", "") is None
        assert get_video_frame_count("/out.mkv", None) is None

    def test_nonzero_exit_code(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = "decode error"
            result = get_video_frame_count("/out.mkv", "/usr/bin/ffprobe")
        assert result is None

    def test_empty_stdout(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            result = get_video_frame_count("/out.mkv", "/usr/bin/ffprobe")
        assert result is None

    def test_non_numeric_stdout(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "lots of frames\n"
            mock_run.return_value.stderr = ""
            result = get_video_frame_count("/out.mkv", "/usr/bin/ffprobe")
        assert result is None

    def test_file_not_found(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            result = get_video_frame_count("/out.mkv", "/usr/bin/ffprobe")
        assert result is None

    def test_timeout(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffprobe", timeout=300)
            result = get_video_frame_count("/out.mkv", "/usr/bin/ffprobe")
        assert result is None

    def test_generic_exception(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = PermissionError()
            result = get_video_frame_count("/out.mkv", "/usr/bin/ffprobe")
        assert result is None


class TestGetVideoStreamDuration:
    def test_returns_first_video_stream_duration(self):
        info = {
            "streams": [
                {"codec_type": "audio", "duration": "9283.0"},
                {"codec_type": "video", "duration": "2.0"},
                {"codec_type": "subtitle", "duration": "9283.0"},
            ]
        }
        assert get_video_stream_duration(info) == 2.0

    def test_returns_none_without_streams(self):
        assert get_video_stream_duration({"duration": 9283.0}) is None

    def test_returns_none_on_none_info(self):
        assert get_video_stream_duration(None) is None

    def test_returns_none_when_video_duration_missing(self):
        info = {"streams": [{"codec_type": "video"}]}
        assert get_video_stream_duration(info) is None

    def test_returns_none_on_unparseable_duration(self):
        info = {"streams": [{"codec_type": "video", "duration": "junk"}]}
        assert get_video_stream_duration(info) is None


class TestVideoStreamCoversContainer:
    def _run(self, stdout, returncode=0):
        mock_run = MagicMock()
        mock_run.returncode = returncode
        mock_run.stdout = stdout
        mock_run.stderr = ""
        return mock_run

    def test_healthy_file_with_tail_packets_returns_true(self):
        stdout = "7087.080\n7087.113\n7087.147\n"
        with patch("subprocess.run", return_value=self._run(stdout)) as mock_run:
            result = video_stream_covers_container(
                "/out.mkv", "/usr/bin/ffprobe", 7093.141
            )
        assert result is True
        cmd = mock_run.call_args[0][0]
        assert cmd[-6] == "packet=pts_time"
        assert cmd[-5] == "-read_intervals"
        assert cmd[-4] == "7063.141%7093.141"  # 30s 尾窗

    def test_truncated_video_with_no_tail_packets_returns_false(self):
        # 截断输出的视频包只在文件开头（pts≈0），seek 到尾部后读不到远期的包
        stdout = "0.000000\n0.042000\n0.083000\n"
        with patch("subprocess.run", return_value=self._run(stdout)):
            result = video_stream_covers_container(
                "/out.mkv", "/usr/bin/ffprobe", 9283.243
            )
        assert result is False

    def test_empty_output_returns_false(self):
        with patch("subprocess.run", return_value=self._run("")):
            result = video_stream_covers_container(
                "/out.mkv", "/usr/bin/ffprobe", 9283.243
            )
        assert result is False

    def test_nonzero_exit_returns_none(self):
        with patch("subprocess.run", return_value=self._run("", returncode=1)):
            result = video_stream_covers_container(
                "/out.mkv", "/usr/bin/ffprobe", 9283.243
            )
        assert result is None

    def test_na_packets_are_ignored(self):
        stdout = "N/A\n0.000000\nN/A\n"
        with patch("subprocess.run", return_value=self._run(stdout)):
            result = video_stream_covers_container(
                "/out.mkv", "/usr/bin/ffprobe", 9283.243
            )
        assert result is False

    def test_invalid_total_duration_returns_none(self):
        with patch("subprocess.run") as mock_run:
            result = video_stream_covers_container("/out.mkv", "/usr/bin/ffprobe", 0)
        mock_run.assert_not_called()
        assert result is None

    def test_file_not_found_returns_none(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = video_stream_covers_container(
                "/out.mkv", "/usr/bin/ffprobe", 9283.243
            )
        assert result is None

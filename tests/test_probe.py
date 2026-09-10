"""probe 模块单元测试：编码探测、流信息解析、帧率探测与归一化/投票、PTS 帧间隔分析。

Unit tests for the probe module: codec probing, stream-info parsing, frame-rate
probing/normalization/voting, and PTS interval analysis.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.probe import VideoProbe

pytestmark = pytest.mark.unit

_probe = VideoProbe("/usr/bin/ffprobe")


class TestProbeSuccess:
    def test_h264(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "h264\n"
            mock_run.return_value.stderr = ""
            result = _probe.get_codec("/test.mp4")
        assert result == "h264"

    def test_hevc(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "hevc\n"
            mock_run.return_value.stderr = ""
            result = _probe.get_codec("/test.mp4")
        assert result == "hevc"

    def test_av1(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "av1\n"
            mock_run.return_value.stderr = ""
            result = _probe.get_codec("/test.mp4")
        assert result == "av1"

    def test_strips_whitespace_and_lowercases(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "  H264 \n"
            mock_run.return_value.stderr = ""
            result = _probe.get_codec("/test.mp4")
        assert result == "h264"


class TestProbeErrors:
    def test_empty_ffprobe_path(self):
        result = VideoProbe("").get_codec("/test.mp4")
        assert result is None

    def test_none_ffprobe_path(self):
        result = VideoProbe(None).get_codec("/test.mp4")
        assert result is None

    def test_nonzero_exit_code(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "error message"
            mock_run.return_value.stdout = ""
            result = _probe.get_codec("/test.mp4")
        assert result is None

    def test_file_not_found(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            result = _probe.get_codec("/test.mp4")
        assert result is None

    def test_timeout(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)
            result = _probe.get_codec("/test.mp4")
        assert result is None

    def test_generic_exception(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = PermissionError()
            result = _probe.get_codec("/test.mp4")
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
            info = _probe.get_streams_info("/test.mp4")
        assert info is not None
        assert len(info["streams"]) == 3
        assert info["streams"][0]["codec_type"] == "video"
        assert info["streams"][1]["codec_type"] == "audio"
        assert info["streams"][2]["codec_type"] == "subtitle"
        assert info["duration"] == 123.456

    def test_empty_ffprobe_path(self):
        result = VideoProbe("").get_streams_info("/test.mp4")
        assert result is None

    def test_none_ffprobe_path(self):
        result = VideoProbe(None).get_streams_info("/test.mp4")
        assert result is None

    def test_nonzero_exit_code(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "error"
            mock_run.return_value.stdout = ""
            result = _probe.get_streams_info("/test.mp4")
        assert result is None

    def test_file_not_found(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            result = _probe.get_streams_info("/test.mp4")
        assert result is None

    def test_timeout(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)
            result = _probe.get_streams_info("/test.mp4")
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
            info = _probe.get_streams_info("/test.mp4")
        assert info["duration"] is None

    def test_bad_json(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "not json"
            mock_run.return_value.stderr = ""
            result = _probe.get_streams_info("/test.mp4")
        assert result is None


class TestGetFrameCount:
    # 按真实 ffprobe JSON 输出构造（键序 r/avg/duration/nb），同时回归
    # CSV 位置解析顺序假设错误的问题
    _META_OK = (
        '{"streams": [{"r_frame_rate": "30000/1001", "avg_frame_rate": '
        '"73743750/2460583", "duration": "7217.710133", "nb_frames": "216315"}]}'
    )
    _META_MKV = (
        '{"streams": [{"r_frame_rate": "N/A", "avg_frame_rate": "N/A", '
        '"duration": "N/A", "nb_frames": "N/A"}]}'
    )
    _META_BAD = (
        '{"streams": [{"r_frame_rate": "25/1", "avg_frame_rate": "25/1", '
        '"duration": "1000.000000", "nb_frames": "216315"}]}'
    )
    _META_BARE = (
        '{"streams": [{"r_frame_rate": "N/A", "avg_frame_rate": "N/A", '
        '"duration": "N/A", "nb_frames": "12345"}]}'
    )

    def test_nb_frames_fast_path(self):
        # mp4/mov 容器索引秒回：nb_frames 通过 duration×avg 交叉校验后直接
        # 采用，不再全量解码计数
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = self._META_OK
            mock_run.return_value.stderr = ""
            n = _probe.get_frame_count("/test.mp4")
        assert n == 216315
        assert mock_run.call_count == 1

    def test_nb_frames_fast_path_command(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = self._META_OK
            mock_run.return_value.stderr = ""
            _probe.get_frame_count("/test.mp4")
        cmd = mock_run.call_args[0][0]
        assert any("nb_frames" in c for c in cmd)
        assert any("avg_frame_rate" in c for c in cmd)
        assert any("r_frame_rate" in c for c in cmd)

    def test_nb_frames_crosscheck_mismatch_falls_back(self):
        # 容器索引与 duration×avg 偏差过大（文件尾损坏等）→ 降级全量计数
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._META_BAD, stderr=""),
                MagicMock(returncode=0, stdout="361548", stderr=""),
            ]
            n = _probe.get_frame_count("/test.mp4")
        assert n == 361548
        assert mock_run.call_count == 2

    def test_mkv_no_nb_frames_falls_back_to_count_packets(self):
        # mkv/webm 无 nb_frames 字段 → 读包计数兜底（不解码，1 包=1 帧）
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._META_MKV, stderr=""),
                MagicMock(returncode=0, stdout="216315", stderr=""),
            ]
            n = _probe.get_frame_count("/test.mp4")
        assert n == 216315

    def test_nb_frames_without_rate_trusts_index(self):
        # 无 duration/帧率元数据可交叉校验时直接信任容器索引
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = self._META_BARE
            mock_run.return_value.stderr = ""
            n = _probe.get_frame_count("/test.mp4")
        assert n == 12345

    def test_returns_frame_count(self):
        # mkv 源：nb_frames 不可得 → -count_packets 读包计数兜底
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._META_MKV, stderr=""),
                MagicMock(returncode=0, stdout="361548", stderr=""),
            ]
            n = _probe.get_frame_count("/test.mp4")
        assert n == 361548

    def test_count_packets_flag_and_select_streams(self):
        # 兜底命令必须是 -count_packets（读包不解码），不能退回全量软解
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._META_MKV, stderr=""),
                MagicMock(returncode=0, stdout="10", stderr=""),
            ]
            _probe.get_frame_count("/test.mp4")
        cmd = mock_run.call_args[0][0]
        assert "-count_packets" in cmd
        assert "-count_frames" not in cmd
        assert "-select_streams" in cmd
        assert "v:0" in cmd
        assert any("nb_read_packets" in c for c in cmd)
        assert cmd[-1] == "/test.mp4"

    def test_na_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._META_MKV, stderr=""),
                MagicMock(returncode=0, stdout="N/A", stderr=""),
            ]
            n = _probe.get_frame_count("/test.mp4")
        assert n is None

    def test_nonzero_exit_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "decode error"
            mock_run.return_value.stdout = ""
            n = _probe.get_frame_count("/test.mp4")
        assert n is None

    def test_parse_error_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="garbage", stderr=""),
                MagicMock(returncode=0, stdout="not-a-number", stderr=""),
            ]
            n = _probe.get_frame_count("/test.mp4")
        assert n is None

    def test_timeout_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)
            n = _probe.get_frame_count("/test.mp4")
        assert n is None

    def test_empty_ffprobe_path(self):
        result = VideoProbe("").get_frame_count("/test.mp4")
        assert result is None


class TestFpsFractionToFloat:
    def test_fraction(self):
        assert VideoProbe.fps_fraction_to_float("24000/1001") == pytest.approx(23.976, rel=1e-4)
        assert VideoProbe.fps_fraction_to_float("30000/1001") == pytest.approx(29.97, rel=1e-4)

    def test_decimal(self):
        assert VideoProbe.fps_fraction_to_float("23.976") == pytest.approx(23.976)

    def test_whitespace(self):
        assert VideoProbe.fps_fraction_to_float(" 30 ") == 30.0

    def test_int_string(self):
        assert VideoProbe.fps_fraction_to_float("60") == 60.0

    def test_zero_denominator(self):
        assert VideoProbe.fps_fraction_to_float("30/0") is None

    def test_non_numeric_fraction(self):
        assert VideoProbe.fps_fraction_to_float("abc/def") is None

    def test_invalid(self):
        assert VideoProbe.fps_fraction_to_float("") is None
        assert VideoProbe.fps_fraction_to_float(None) is None
        assert VideoProbe.fps_fraction_to_float("abc") is None

    def test_negative_or_zero_decimal(self):
        assert VideoProbe.fps_fraction_to_float("0") is None
        assert VideoProbe.fps_fraction_to_float("-30") is None


def _pts_stdout(fps, frames=300):
    step = 1.0 / fps
    return "\n".join(f"{i * step:.9f}" for i in range(frames)) + "\n"

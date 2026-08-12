"""probe 模块单元测试：编码探测、流信息解析、帧率探测与归一化/投票、PTS 帧间隔分析。

Unit tests for the probe module: codec probing, stream-info parsing, frame-rate
probing/normalization/voting, and PTS interval analysis.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.probe import (
    VideoProbe,
    get_streams_info,
    get_video_codec,
    get_video_frame_rate,
)

pytestmark = pytest.mark.unit

_probe = VideoProbe("/usr/bin/ffprobe")


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


class TestGetVideoFrameRate:
    _META_VFR = "avg_frame_rate=2128917989/70854606\nr_frame_rate=60/1\n"
    _META_CFR = "avg_frame_rate=30000/1001\nr_frame_rate=30000/1001\n"

    def test_cfr_consistent_returns_avg_without_sampling(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = self._META_CFR
            mock_run.return_value.stderr = ""
            result = get_video_frame_rate("/test.mp4", "/usr/bin/ffprobe")
        assert result == "30000/1001"
        assert mock_run.call_count == 1

    def test_cfr_long_fraction_normalized(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.return_value.stdout = (
                "avg_frame_rate=73743750/2460583\nr_frame_rate=30000/1001\n"
            )
            result = get_video_frame_rate("/test.mp4", "/usr/bin/ffprobe")
        assert result == "30000/1001"
        assert mock_run.call_count == 1

    def test_vfr_uses_pts_interval_mode(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._META_VFR, stderr=""),
                MagicMock(returncode=0, stdout=_pts_stdout(30), stderr=""),
            ]
            result = get_video_frame_rate("/test.mp4", "/usr/bin/ffprobe")
        assert result == "30/1"
        assert mock_run.call_count == 2

    def test_vfr_2997_interval(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._META_VFR, stderr=""),
                MagicMock(returncode=0, stdout=_pts_stdout(30000.0 / 1001.0), stderr=""),
            ]
            result = get_video_frame_rate("/test.mp4", "/usr/bin/ffprobe")
        assert result == "30000/1001"

    def test_pts_probe_failure_falls_back_to_avg(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._META_VFR, stderr=""),
                MagicMock(returncode=1, stdout="", stderr="decoding failed"),
            ]
            result = get_video_frame_rate("/test.mp4", "/usr/bin/ffprobe")
        assert result == "2128917989/70854606"

    def test_pts_probe_timeout_falls_back_to_avg(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._META_VFR, stderr=""),
                subprocess.TimeoutExpired(cmd="ffprobe", timeout=60),
            ]
            result = get_video_frame_rate("/test.mp4", "/usr/bin/ffprobe")
        assert result == "2128917989/70854606"

    def test_empty_ffprobe_path(self):
        assert get_video_frame_rate("/test.mp4", "") is None

    def test_none_ffprobe_path(self):
        assert get_video_frame_rate("/test.mp4", None) is None


class TestClassifyFps:
    _META_CFR = "avg_frame_rate=30000/1001\nr_frame_rate=30000/1001\n"
    _META_VFR = "avg_frame_rate=2128917989/70854606\nr_frame_rate=60/1\n"

    def _classify(self, meta, pts_stdout=None, pts_exception=None):
        side_effects = [MagicMock(returncode=0, stdout=meta, stderr="")]
        if pts_exception is not None:
            side_effects.append(pts_exception)
        elif pts_stdout is not None:
            side_effects.append(MagicMock(returncode=0, stdout=pts_stdout, stderr=""))
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.side_effect = side_effects
            tier, rate = _probe.classify_fps("/test.mp4")
        return tier, rate, mock_run

    def test_tier1_cfr_meta_agrees_no_sampling(self):
        tier, rate, mock_run = self._classify(self._META_CFR)
        assert (tier, rate) == ("1", "30000/1001")
        assert mock_run.call_count == 1

    def test_tier2_pseudo_vfr_dominant_share(self):
        with patch("src.probe.config.VMAF_PSEUDO_VFR_MIN_RATIO", 0.75):
            tier, rate, _ = self._classify(self._META_VFR, _pts_stdout(30))
        assert (tier, rate) == ("2", "30/1")

    def test_tier3_mixed_vfr_below_threshold(self):
        # 30/60 各半 → 主导份额 ~0.5，低于 0.75 → 档3 混合 VFR
        lines = [f"{i * 33333e-6:.9f}" for i in range(200)]
        lines += [f"{10 + i * 16667e-6:.9f}" for i in range(200)]
        tier, rate, _ = self._classify(self._META_VFR, "\n".join(lines) + "\n")
        assert (tier, rate) == ("3", None)

    def test_tier3_pts_probe_failure(self):
        tier, rate, _ = self._classify(
            self._META_VFR, pts_exception=subprocess.TimeoutExpired(cmd="ffprobe", timeout=60)
        )
        assert (tier, rate) == ("3", None)

    def test_tier3_no_metadata(self):
        tier, rate, _ = self._classify("")
        assert (tier, rate) == ("3", None)

    def test_empty_ffprobe_path(self):
        assert VideoProbe("").classify_fps("/test.mp4") == ("3", None)


class TestVoteCommonRate:
    def test_tick_quantized_30fps_merges_buckets(self):
        diffs = [33317] * 124 + [33333] * 138 + [33350] * 133 + [16667] * 20 + [50000] * 45
        frac, ratio = _probe._vote_common_rate(diffs)
        assert frac == "30/1"
        assert ratio == pytest.approx(0.80, abs=0.03)

    def test_2997_grid(self):
        diffs = [33367] * 150 + [33317] * 10 + [50000] * 10
        frac, ratio = _probe._vote_common_rate(diffs)
        assert frac == "30000/1001"
        assert ratio > 0.85

    def test_60fps(self):
        diffs = [16667] * 200 + [33333] * 10
        frac, ratio = _probe._vote_common_rate(diffs)
        assert frac == "60/1"
        assert ratio > 0.9

    def test_uncommon_rate_rejected(self):
        diffs = [28571] * 300
        assert _probe._vote_common_rate(diffs) is None

    def test_changed_fps_mixed_dominant_wins(self):
        diffs = [33333] * 200 + [16667] * 200
        frac, ratio = _probe._vote_common_rate(diffs)
        assert frac in ("30/1", "60/1")
        assert ratio == pytest.approx(0.5, abs=0.01)

    def test_chaotic_intervals_rejected(self):
        diffs = [20000 + (i * 137 % 40000) for i in range(300)]
        assert _probe._vote_common_rate(diffs) is None


class TestProbePtsIntervals:
    def test_reads_three_segments_and_filters_gaps(self):
        lines = []
        for seg_start in (0.0, 1000.0, 2000.0):
            for i in range(31):
                lines.append(f"{seg_start + i / 30:.6f}")
        stdout = "\n".join(lines) + "\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = stdout
            mock_run.return_value.stderr = ""
            intervals = _probe._probe_pts_intervals("/test.mp4")
        assert len(intervals) == 90
        assert all(i in (33333, 33334) for i in intervals)
        cmd = mock_run.call_args.args[0]
        assert "-read_intervals" in cmd
        assert "0%+5,50%+5,95%+5" in cmd

    def test_failure_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "err"
            assert _probe._probe_pts_intervals("/test.mp4") is None


class TestNormalizeCommonRate:
    @pytest.mark.parametrize(
        "rate,expected",
        [
            ("73743750/2460583", "30000/1001"),
            ("30000/1001", "30000/1001"),
            ("30/1", "30/1"),
            ("24000/1001", "24000/1001"),
            ("60/1", "60/1"),
        ],
    )
    def test_common_rates(self, rate, expected):
        assert _probe._normalize_common_rate(rate) == expected

    @pytest.mark.parametrize("rate", ["35", "27.5", "0", None, "abc"])
    def test_non_common_rates_passthrough(self, rate):
        assert _probe._normalize_common_rate(rate) == rate

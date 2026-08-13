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
    detect_vmaf_align_rate,
    get_frame_count,
    get_streams_info,
    get_video_codec,
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
            n = get_frame_count("/test.mp4", "/usr/bin/ffprobe")
        assert n == 216315
        assert mock_run.call_count == 1

    def test_nb_frames_fast_path_command(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = self._META_OK
            mock_run.return_value.stderr = ""
            get_frame_count("/test.mp4", "/usr/bin/ffprobe")
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
            n = get_frame_count("/test.mp4", "/usr/bin/ffprobe")
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
            n = get_frame_count("/test.mp4", "/usr/bin/ffprobe")
        assert n == 216315

    def test_nb_frames_without_rate_trusts_index(self):
        # 无 duration/帧率元数据可交叉校验时直接信任容器索引
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = self._META_BARE
            mock_run.return_value.stderr = ""
            n = get_frame_count("/test.mp4", "/usr/bin/ffprobe")
        assert n == 12345

    def test_returns_frame_count(self):
        # mkv 源：nb_frames 不可得 → -count_packets 读包计数兜底
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._META_MKV, stderr=""),
                MagicMock(returncode=0, stdout="361548", stderr=""),
            ]
            n = get_frame_count("/test.mp4", "/usr/bin/ffprobe")
        assert n == 361548

    def test_count_packets_flag_and_select_streams(self):
        # 兜底命令必须是 -count_packets（读包不解码），不能退回全量软解
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._META_MKV, stderr=""),
                MagicMock(returncode=0, stdout="10", stderr=""),
            ]
            get_frame_count("/test.mp4", "/usr/bin/ffprobe")
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
            n = get_frame_count("/test.mp4", "/usr/bin/ffprobe")
        assert n is None

    def test_nonzero_exit_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "decode error"
            mock_run.return_value.stdout = ""
            n = get_frame_count("/test.mp4", "/usr/bin/ffprobe")
        assert n is None

    def test_parse_error_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="garbage", stderr=""),
                MagicMock(returncode=0, stdout="not-a-number", stderr=""),
            ]
            n = get_frame_count("/test.mp4", "/usr/bin/ffprobe")
        assert n is None

    def test_timeout_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)
            n = get_frame_count("/test.mp4", "/usr/bin/ffprobe")
        assert n is None

    def test_empty_ffprobe_path(self):
        result = get_frame_count("/test.mp4", "")
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


class TestDetectVmafAlignRate:
    _META_CFR = "avg_frame_rate=30000/1001\nr_frame_rate=30000/1001\n"
    _META_VFR = "avg_frame_rate=2128917989/70854606\nr_frame_rate=60/1\n"
    _META_AVG_ONLY = "avg_frame_rate=30000/1001\nr_frame_rate=0/0\n"
    _META_R_ONLY = "avg_frame_rate=N/A\nr_frame_rate=60/1\n"

    def _detect(self, meta, pts_stdout=None, pts_exception=None):
        side_effects = [MagicMock(returncode=0, stdout=meta, stderr="")]
        if pts_exception is not None:
            side_effects.append(pts_exception)
        elif pts_stdout is not None:
            side_effects.append(MagicMock(returncode=0, stdout=pts_stdout, stderr=""))
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.side_effect = side_effects
            result = detect_vmaf_align_rate("/test.mp4", "/usr/bin/ffprobe")
        return result, mock_run

    def test_cfr_identical_returns_avg_without_sampling(self):
        result, mock_run = self._detect(self._META_CFR)
        assert result == "30000/1001"
        assert mock_run.call_count == 1

    def test_near_equal_texts_agree_without_sampling(self):
        # 文本不同但值相近（73743750/2460583≈29.96987 vs 30000/1001≈29.97003，
        # 相对差 ~0.0002%）→ 0.05% 容差内判一致：零采样，返回规范化 avg
        meta = "avg_frame_rate=73743750/2460583\nr_frame_rate=30000/1001\n"
        result, mock_run = self._detect(meta)
        assert result == "30000/1001"
        assert mock_run.call_count == 1

    def test_2997_vs_30_goes_to_vote(self):
        # 30/1 vs 30000/1001 相对差恰 0.1%（> 0.05% 容差）→ 判 VFR 嫌疑，
        # 投票裁决出真实帧率，绝不按容差吞掉档位差异
        meta = "avg_frame_rate=30/1\nr_frame_rate=30000/1001\n"
        result, _ = self._detect(meta, _pts_stdout(30))
        assert result == "30/1"

    def test_vfr_uses_pts_interval_vote(self):
        result, mock_run = self._detect(self._META_VFR, _pts_stdout(30))
        assert result == "30/1"
        assert mock_run.call_count == 2

    def test_vfr_2997_interval(self):
        result, mock_run = self._detect(self._META_VFR, _pts_stdout(30000.0 / 1001.0))
        assert result == "30000/1001"

    def test_vote_wins_without_share_threshold(self):
        # 30/60 各半 → 主导份额 ~0.5，无分档阈值，投票主导帧率照常采用
        lines = [f"{i * 33333e-6:.9f}" for i in range(200)]
        lines += [f"{10 + i * 16667e-6:.9f}" for i in range(200)]
        result, _ = self._detect(self._META_VFR, "\n".join(lines) + "\n")
        assert result == "30/1"

    def test_vote_too_weak_falls_back_to_avg(self):
        # 帧间隔不在常见帧率表（17fps）→ 投票无结果 → 回退 avg
        result, _ = self._detect(self._META_VFR, _pts_stdout(17))
        assert result == "2128917989/70854606"

    def test_pts_probe_failure_falls_back_to_avg(self):
        result, _ = self._detect(
            self._META_VFR, pts_exception=subprocess.TimeoutExpired(cmd="ffprobe", timeout=60)
        )
        assert result == "2128917989/70854606"

    def test_avg_missing_falls_back_to_r_without_sampling(self):
        # 单边元数据（avg 缺失）不做投票，直接回退 r
        result, mock_run = self._detect(self._META_R_ONLY)
        assert result == "60/1"
        assert mock_run.call_count == 1

    def test_r_missing_falls_back_to_avg_without_sampling(self):
        result, mock_run = self._detect(self._META_AVG_ONLY)
        assert result == "30000/1001"
        assert mock_run.call_count == 1

    def test_no_metadata_returns_none(self):
        result, _ = self._detect("")
        assert result is None

    def test_empty_ffprobe_path(self):
        assert detect_vmaf_align_rate("/test.mp4", "") is None

    def test_none_ffprobe_path(self):
        assert detect_vmaf_align_rate("/test.mp4", None) is None


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

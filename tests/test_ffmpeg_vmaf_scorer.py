"""ffmpeg_vmaf_scorer 模块单元测试：HW 像素格式映射、JSON 分数解析、
帧数诊断日志、VMAF 运行与 FfmpegVMAFScorer.compute_vmaf 包装。

Unit tests for the ffmpeg_vmaf_scorer module: HW pixel-format mapping, JSON
score parsing, frame-count diagnostics, VMAF run and the compute_vmaf wrapper.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src.ffmpeg_vmaf_scorer import FfmpegVMAFScorer

pytestmark = pytest.mark.unit


def _make_scorer(tmp_path=None, **overrides):
    """以显式注入构造 scorer（纯注入：不依赖 config 默认值）。

    Build a scorer with explicit injection (pure DI: no config defaults).
    """
    defaults = dict(
        ffprobe_path="/ffprobe",
        vmaf_ffmpeg_path="/ffmpeg",
        vmaf_model_path=None,
        vmaf_log_dir=str(tmp_path) if tmp_path else "/tmp/vmaf_logs",
        n_threads=1,
        n_subsample=1,
        vmaf_timeout=config.VMAF_TIMEOUT,
        vmaf_hwaccel=False,
        vmaf_hw_fallback=True,
    )
    defaults.update(overrides)
    return FfmpegVMAFScorer(**defaults)


class TestHwDownloadFormat:
    def test_p010le_from_probe(self, tmp_path):
        f = tmp_path / "test.mkv"
        f.write_text("x")
        with patch("src.ffmpeg_vmaf_scorer.VideoProbe.get_pix_fmt", return_value="yuv420p10le"):
            assert _make_scorer()._hw_download_format(str(f)) == "p010le"

    def test_nv12_from_probe(self, tmp_path):
        f = tmp_path / "test.mkv"
        f.write_text("x")
        with patch("src.ffmpeg_vmaf_scorer.VideoProbe.get_pix_fmt", return_value="yuv420p"):
            assert _make_scorer()._hw_download_format(str(f)) == "nv12"

    def test_default_on_probe_failure(self, tmp_path):
        f = tmp_path / "test.mkv"
        f.write_text("x")
        with patch("src.ffmpeg_vmaf_scorer.VideoProbe.get_pix_fmt", return_value=None):
            assert _make_scorer()._hw_download_format(str(f)) == "nv12"

    def test_p010le_direct_match(self, tmp_path):
        f = tmp_path / "test.mkv"
        f.write_text("x")
        with patch("src.ffmpeg_vmaf_scorer.VideoProbe.get_pix_fmt", return_value="p010le"):
            assert _make_scorer()._hw_download_format(str(f)) == "p010le"


class TestReadVmafFromJson:
    def test_reads_mean_and_min(self, tmp_path):
        log = tmp_path / "vmaf.json"
        log.write_text(json.dumps({"pooled_metrics": {"vmaf": {"mean": 92.5, "min": 88.1}}}))
        mean, minimum = _make_scorer()._read_vmaf_from_json(log)
        assert mean == 92.5
        assert minimum == 88.1

    def test_missing_pooled_metrics(self, tmp_path):
        log = tmp_path / "vmaf.json"
        log.write_text(json.dumps({"frames": []}))
        mean, minimum = _make_scorer()._read_vmaf_from_json(log)
        assert mean is None
        assert minimum is None

    def test_missing_vmaf_key(self, tmp_path):
        log = tmp_path / "vmaf.json"
        log.write_text(json.dumps({"pooled_metrics": {}}))
        mean, minimum = _make_scorer()._read_vmaf_from_json(log)
        assert mean is None
        assert minimum is None

    def test_bad_json(self, tmp_path):
        log = tmp_path / "vmaf.json"
        log.write_text("not json")
        mean, minimum = _make_scorer()._read_vmaf_from_json(log)
        assert mean is None
        assert minimum is None

    def test_missing_file(self, tmp_path):
        log = tmp_path / "nonexistent.json"
        mean, minimum = _make_scorer()._read_vmaf_from_json(log)
        assert mean is None
        assert minimum is None


class TestFrameCountsCheck:
    def test_returns_none_always(self):
        """纯日志方法：任何输入都返回 None，不阻断打分。"""
        scorer = _make_scorer()
        assert scorer._frame_counts_check(1826, 1826) is None
        assert scorer._frame_counts_check(1000, 1011) is None
        assert scorer._frame_counts_check(None, 1826) is None
        assert scorer._frame_counts_check(None, None) is None

    def test_logs_unavailable(self, caplog):
        with caplog.at_level(logging.INFO):
            _make_scorer()._frame_counts_check(None, 1826)
        assert "differ" in caplog.text

    def test_logs_counts(self, caplog):
        with caplog.at_level(logging.INFO):
            _make_scorer()._frame_counts_check(1826, 1826)
        assert "1826" in caplog.text


class TestRunVmaf:
    def _run(self, scorer, ref, dist, log_path, **kwargs):
        return scorer._run_vmaf(ref, dist, log_path, **kwargs)

    def test_sw_success_from_json(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        log_path = tmp_path / "out.json"
        scorer = _make_scorer(tmp_path)

        result = MagicMock()
        result.returncode = 0
        result.stderr = b""
        result.stdout = b""
        with (
            patch("subprocess.run", return_value=result),
            patch(
                "src.ffmpeg_vmaf_scorer.FfmpegVMAFScorer._read_vmaf_from_json",
                return_value=(94.0, 90.0),
            ),
        ):
            score = self._run(scorer, ref, dist, log_path, ref_frame_count=1, dist_frame_count=1)
        assert score == 94.0

    def test_no_log_path_returns_none_when_json_unavailable(self, tmp_path):
        """无 log_path 且 rc=0 时，stderr 不再取分，返回 None。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        scorer = _make_scorer()

        result = MagicMock()
        result.returncode = 0
        result.stderr = b"VMAF score: 88.5"
        result.stdout = b""
        with (
            patch("subprocess.run", return_value=result),
            patch(
                "src.ffmpeg_vmaf_scorer.FfmpegVMAFScorer._read_vmaf_from_json",
                return_value=(None, None),
            ),
        ):
            score = self._run(scorer, ref, dist, None, ref_frame_count=1, dist_frame_count=1)
        assert score is None

    def test_nonzero_returncode(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        scorer = _make_scorer()

        result = MagicMock()
        result.returncode = 1
        with patch("subprocess.run", return_value=result):
            score = self._run(scorer, ref, dist, None, ref_frame_count=1, dist_frame_count=1)
        assert score is None

    def test_hw_filter_complex_contains_hwdownload(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        log_path = tmp_path / "out.json"
        scorer = _make_scorer(tmp_path)

        result = MagicMock()
        result.returncode = 0
        result.stderr = b""
        result.stdout = b""
        with (
            patch.object(scorer, "_hw_download_format", return_value="nv12"),
            patch("subprocess.run", return_value=result) as mock_run,
            patch(
                "src.ffmpeg_vmaf_scorer.FfmpegVMAFScorer._read_vmaf_from_json",
                return_value=(None, None),
            ),
        ):
            self._run(
                scorer, ref, dist, log_path, hwaccel=True, ref_frame_count=1, dist_frame_count=1
            )

        call_args = mock_run.call_args[0][0]
        filter_idx = call_args.index("-filter_complex")
        assert "hwdownload" in call_args[filter_idx + 1]
        assert "-hwaccel" in call_args
        assert "qsv" in call_args

    def test_scores_despite_count_mismatch(self, tmp_path):
        """两路帧数相对差 >1% 不再阻断打分：仍调用 ffmpeg 帧序对齐打分。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        scorer = _make_scorer()

        with patch("subprocess.run") as mock_run:
            self._run(scorer, ref, dist, None, ref_frame_count=10000, dist_frame_count=10200)
        mock_run.assert_called()

    def test_scores_when_counts_unavailable(self, tmp_path):
        """任一帧数不可得（None）不再阻断打分：仍调用 ffmpeg。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        scorer = _make_scorer()

        with patch("subprocess.run") as mock_run:
            self._run(scorer, ref, dist, None, ref_frame_count=None, dist_frame_count=1826)
        mock_run.assert_called()

    def test_libvmaf_opts_pin_fqm_framesync_semantics(self, tmp_path):
        """libvmaf opts 恒带 shortest=1:repeatlast=0（与 ffmpeg-quality-metrics
        一致）：帧数不足时以短流为准截断、不重复最后一帧。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        scorer = _make_scorer()

        result = MagicMock(returncode=0, stderr=b"", stdout=b"")
        with (
            patch("subprocess.run", return_value=result) as mock_run,
            patch(
                "src.ffmpeg_vmaf_scorer.FfmpegVMAFScorer._read_vmaf_from_json",
                return_value=(None, None),
            ),
        ):
            self._run(scorer, ref, dist, None, ref_frame_count=1, dist_frame_count=1)

        call_args = mock_run.call_args[0][0]
        fc = call_args[call_args.index("-filter_complex") + 1]
        assert "shortest=1:repeatlast=0" in fc

    def test_timeout_is_injected(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        scorer = _make_scorer(vmaf_timeout=1234)

        result = MagicMock(returncode=0, stderr=b"", stdout=b"")
        with (
            patch("subprocess.run", return_value=result) as mock_run,
            patch(
                "src.ffmpeg_vmaf_scorer.FfmpegVMAFScorer._read_vmaf_from_json",
                return_value=(94.0, 90.0),
            ),
        ):
            self._run(
                scorer,
                ref,
                dist,
                tmp_path / "out.json",
                ref_frame_count=1,
                dist_frame_count=1,
            )

        assert mock_run.call_args[1]["timeout"] == 1234


class TestComputeVmaf:
    def test_returns_none_when_no_ffmpeg_path(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        scorer = _make_scorer(vmaf_ffmpeg_path="")
        assert scorer.compute_vmaf(ref, dist) is None

    def test_generates_default_log_path(self, tmp_path):
        """未传 log_path 时自动生成到注入的 vmaf_log_dir 的 json 日志（取分来源）。"""
        ref = tmp_path / "My Video 1080p.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        scorer = _make_scorer(vmaf_log_dir=str(tmp_path / "vmaf_logs"))
        with patch.object(scorer, "_run_vmaf", return_value=90.0) as mock_run:
            score = scorer.compute_vmaf(ref, dist)

        assert score == 90.0
        log_path = mock_run.call_args[0][2]
        assert log_path.parent == tmp_path / "vmaf_logs"
        assert log_path.name.startswith("My_Video_1080p.")
        assert log_path.name.endswith(".json")

    def test_hw_then_fallback(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        scorer = _make_scorer(vmaf_hwaccel=True, vmaf_hw_fallback=True)
        with patch.object(scorer, "_run_vmaf", side_effect=[None, 91.0]) as mock_run:
            score = scorer.compute_vmaf(ref, dist)

        assert score == 91.0
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0][1]["hwaccel"] is True
        assert mock_run.call_args_list[1][1]["hwaccel"] is False

    def test_hw_success_no_fallback(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        scorer = _make_scorer(vmaf_hwaccel=True, vmaf_hw_fallback=True)
        with patch.object(scorer, "_run_vmaf", return_value=96.0) as mock_run:
            score = scorer.compute_vmaf(ref, dist)

        assert score == 96.0
        assert mock_run.call_count == 1

    def test_hw_fail_no_fallback_returns_none(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        scorer = _make_scorer(vmaf_hwaccel=True, vmaf_hw_fallback=False)
        with patch.object(scorer, "_run_vmaf", return_value=None):
            score = scorer.compute_vmaf(ref, dist)

        assert score is None

    def test_sw_only_path(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        scorer = _make_scorer()
        with patch.object(scorer, "_run_vmaf", return_value=90.0) as mock_run:
            score = scorer.compute_vmaf(ref, dist)

        assert score == 90.0
        mock_run.assert_called_once()
        assert mock_run.call_args[1]["hwaccel"] is False

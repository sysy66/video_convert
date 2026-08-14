"""quality 模块单元测试：VMAF 接受/拒绝判定、HW 像素格式映射、JSON 分数解析、
日志清理、VMAF 运行与 compute_vmaf 包装。

Unit tests for the quality module: accept/reject verdicts, HW pixel-format
mapping, JSON score parsing, log cleanup, VMAF run and compute_vmaf wrapper.
"""

import json
import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src.quality import (
    _frame_counts_check,
    _hw_download_format,
    _read_vmaf_from_json,
    _run_vmaf,
    cleanup_vmaf_logs,
    compute_vmaf,
    should_accept,
    vmaf_verdict,
)

pytestmark = pytest.mark.unit


class TestVmafVerdict:
    def test_none_score_retries(self):
        assert vmaf_verdict(None, 0.5) == "retry"

    def test_extreme_low_score_retries(self):
        with patch("src.quality.config.VMAF_EXTREME_LOW", 60):
            assert vmaf_verdict(55.0, 0.5) == "retry"
            assert vmaf_verdict(59.99, 1.0) == "retry"

    def test_ratio_over_max_rejects_first(self):
        with patch("src.quality.config.ACCEPT_RATIO_MAX", 1.00):
            assert vmaf_verdict(55.0, 1.1) == "reject"
            assert vmaf_verdict(None, 1.1) == "reject"

    def test_low_score_rejects(self):
        with patch("src.quality.config.VMAF_EXTREME_LOW", 60):
            with patch("src.quality.config.VMAF_ACCEPT_LOW", 80):
                assert vmaf_verdict(70.0, 0.5) == "reject"

    def test_middle_good_ratio_accepts(self):
        with patch("src.quality.config.VMAF_EXTREME_LOW", 60):
            with patch("src.quality.config.VMAF_ACCEPT_LOW", 80):
                with patch("src.quality.config.ACCEPT_RATIO_LOW", 0.75):
                    with patch("src.quality.config.VMAF_ACCEPT_HIGH", 93):
                        assert vmaf_verdict(85.0, 0.5) == "accept"

    def test_middle_bad_ratio_rejects(self):
        with patch("src.quality.config.VMAF_EXTREME_LOW", 60):
            with patch("src.quality.config.VMAF_ACCEPT_LOW", 80):
                with patch("src.quality.config.ACCEPT_RATIO_LOW", 0.75):
                    with patch("src.quality.config.VMAF_ACCEPT_HIGH", 93):
                        assert vmaf_verdict(85.0, 0.9) == "reject"

    def test_high_good_ratio_accepts(self):
        with patch("src.quality.config.VMAF_ACCEPT_HIGH", 93):
            with patch("src.quality.config.ACCEPT_RATIO_MAX", 1.00):
                assert vmaf_verdict(95.0, 0.8) == "accept"

    def test_high_bad_ratio_rejects(self):
        with patch("src.quality.config.VMAF_ACCEPT_HIGH", 93):
            with patch("src.quality.config.ACCEPT_RATIO_MAX", 1.00):
                assert vmaf_verdict(95.0, 1.1) == "reject"


class TestShouldAccept:
    def test_vmaf_high_accept_good_ratio(self):
        with patch("src.quality.config.VMAF_ACCEPT_HIGH", 93):
            with patch("src.quality.config.ACCEPT_RATIO_MAX", 1.00):
                assert should_accept(95.0, 0.8) is True

    def test_vmaf_high_reject_bad_ratio(self):
        with patch("src.quality.config.VMAF_ACCEPT_HIGH", 93):
            with patch("src.quality.config.ACCEPT_RATIO_MAX", 1.00):
                assert should_accept(95.0, 1.1) is False

    def test_vmaf_low_reject(self):
        with patch("src.quality.config.VMAF_ACCEPT_LOW", 80):
            assert should_accept(70.0, 0.5) is False

    def test_vmaf_middle_good_ratio(self):
        with patch("src.quality.config.VMAF_ACCEPT_LOW", 80):
            with patch("src.quality.config.ACCEPT_RATIO_LOW", 0.75):
                with patch("src.quality.config.VMAF_ACCEPT_HIGH", 93):
                    assert should_accept(85.0, 0.5) is True

    def test_vmaf_middle_bad_ratio(self):
        with patch("src.quality.config.VMAF_ACCEPT_LOW", 80):
            with patch("src.quality.config.ACCEPT_RATIO_LOW", 0.75):
                with patch("src.quality.config.VMAF_ACCEPT_HIGH", 93):
                    assert should_accept(85.0, 0.9) is False

    def test_vmaf_exactly_high(self):
        with patch("src.quality.config.VMAF_ACCEPT_HIGH", 93):
            with patch("src.quality.config.ACCEPT_RATIO_MAX", 1.00):
                assert should_accept(93.0, 1.0) is True

    def test_vmaf_exactly_low(self):
        with patch("src.quality.config.VMAF_ACCEPT_LOW", 80):
            assert should_accept(79.9, 0.5) is False


class TestHwDownloadFormat:
    def test_p010le_from_probe(self, tmp_path):
        f = tmp_path / "test.mkv"
        f.write_text("x")
        with patch("src.quality.get_video_pix_fmt", return_value="yuv420p10le"):
            assert _hw_download_format(str(f), "/ffprobe") == "p010le"

    def test_nv12_from_probe(self, tmp_path):
        f = tmp_path / "test.mkv"
        f.write_text("x")
        with patch("src.quality.get_video_pix_fmt", return_value="yuv420p"):
            assert _hw_download_format(str(f), "/ffprobe") == "nv12"

    def test_default_on_probe_failure(self, tmp_path):
        f = tmp_path / "test.mkv"
        f.write_text("x")
        with patch("src.quality.get_video_pix_fmt", return_value=None):
            assert _hw_download_format(str(f), "/ffprobe") == "nv12"

    def test_p010le_direct_match(self, tmp_path):
        f = tmp_path / "test.mkv"
        f.write_text("x")
        with patch("src.quality.get_video_pix_fmt", return_value="p010le"):
            assert _hw_download_format(str(f), "/ffprobe") == "p010le"


class TestReadVmafFromJson:
    def test_reads_mean_and_min(self, tmp_path):
        log = tmp_path / "vmaf.json"
        log.write_text(json.dumps({"pooled_metrics": {"vmaf": {"mean": 92.5, "min": 88.1}}}))
        mean, minimum = _read_vmaf_from_json(log)
        assert mean == 92.5
        assert minimum == 88.1

    def test_missing_pooled_metrics(self, tmp_path):
        log = tmp_path / "vmaf.json"
        log.write_text(json.dumps({"frames": []}))
        mean, minimum = _read_vmaf_from_json(log)
        assert mean is None
        assert minimum is None

    def test_missing_vmaf_key(self, tmp_path):
        log = tmp_path / "vmaf.json"
        log.write_text(json.dumps({"pooled_metrics": {}}))
        mean, minimum = _read_vmaf_from_json(log)
        assert mean is None
        assert minimum is None

    def test_bad_json(self, tmp_path):
        log = tmp_path / "vmaf.json"
        log.write_text("not json")
        mean, minimum = _read_vmaf_from_json(log)
        assert mean is None
        assert minimum is None

    def test_missing_file(self, tmp_path):
        log = tmp_path / "nonexistent.json"
        mean, minimum = _read_vmaf_from_json(log)
        assert mean is None
        assert minimum is None


class TestFrameCountsCheck:
    def test_returns_none_always(self):
        """纯日志函数：任何输入都返回 None，不阻断打分。"""
        assert _frame_counts_check(1826, 1826) is None
        assert _frame_counts_check(1000, 1011) is None
        assert _frame_counts_check(None, 1826) is None
        assert _frame_counts_check(None, None) is None

    def test_logs_unavailable(self, caplog):
        with caplog.at_level(logging.INFO):
            _frame_counts_check(None, 1826)
        assert "unavailable" in caplog.text

    def test_logs_mismatch(self, caplog):
        with caplog.at_level(logging.INFO):
            _frame_counts_check(10000, 10200)
        assert "mismatch" in caplog.text

    def test_logs_counts(self, caplog):
        with caplog.at_level(logging.INFO):
            _frame_counts_check(1826, 1826)
        assert "1826" in caplog.text


class TestCleanupVmafLogs:
    def test_deletes_expired_logs(self, tmp_path):
        with patch("src.quality.config.VMAF_LOG_DIR", str(tmp_path)):
            old = tmp_path / "old.json"
            old.write_text("x")
            old_time = time.time() - (config.VMAF_LOG_RETENTION_DAYS + 1) * 86400
            os_mod_time = MagicMock(return_value=old_time)
            with patch("os.path.getmtime", os_mod_time):
                count = cleanup_vmaf_logs()
            assert count == 1
            assert not old.exists()

    def test_skips_recent_logs(self, tmp_path):
        with patch("src.quality.config.VMAF_LOG_DIR", str(tmp_path)):
            recent = tmp_path / "recent.json"
            recent.write_text("x")
            count = cleanup_vmaf_logs()
            assert count == 0
            assert recent.exists()

    def test_skips_non_json_files(self, tmp_path):
        with patch("src.quality.config.VMAF_LOG_DIR", str(tmp_path)):
            (tmp_path / "log.txt").write_text("x")
            count = cleanup_vmaf_logs()
            assert count == 0

    def test_nonexistent_dir(self):
        with patch("src.quality.config.VMAF_LOG_DIR", "/nonexistent/vmaf_logs"):
            count = cleanup_vmaf_logs()
            assert count == 0


class TestRunVmaf:
    def test_sw_success_from_json(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        log_path = tmp_path / "out.json"

        with patch("src.quality.config.VMAF_MODEL_PATH", None):
            result = MagicMock()
            result.returncode = 0
            result.stderr = b""
            result.stdout = b""
            with patch("subprocess.run", return_value=result):
                with patch("src.quality._read_vmaf_from_json", return_value=(94.0, 90.0)):
                    score = _run_vmaf(
                        ref,
                        dist,
                        "/ffmpeg",
                        log_path,
                        hwaccel=False,
                        ref_frame_count=1,
                        dist_frame_count=1,
                    )
        assert score == 94.0

    def test_json_pooled_wins_over_stderr(self, tmp_path):
        """分数唯一来源是 json pooled_metrics.vmaf.mean，不再解析 stderr。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        log_path = tmp_path / "out.json"
        log_path.write_text(json.dumps({"pooled_metrics": {"vmaf": {"mean": 95.6, "min": 90.0}}}))

        with patch("src.quality.config.VMAF_MODEL_PATH", None):
            result = MagicMock()
            result.returncode = 0
            result.stderr = b"VMAF score: 86.542581"
            result.stdout = b""
            with patch("subprocess.run", return_value=result):
                with patch("src.quality._read_vmaf_from_json", return_value=(95.6, 90.0)):
                    score = _run_vmaf(
                        ref,
                        dist,
                        "/ffmpeg",
                        log_path,
                        hwaccel=False,
                        ref_frame_count=1,
                        dist_frame_count=1,
                    )
        assert score == 95.6

    def test_no_log_path_returns_none_when_json_unavailable(self, tmp_path):
        """无 log_path 且 rc=0 时，stderr 不再取分，返回 None。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_MODEL_PATH", None):
            result = MagicMock()
            result.returncode = 0
            result.stderr = b"VMAF score: 88.5"
            result.stdout = b""
            with patch("subprocess.run", return_value=result):
                with patch("src.quality._read_vmaf_from_json", return_value=(None, None)):
                    score = _run_vmaf(
                        ref,
                        dist,
                        "/ffmpeg",
                        None,
                        hwaccel=False,
                        ref_frame_count=1,
                        dist_frame_count=1,
                    )
        assert score is None

    def test_nonzero_returncode(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        result = MagicMock()
        result.returncode = 1
        with patch("subprocess.run", return_value=result):
            score = _run_vmaf(
                ref,
                dist,
                "/ffmpeg",
                None,
                hwaccel=False,
                ref_frame_count=1,
                dist_frame_count=1,
            )
        assert score is None

    def test_hw_filter_complex_contains_hwdownload(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        log_path = tmp_path / "out.json"

        with patch("src.quality.config.VMAF_MODEL_PATH", None):
            with patch("src.quality._hw_download_format", return_value="nv12"):
                result = MagicMock()
                result.returncode = 0
                result.stderr = b""
                result.stdout = b""
                with patch("subprocess.run", return_value=result) as mock_run:
                    with patch("src.quality._read_vmaf_from_json", return_value=(None, None)):
                        _run_vmaf(
                            ref,
                            dist,
                            "/ffmpeg",
                            log_path,
                            hwaccel=True,
                            ffprobe_path="/ffprobe",
                            ref_frame_count=1,
                            dist_frame_count=1,
                        )

        call_args = mock_run.call_args[0][0]
        filter_idx = call_args.index("-filter_complex")
        assert "hwdownload" in call_args[filter_idx + 1]
        assert "-hwaccel" in call_args
        assert "qsv" in call_args

    def test_frame_index_align_when_counts_equal(self, tmp_path):
        """两路帧数相等（帧序对齐前提）时，两个输入都用 setpts=N/30/TB
        按帧序号线性化，不插 fps 滤镜（与 tbr/帧率网格无关）。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_MODEL_PATH", None):
            result = MagicMock(returncode=0, stderr=b"", stdout=b"")
            with patch("subprocess.run", return_value=result) as mock_run:
                with patch("src.quality._read_vmaf_from_json", return_value=(None, None)):
                    _run_vmaf(
                        ref,
                        dist,
                        "/ffmpeg",
                        None,
                        hwaccel=False,
                        ffprobe_path="/ffprobe",
                        ref_frame_count=1826,
                        dist_frame_count=1826,
                    )

        call_args = mock_run.call_args[0][0]
        fc = call_args[call_args.index("-filter_complex") + 1]
        assert fc.count("setpts=N/30/TB") == 2
        assert "fps=" not in fc
        assert "[d0][r0]scale2ref" in fc

    def test_scores_despite_count_mismatch(self, tmp_path):
        """两路帧数相对差 >1% 不再阻断打分：仍调用 ffmpeg 帧序对齐打分。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_MODEL_PATH", None):
            with patch("subprocess.run") as mock_run:
                _run_vmaf(
                    ref,
                    dist,
                    "/ffmpeg",
                    None,
                    hwaccel=False,
                    ffprobe_path="/ffprobe",
                    ref_frame_count=10000,
                    dist_frame_count=10200,
                )
        mock_run.assert_called()

    def test_scores_when_counts_unavailable(self, tmp_path):
        """任一帧数不可得（None）不再阻断打分：仍调用 ffmpeg。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_MODEL_PATH", None):
            with patch("subprocess.run") as mock_run:
                _run_vmaf(
                    ref,
                    dist,
                    "/ffmpeg",
                    None,
                    hwaccel=False,
                    ref_frame_count=None,
                    dist_frame_count=1826,
                )
        mock_run.assert_called()

    def test_frame_index_align_in_hw_filter_complex(self, tmp_path):
        """HW 路径：帧数相等时两个输入同样用 setpts=N/30/TB 帧序对齐。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_MODEL_PATH", None):
            with patch("src.quality._hw_download_format", return_value="nv12"):
                result = MagicMock(returncode=0, stderr=b"", stdout=b"")
                with patch("subprocess.run", return_value=result) as mock_run:
                    with patch("src.quality._read_vmaf_from_json", return_value=(None, None)):
                        _run_vmaf(
                            ref,
                            dist,
                            "/ffmpeg",
                            tmp_path / "o.json",
                            hwaccel=True,
                            ffprobe_path="/ffprobe",
                            ref_frame_count=1826,
                            dist_frame_count=1826,
                        )

        call_args = mock_run.call_args[0][0]
        fc = call_args[call_args.index("-filter_complex") + 1]
        assert fc.count("setpts=N/30/TB") == 2
        assert "fps=" not in fc
        assert "hwdownload" in fc

    def test_frame_index_align_permits_one_percent_tail_difference(self, tmp_path):
        """相对差 ≤1%（如 100 vs 99）仍走帧序对齐：帧序链路不因小尾部
        差异回退投影，短流截断由 libvmaf shortest=1/repeatlast=0 完成。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_MODEL_PATH", None):
            result = MagicMock(returncode=0, stderr=b"", stdout=b"")
            with patch("subprocess.run", return_value=result) as mock_run:
                with patch("src.quality._read_vmaf_from_json", return_value=(None, None)):
                    _run_vmaf(
                        ref,
                        dist,
                        "/ffmpeg",
                        None,
                        hwaccel=False,
                        ffprobe_path="/ffprobe",
                        ref_frame_count=100,
                        dist_frame_count=99,
                    )

        call_args = mock_run.call_args[0][0]
        fc = call_args[call_args.index("-filter_complex") + 1]
        assert fc.count("setpts=N/30/TB") == 2
        assert "fps=" not in fc

    def test_libvmaf_opts_pin_fqm_framesync_semantics(self, tmp_path):
        """libvmaf opts 恒带 shortest=1:repeatlast=0（与 ffmpeg-quality-metrics
        一致）：帧数不足时以短流为准截断、不重复最后一帧。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_MODEL_PATH", None):
            result = MagicMock(returncode=0, stderr=b"", stdout=b"")
            with patch("subprocess.run", return_value=result) as mock_run:
                with patch("src.quality._read_vmaf_from_json", return_value=(None, None)):
                    _run_vmaf(
                        ref,
                        dist,
                        "/ffmpeg",
                        None,
                        hwaccel=False,
                        ref_frame_count=1,
                        dist_frame_count=1,
                    )

        call_args = mock_run.call_args[0][0]
        fc = call_args[call_args.index("-filter_complex") + 1]
        assert "shortest=1:repeatlast=0" in fc

    def test_vmaf_timeout_is_passed(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_MODEL_PATH", None):
            result = MagicMock(returncode=0, stderr=b"", stdout=b"")
            with patch("subprocess.run", return_value=result) as mock_run:
                with patch("src.quality._read_vmaf_from_json", return_value=(94.0, 90.0)):
                    _run_vmaf(
                        ref,
                        dist,
                        "/ffmpeg",
                        tmp_path / "out.json",
                        hwaccel=False,
                        ref_frame_count=1,
                        dist_frame_count=1,
                    )

        assert mock_run.call_args[1]["timeout"] == config.VMAF_TIMEOUT


class TestComputeVmaf:
    def test_returns_none_when_no_ffmpeg_path(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        score = compute_vmaf(ref, dist, ffmpeg_path=None)
        assert score is None

    def test_generates_default_log_path(self, tmp_path):
        """未传 log_path 时自动生成到 VMAF_LOG_DIR 的 json 日志（取分来源）。"""
        ref = tmp_path / "My Video 1080p.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_HWACCEL", False):
            with patch("src.quality.config.VMAF_LOG_DIR", str(tmp_path / "vmaf_logs")):
                with patch("src.quality._run_vmaf", return_value=90.0) as mock_run:
                    score = compute_vmaf(ref, dist, ffmpeg_path="/ffmpeg")

        assert score == 90.0
        log_path = mock_run.call_args[0][3]
        assert log_path.parent == tmp_path / "vmaf_logs"
        assert log_path.name.startswith("My_Video_1080p.")
        assert log_path.name.endswith(".json")

    def test_hw_then_fallback(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_HWACCEL", True):
            with patch("src.quality.config.VMAF_HW_FALLBACK", True):
                with patch("src.quality._run_vmaf", side_effect=[None, 91.0]) as mock_run:
                    score = compute_vmaf(ref, dist, ffmpeg_path="/ffmpeg")

        assert score == 91.0
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0][1]["hwaccel"] is True
        assert mock_run.call_args_list[1][1]["hwaccel"] is False

    def test_hw_success_no_fallback(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_HWACCEL", True):
            with patch("src.quality.config.VMAF_HW_FALLBACK", True):
                with patch("src.quality._run_vmaf", return_value=96.0) as mock_run:
                    score = compute_vmaf(ref, dist, ffmpeg_path="/ffmpeg")

        assert score == 96.0
        assert mock_run.call_count == 1

    def test_hw_fail_no_fallback_returns_none(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_HWACCEL", True):
            with patch("src.quality.config.VMAF_HW_FALLBACK", False):
                with patch("src.quality._run_vmaf", return_value=None):
                    score = compute_vmaf(ref, dist, ffmpeg_path="/ffmpeg")

        assert score is None

    def test_sw_only_path(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_HWACCEL", False):
            with patch("src.quality._run_vmaf", return_value=90.0) as mock_run:
                score = compute_vmaf(ref, dist, ffmpeg_path="/ffmpeg")

        assert score == 90.0
        mock_run.assert_called_once()
        assert mock_run.call_args[1]["hwaccel"] is False

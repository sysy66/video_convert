import json
import time
from unittest.mock import MagicMock, patch

from src import config
from src.quality import (
    _hw_download_format,
    _read_vmaf_from_json,
    _run_vmaf,
    cleanup_vmaf_logs,
    compute_vmaf,
    should_accept,
)


class TestShouldAccept:
    def test_vmaf_none_required_true(self):
        with patch("src.quality.config.VMAF_REQUIRED", True):
            assert should_accept(None, 0.5) is False

    def test_vmaf_none_required_false_low_ratio(self):
        with patch("src.quality.config.VMAF_REQUIRED", False):
            assert should_accept(None, 0.5) is True

    def test_vmaf_none_required_false_high_ratio(self):
        with patch("src.quality.config.VMAF_REQUIRED", False):
            assert should_accept(None, 1.5) is False

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
                    score = _run_vmaf(ref, dist, "/ffmpeg", log_path, hwaccel=False)
        assert score == 94.0

    def test_sw_success_from_stderr(self, tmp_path):
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
                    score = _run_vmaf(ref, dist, "/ffmpeg", None, hwaccel=False)
        assert score == 88.5

    def test_nonzero_returncode(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        result = MagicMock()
        result.returncode = 1
        with patch("subprocess.run", return_value=result):
            score = _run_vmaf(ref, dist, "/ffmpeg", None, hwaccel=False)
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
                            ref, dist, "/ffmpeg", log_path, hwaccel=True, ffprobe_path="/ffprobe"
                        )

        call_args = mock_run.call_args[0][0]
        filter_idx = call_args.index("-filter_complex")
        assert "hwdownload" in call_args[filter_idx + 1]
        assert "-hwaccel" in call_args
        assert "qsv" in call_args

    def test_vmaf_timeout_is_passed(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        with patch("src.quality.config.VMAF_MODEL_PATH", None):
            result = MagicMock(returncode=0, stderr=b"", stdout=b"")
            with patch("subprocess.run", return_value=result) as mock_run:
                with patch("src.quality._read_vmaf_from_json", return_value=(94.0, 90.0)):
                    _run_vmaf(ref, dist, "/ffmpeg", tmp_path / "out.json", hwaccel=False)

        assert mock_run.call_args[1]["timeout"] == config.VMAF_TIMEOUT


class TestComputeVmaf:
    def test_returns_none_when_no_ffmpeg_path(self, tmp_path):
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")

        score = compute_vmaf(ref, dist, ffmpeg_path=None)
        assert score is None

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

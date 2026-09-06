"""quality 模块单元测试：VMAF 接受/拒绝判定与日志清理。

Unit tests for the quality module: accept/reject verdicts and log cleanup.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src.quality import (
    cleanup_vmaf_logs,
    should_accept,
    vmaf_verdict,
)

pytestmark = pytest.mark.unit


class TestVmafVerdict:
    def test_none_score_rejects(self):
        assert vmaf_verdict(None, 0.5) == "reject"

    def test_extreme_low_score_rejects(self):
        assert vmaf_verdict(55.0, 0.5) == "reject"
        assert vmaf_verdict(59.99, 1.0) == "reject"

    def test_ratio_over_max_rejects(self):
        with patch("src.quality.config.ACCEPT_RATIO_MAX", 1.00):
            assert vmaf_verdict(55.0, 1.1) == "reject"
            assert vmaf_verdict(None, 1.1) == "reject"

    def test_low_score_rejects(self):
        with patch("src.quality.config.VMAF_ACCEPT_LOW", 80):
            assert vmaf_verdict(70.0, 0.5) == "reject"

    def test_middle_good_ratio_accepts(self):
        with patch("src.quality.config.VMAF_ACCEPT_LOW", 80):
            with patch("src.quality.config.ACCEPT_RATIO_LOW", 0.75):
                with patch("src.quality.config.VMAF_ACCEPT_HIGH", 93):
                    assert vmaf_verdict(85.0, 0.5) == "accept"

    def test_middle_bad_ratio_rejects(self):
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

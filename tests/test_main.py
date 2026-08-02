import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import main
from src import config

pytestmark = pytest.mark.unit


def _reset_running():
    main._running = True


class TestSignalHandler:
    def test_sets_running_false(self):
        import signal

        main._running = True
        main.signal_handler(signal.SIGINT, None)
        assert main._running is False


class TestCliOverrides:
    def test_no_vmaf_sets_required_false(self):
        original = config.VMAF_REQUIRED
        try:
            with patch.object(sys, "argv", ["main", "--no-vmaf"]):
                main._apply_cli_overrides()
            assert config.VMAF_REQUIRED is False
        finally:
            config.VMAF_REQUIRED = original

    def test_vmaf_sets_required_true(self):
        original = config.VMAF_REQUIRED
        try:
            config.VMAF_REQUIRED = False
            with patch.object(sys, "argv", ["main", "--vmaf"]):
                main._apply_cli_overrides()
            assert config.VMAF_REQUIRED is True
        finally:
            config.VMAF_REQUIRED = original

    def test_default_leaves_required_unchanged(self):
        original = config.VMAF_REQUIRED
        try:
            config.VMAF_REQUIRED = True
            with patch.object(sys, "argv", ["main"]):
                main._apply_cli_overrides()
            assert config.VMAF_REQUIRED is True
        finally:
            config.VMAF_REQUIRED = original

    def test_encoder_adds_to_skip_codecs(self):
        original_encoder = config.VIDEO_ENCODER
        original_skip = config.SKIP_CODECS.copy()
        try:
            with patch.object(sys, "argv", ["main", "--encoder", "hevc"]):
                main._apply_cli_overrides()
            assert config.VIDEO_ENCODER == "hevc"
            assert "hevc" in config.SKIP_CODECS
        finally:
            config.VIDEO_ENCODER = original_encoder
            config.SKIP_CODECS = original_skip

    def test_skip_codecs_appends(self):
        original = config.SKIP_CODECS.copy()
        try:
            with patch.object(sys, "argv", ["main", "--skip-codecs", "vc1", "vp9"]):
                main._apply_cli_overrides()
            assert "vc1" in config.SKIP_CODECS
            assert "vp9" in config.SKIP_CODECS
        finally:
            config.SKIP_CODECS = original


class TestMainLoop:
    def test_calls_walker(self):
        _reset_running()

        def stop_after_walk(*_a, **_kw):
            main._running = False
            return iter([])

        with (
            patch("main.walk_directory", side_effect=stop_after_walk),
            patch("main.should_process"),
            patch("main.transcode"),
            patch("main.History"),
            patch("main.validate_config", return_value=([], [])),
            patch("main.cleanup_stale_temp_dirs"),
            patch("main.mark_stale_started_as_failed"),
            patch("main.cleanup_vmaf_logs"),
        ):
            main.main_loop()

        assert main._running is False

    def test_transcodes_found_files(self):
        _reset_running()

        results = []

        def yield_file(*_a, **_kw):
            return iter([Path("/a.mp4")])

        def fake_transcode(*args, **_kw):
            results.append(args[0])
            main._running = False
            return True

        with (
            patch("main.walk_directory", side_effect=yield_file),
            patch("main.should_process", return_value=True),
            patch("main.transcode", side_effect=fake_transcode) as mock_transcode,
            patch("main.History"),
            patch("main.validate_config", return_value=([], [])),
            patch("main.cleanup_stale_temp_dirs"),
            patch("main.mark_stale_started_as_failed"),
            patch("main.cleanup_vmaf_logs"),
        ):
            main.main_loop()

        mock_transcode.assert_called_once()
        assert results[0].name == "a.mp4"

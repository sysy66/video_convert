import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import main
from src import config

pytestmark = pytest.mark.unit


def _reset_running():
    main._running = True


class TestSetupLogging:
    def test_creates_log_dir(self, tmp_path, monkeypatch):
        log_file = str(tmp_path / "logs" / "app.log")
        monkeypatch.setattr(config, "LOG_FILE", log_file)
        main._setup_logging()
        assert (tmp_path / "logs").is_dir()

    def test_basic_config_called(self, monkeypatch):
        monkeypatch.setattr(config, "LOG_FILE", "/tmp/x/app.log")
        with patch("main.logging.basicConfig") as mock_basic:
            main._setup_logging()
        mock_basic.assert_called_once()


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


class TestMainEntry:
    def test_lock_acquired_registers_signals(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "logs" / "app.log"))
        (tmp_path / "logs").mkdir()
        history = MagicMock()
        with (
            patch("main._apply_cli_overrides"),
            patch("main._setup_logging"),
            patch("main.fcntl.flock"),
            patch("main.signal.signal") as mock_signal,
            patch("main.main_loop", return_value=history),
            patch("main.kill_active_ffmpeg") as mock_kill,
        ):
            main.main()

        mock_signal.assert_any_call(signal.SIGINT, main.signal_handler)
        mock_signal.assert_any_call(signal.SIGTERM, main.signal_handler)
        history.close.assert_called_once()
        mock_kill.assert_called_once()

    def test_lock_unavailable_exits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "logs" / "app.log"))
        (tmp_path / "logs").mkdir()
        with (
            patch("main._apply_cli_overrides"),
            patch("main._setup_logging"),
            patch("main.fcntl.flock", side_effect=BlockingIOError),
            patch("main.sys.exit", side_effect=SystemExit) as mock_exit,
            pytest.raises(SystemExit),
        ):
            main.main()

        mock_exit.assert_called_once_with(1)

    def test_lock_oserror_exits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "logs" / "app.log"))
        (tmp_path / "logs").mkdir()
        with (
            patch("main._apply_cli_overrides"),
            patch("main._setup_logging"),
            patch("main.fcntl.flock", side_effect=OSError("no lock")),
            patch("main.sys.exit", side_effect=SystemExit) as mock_exit,
            pytest.raises(SystemExit),
        ):
            main.main()

        mock_exit.assert_called_once_with(1)

    def test_main_loop_exception_reraised_and_cleanup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "logs" / "app.log"))
        (tmp_path / "logs").mkdir()
        with (
            patch("main._apply_cli_overrides"),
            patch("main._setup_logging"),
            patch("main.fcntl.flock"),
            patch("main.signal.signal"),
            patch("main.main_loop", side_effect=RuntimeError("boom")),
            patch("main.kill_active_ffmpeg") as mock_kill,
            pytest.raises(RuntimeError, match="boom"),
        ):
            main.main()

        mock_kill.assert_called_once()

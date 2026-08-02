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


@pytest.fixture(autouse=True)
def _restore_config():
    saved = {
        "LOG_FILE": config.LOG_FILE,
        "HISTORY_FILE": config.HISTORY_FILE,
        "FFMPEG_PATH": config.FFMPEG_PATH,
        "FFPROBE_PATH": config.FFPROBE_PATH,
        "VMAF_FFMPEG_PATH": config.VMAF_FFMPEG_PATH,
        "VMAF_MODEL_PATH": config.VMAF_MODEL_PATH,
        "SCAN_INTERVAL": config.SCAN_INTERVAL,
        "MAX_WORKERS": config.MAX_WORKERS,
        "VIDEO_ENCODER": config.VIDEO_ENCODER,
        "SKIP_CODECS": config.SKIP_CODECS.copy(),
        "VMAF_HWACCEL": config.VMAF_HWACCEL,
        "VMAF_HW_FALLBACK": config.VMAF_HW_FALLBACK,
        "VMAF_REQUIRED": config.VMAF_REQUIRED,
        "LIBRARIES": config.LIBRARIES,
        "_LIBRARIES_RAW": config._LIBRARIES_RAW,
    }
    yield
    for name, value in saved.items():
        setattr(config, name, value)


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


class TestCliOverridesPaths:
    def test_log_sets_file(self):
        with patch.object(sys, "argv", ["main", "--log", "/tmp/x.log"]):
            main._apply_cli_overrides()
        assert config.LOG_FILE == "/tmp/x.log"

    def test_history_sets_file(self):
        with patch.object(sys, "argv", ["main", "--history", "/tmp/h.db"]):
            main._apply_cli_overrides()
        assert config.HISTORY_FILE == "/tmp/h.db"

    def test_ffmpeg_sets_path(self):
        with patch.object(sys, "argv", ["main", "--ffmpeg", "/usr/bin/ffmpeg"]):
            main._apply_cli_overrides()
        assert config.FFMPEG_PATH == "/usr/bin/ffmpeg"

    def test_ffprobe_sets_path(self):
        with patch.object(sys, "argv", ["main", "--ffprobe", "/usr/bin/ffprobe"]):
            main._apply_cli_overrides()
        assert config.FFPROBE_PATH == "/usr/bin/ffprobe"

    def test_scan_interval_sets_value(self):
        with patch.object(sys, "argv", ["main", "--scan-interval", "60"]):
            main._apply_cli_overrides()
        assert config.SCAN_INTERVAL == 60

    def test_max_workers_sets_value(self):
        with patch.object(sys, "argv", ["main", "--max-workers", "4"]):
            main._apply_cli_overrides()
        assert config.MAX_WORKERS == 4

    def test_vmaf_ffmpeg_sets_path(self):
        with patch.object(sys, "argv", ["main", "--vmaf-ffmpeg", "/opt/ffmpeg/ffmpeg"]):
            main._apply_cli_overrides()
        assert config.VMAF_FFMPEG_PATH == "/opt/ffmpeg/ffmpeg"

    def test_vmaf_model_sets_path(self):
        with patch.object(sys, "argv", ["main", "--vmaf-model", "/tmp/model.json"]):
            main._apply_cli_overrides()
        assert config.VMAF_MODEL_PATH == "/tmp/model.json"

    def test_vmaf_hwaccel_enables(self):
        with patch.object(sys, "argv", ["main", "--vmaf-hwaccel"]):
            main._apply_cli_overrides()
        assert config.VMAF_HWACCEL is True

    def test_vmaf_hw_fallback_enables(self):
        with patch.object(sys, "argv", ["main", "--vmaf-hw-fallback"]):
            main._apply_cli_overrides()
        assert config.VMAF_HW_FALLBACK is True


class TestCliOverridesLibraries:
    def test_simple_path(self):
        with patch.object(sys, "argv", ["main", "--library", "lib1=/mnt/lib1"]):
            main._apply_cli_overrides()
        assert config.LIBRARIES == {
            "lib1": {"input": "/mnt/lib1", "output": "/mnt/lib1", "cache": "/mnt/lib1"}
        }

    def test_three_part_path(self):
        with patch.object(sys, "argv", ["main", "--library", "lib1=/mnt/in:/mnt/out:/mnt/cache"]):
            main._apply_cli_overrides()
        assert config.LIBRARIES == {
            "lib1": {"input": "/mnt/in", "output": "/mnt/out", "cache": "/mnt/cache"}
        }

    def test_multiple_libraries(self):
        with patch.object(sys, "argv", ["main", "--library", "a=/mnt/a", "--library", "b=/mnt/b"]):
            main._apply_cli_overrides()
        assert set(config.LIBRARIES) == {"a", "b"}

    def test_missing_equals_exits(self):
        with (
            patch.object(sys, "argv", ["main", "--library", "lib1"]),
            patch("main.sys.exit", side_effect=SystemExit) as mock_exit,
            pytest.raises(SystemExit),
        ):
            main._apply_cli_overrides()
        mock_exit.assert_called_once_with(1)

    def test_invalid_parts_exits(self):
        with (
            patch.object(sys, "argv", ["main", "--library", "lib1=/mnt/in:/mnt/out"]),
            patch("main.sys.exit", side_effect=SystemExit) as mock_exit,
            pytest.raises(SystemExit),
        ):
            main._apply_cli_overrides()
        mock_exit.assert_called_once_with(1)


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

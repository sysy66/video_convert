import signal
from pathlib import Path
from unittest.mock import patch, MagicMock

import main


def _reset_running():
    main._running = True


def _fake_history(**kwargs):
    h = MagicMock(**kwargs)
    return h


class TestSignalHandler:
    def test_sets_running_false(self):
        main._running = True
        main.signal_handler(signal.SIGINT, None)
        assert main._running is False


class TestMainLoop:
    def test_calls_scan(self):
        _reset_running()

        def stop_after_scan(*args, **kwargs):
            main._running = False
            return []

        with (
            patch("main.scan", side_effect=stop_after_scan),
            patch("main.transcode"),
            patch("main.History") as mock_history_cls,
            patch("main.cleanup_stale_temp_dirs"),
        ):
            mock_history = _fake_history()
            mock_history.get_last_full_scan.return_value = 0
            mock_history_cls.return_value = mock_history
            main.main_loop()

        assert main._running is False

    def test_full_scan_on_first_run(self):
        _reset_running()

        def stop_after_scan(*args, **kwargs):
            main._running = False
            return []

        with (
            patch("main.scan", side_effect=stop_after_scan) as mock_scan,
            patch("main.transcode"),
            patch("main.History") as mock_history_cls,
            patch("main.cleanup_stale_temp_dirs"),
        ):
            mock_history = _fake_history()
            mock_history.get_last_full_scan.return_value = 0
            mock_history_cls.return_value = mock_history
            main.main_loop()

        mock_scan.assert_called_once()
        pos_args, _ = mock_scan.call_args
        assert pos_args[5] is True

    def test_incremental_scan_when_recent_full_scan(self):
        _reset_running()

        def stop_after_scan(*args, **kwargs):
            main._running = False
            return []

        with (
            patch("main.scan", side_effect=stop_after_scan) as mock_scan,
            patch("main.transcode"),
            patch("main.History") as mock_history_cls,
            patch("main.cleanup_stale_temp_dirs"),
        ):
            mock_history = _fake_history()
            mock_history.get_last_full_scan.return_value = 9999999999
            mock_history_cls.return_value = mock_history
            main.main_loop()

        pos_args, _ = mock_scan.call_args
        assert pos_args[5] is False

    def test_sets_last_full_scan_after_full_scan(self):
        _reset_running()

        def stop_after_scan(*args, **kwargs):
            main._running = False
            return []

        with (
            patch("main.scan", side_effect=stop_after_scan),
            patch("main.transcode"),
            patch("main.History") as mock_history_cls,
            patch("main.cleanup_stale_temp_dirs"),
        ):
            mock_history = _fake_history()
            mock_history.get_last_full_scan.return_value = 0
            mock_history_cls.return_value = mock_history
            main.main_loop()

        mock_history.set_last_full_scan.assert_called_once()

    def test_transcodes_found_files(self):
        _reset_running()

        def stop_after_scan(*args, **kwargs):
            main._running = False
            return [Path("/a.mp4")]

        def fake_transcode(*args, **kwargs):
            return True

        with (
            patch("main.scan", side_effect=stop_after_scan),
            patch("main.transcode", side_effect=fake_transcode) as mock_transcode,
            patch("main.History") as mock_history_cls,
            patch("main.cleanup_stale_temp_dirs"),
        ):
            mock_history = _fake_history()
            mock_history.get_last_full_scan.return_value = 0
            mock_history_cls.return_value = mock_history
            main.main_loop()

        mock_transcode.assert_called_once()

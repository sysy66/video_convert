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
            patch("main.History"),
            patch("main.cleanup_stale_temp_dirs"),
        ):
            main.main_loop()

        assert main._running is False

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
            patch("main.History"),
            patch("main.cleanup_stale_temp_dirs"),
        ):
            main.main_loop()

        mock_transcode.assert_called_once()

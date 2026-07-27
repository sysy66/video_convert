from pathlib import Path
from unittest.mock import patch

import main


def _reset_running():
    main._running = True


class TestSignalHandler:
    def test_sets_running_false(self):
        import signal

        main._running = True
        main.signal_handler(signal.SIGINT, None)
        assert main._running is False


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

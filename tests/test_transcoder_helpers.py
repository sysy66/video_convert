import signal
from unittest.mock import MagicMock, patch

import pytest

from src import transcoder

pytestmark = pytest.mark.unit


def _clear_pids():
    with transcoder._active_ffmpeg_lock:
        transcoder._active_ffmpeg_pids.clear()


def _clear_active():
    with transcoder._active_transcodes_lock:
        transcoder._active_transcodes.clear()


class TestFfmpegPidRegistry:
    def test_register_adds_pid(self):
        _clear_pids()
        transcoder.register_ffmpeg_pid(123)
        assert 123 in transcoder._active_ffmpeg_pids

    def test_unregister_removes_pid(self):
        _clear_pids()
        transcoder.register_ffmpeg_pid(123)
        transcoder.unregister_ffmpeg_pid(123)
        assert 123 not in transcoder._active_ffmpeg_pids

    def test_unregister_unknown_is_idempotent(self):
        _clear_pids()
        transcoder.unregister_ffmpeg_pid(999)
        assert transcoder._active_ffmpeg_pids == set()


class TestKillActiveFfmpeg:
    def test_no_pids_returns_early(self):
        _clear_pids()
        with patch("src.process_guard.os.kill") as mock_kill:
            transcoder.kill_active_ffmpeg()
        mock_kill.assert_not_called()

    def test_terminate_then_kill_after_sleep(self):
        _clear_pids()
        transcoder.register_ffmpeg_pid(123)
        with (
            patch("src.process_guard.os.kill") as mock_kill,
            patch("src.process_guard._time.sleep") as mock_sleep,
        ):
            transcoder.kill_active_ffmpeg()
        mock_kill.assert_any_call(123, signal.SIGTERM)
        mock_kill.assert_any_call(123, signal.SIGKILL)
        mock_sleep.assert_called_once_with(2)

    def test_process_lookup_error_ignored(self):
        _clear_pids()
        transcoder.register_ffmpeg_pid(123)

        def raise_lookup(*_a):
            raise ProcessLookupError

        with (
            patch("src.process_guard.os.kill", side_effect=raise_lookup),
            patch("src.process_guard._time.sleep"),
        ):
            transcoder.kill_active_ffmpeg()

    def test_oserror_logs_warning(self):
        _clear_pids()
        transcoder.register_ffmpeg_pid(123)

        def raise_oserror(*_a):
            raise OSError("permission denied")

        with (
            patch("src.process_guard.os.kill", side_effect=raise_oserror),
            patch("src.process_guard._time.sleep"),
        ):
            transcoder.kill_active_ffmpeg()


class TestMarkStaleStartedAsFailed:
    def test_marks_stale_entries(self):
        _clear_active()
        history = MagicMock()
        history.iter_started.return_value = ["/a.mp4", "/b.mp4"]
        transcoder.mark_stale_started_as_failed(history)
        assert history.mark_failed.call_count == 2
        history.mark_failed.assert_any_call("/a.mp4", "Stale: process interrupted, will retry")
        history.mark_failed.assert_any_call("/b.mp4", "Stale: process interrupted, will retry")

    def test_skips_active_transcodes(self):
        with transcoder._active_transcodes_lock:
            transcoder._active_transcodes.add("/a.mp4")
        try:
            history = MagicMock()
            history.iter_started.return_value = ["/a.mp4", "/b.mp4"]
            transcoder.mark_stale_started_as_failed(history)
            history.mark_failed.assert_called_once()
            history.mark_failed.assert_any_call("/b.mp4", "Stale: process interrupted, will retry")
        finally:
            _clear_active()

    def test_no_stale_no_calls(self):
        _clear_active()
        history = MagicMock()
        history.iter_started.return_value = []
        transcoder.mark_stale_started_as_failed(history)
        history.mark_failed.assert_not_called()


class TestUniquePath:
    def test_returns_path_when_free(self, tmp_path):
        p = tmp_path / "a.mkv"
        assert transcoder._unique_path(p) == p

    def test_appends_counter_when_exists(self, tmp_path):
        p = tmp_path / "a.mkv"
        p.write_text("x")
        (tmp_path / "a.1.mkv").write_text("y")
        assert transcoder._unique_path(p) == tmp_path / "a.2.mkv"

    def test_increments_until_free(self, tmp_path):
        p = tmp_path / "a.mkv"
        p.write_text("x")
        for i in range(1, 5):
            (tmp_path / f"a.{i}.mkv").write_text("y")
        assert transcoder._unique_path(p) == tmp_path / "a.5.mkv"


class TestFormatSize:
    def test_bytes(self):
        assert transcoder._format_size(512) == "512.0 B"

    def test_kilobytes(self):
        assert transcoder._format_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert transcoder._format_size(5 * 1024**2) == "5.0 MB"

    def test_gigabytes(self):
        assert transcoder._format_size(3 * 1024**3) == "3.0 GB"

    def test_terabytes(self):
        assert transcoder._format_size(2 * 1024**4) == "2.0 TB"

    def test_negative_values(self):
        assert transcoder._format_size(-1024) == "-1.0 KB"


class TestRandomSuffix:
    def test_default_length(self):
        assert len(transcoder._random_suffix()) == 6

    def test_custom_length(self):
        assert len(transcoder._random_suffix(12)) == 12

    def test_charset_limited(self):
        import string

        suffix = transcoder._random_suffix(100)
        allowed = set(string.ascii_lowercase + string.digits)
        assert all(c in allowed for c in suffix)


class TestParseProgressTime:
    def test_parses_hms(self):
        assert transcoder._parse_progress_time("time=00:01:30.5") == 90.5

    def test_parses_hours(self):
        assert transcoder._parse_progress_time("time=01:00:00") == 3600.0

    def test_no_match_returns_none(self):
        assert transcoder._parse_progress_time("frame=123 fps=30.0") is None

    def test_does_not_match_plain_seconds(self):
        assert transcoder._parse_progress_time("time=123.5") is None


class TestParseProgressFps:
    def test_parses_fps(self):
        assert transcoder._parse_progress_fps("fps=30.5") == 30.5

    def test_parses_whitespace(self):
        assert transcoder._parse_progress_fps("fps=  15.0") == 15.0

    def test_no_match_returns_none(self):
        assert transcoder._parse_progress_fps("time=00:00:01") is None


class TestRmtreeRobust:
    def test_removes_normal_dir(self, tmp_path):
        d = tmp_path / "work"
        d.mkdir()
        (d / "f").write_text("x")
        assert transcoder._rmtree_robust(d) is True
        assert not d.exists()

    def test_falls_back_to_manual_removal(self, tmp_path):
        d = tmp_path / "work"
        d.mkdir()
        (d / "f").write_text("x")

        def fail_once(*_a, **_kw):
            raise OSError("busy")

        with patch("shutil.rmtree", side_effect=fail_once):
            assert transcoder._rmtree_robust(d) is True
        assert not d.exists()

    def test_all_strategies_fail_returns_false(self, tmp_path):
        d = tmp_path / "work"
        d.mkdir()

        def fail(*_a, **_kw):
            raise OSError("stubborn")

        with (
            patch("shutil.rmtree", side_effect=fail),
            patch("os.listdir", side_effect=fail),
            patch(
                "src.temp_cleanup.subprocess.run",
                side_effect=OSError("no rm"),
            ),
        ):
            assert transcoder._rmtree_robust(d) is False
        assert d.exists()

    def test_file_entry_handled(self, tmp_path):
        d = tmp_path / "work"
        d.mkdir()
        (d / "f").write_text("x")
        with patch("shutil.rmtree", side_effect=[OSError("busy"), None]):
            assert transcoder._rmtree_robust(d) is True


class TestSummarizeErrorStderr:
    def test_drops_frame_progress_lines(self):
        stderr = (
            "ffmpeg version n8.1.2\n"
            "frame=  101 fps=0.0 q=-0.0 size=       1KiB time=00:00:03.33\n"
            "frame=  185 fps=185 q=-0.0 size=     251KiB\n"
            "Error while decoding stream\n"
        )
        result = transcoder._summarize_error_stderr(stderr)
        assert "frame=" not in result
        assert "ffmpeg version n8.1.2" in result
        assert "Error while decoding stream" in result

    def test_keeps_short_output_unchanged(self):
        stderr = "line one\nline two\n"
        assert transcoder._summarize_error_stderr(stderr) == "line one\nline two"

    def test_truncates_long_output_keeps_head_and_tail(self):
        head = [f"head-{i}" for i in range(50)]
        tail = [f"tail-{i}" for i in range(80)]
        stderr = "\n".join(head + tail)
        result = transcoder._summarize_error_stderr(stderr)
        lines = result.splitlines()
        assert len(lines) == 100
        assert lines[0] == "head-0"
        assert lines[-1] == "tail-79"
        assert any("truncated" in line for line in lines)

    def test_empty_stderr_returns_empty(self):
        assert transcoder._summarize_error_stderr("") == ""

    def test_whitespace_only_stderr_returns_empty(self):
        assert transcoder._summarize_error_stderr("   \n  \n") == ""

"""transcoder 辅助函数单元测试：进度解析、路径处理与进程管理辅助。

Unit tests for transcoder helper functions: progress parsing, path handling, process helpers.
"""

import signal
from unittest.mock import patch

import pytest

from src import ffmpeg_runner, path_utils, process_guard, temp_cleanup

pytestmark = pytest.mark.unit


def _clear_pids():
    with process_guard._active_ffmpeg_lock:
        process_guard._active_ffmpeg_pids.clear()


class TestFfmpegPidRegistry:
    def test_register_adds_pid(self):
        _clear_pids()
        process_guard.register_ffmpeg_pid(123)
        assert 123 in process_guard._active_ffmpeg_pids

    def test_unregister_removes_pid(self):
        _clear_pids()
        process_guard.register_ffmpeg_pid(123)
        process_guard.unregister_ffmpeg_pid(123)
        assert 123 not in process_guard._active_ffmpeg_pids

    def test_unregister_unknown_is_idempotent(self):
        _clear_pids()
        process_guard.unregister_ffmpeg_pid(999)
        assert process_guard._active_ffmpeg_pids == set()


class TestKillActiveFfmpeg:
    def test_no_pids_returns_early(self):
        _clear_pids()
        with patch("src.process_guard.os.kill") as mock_kill:
            process_guard.kill_active_ffmpeg()
        mock_kill.assert_not_called()

    def test_terminate_then_kill_after_sleep(self):
        _clear_pids()
        process_guard.register_ffmpeg_pid(123)
        with (
            patch("src.process_guard.os.kill") as mock_kill,
            patch("src.process_guard._time.sleep") as mock_sleep,
        ):
            process_guard.kill_active_ffmpeg()
        mock_kill.assert_any_call(123, signal.SIGTERM)
        mock_kill.assert_any_call(123, signal.SIGKILL)
        mock_sleep.assert_called_once_with(2)

    def test_process_lookup_error_ignored(self):
        _clear_pids()
        process_guard.register_ffmpeg_pid(123)

        def raise_lookup(*_a):
            raise ProcessLookupError

        with (
            patch("src.process_guard.os.kill", side_effect=raise_lookup),
            patch("src.process_guard._time.sleep"),
        ):
            process_guard.kill_active_ffmpeg()

    def test_oserror_logs_warning(self):
        _clear_pids()
        process_guard.register_ffmpeg_pid(123)

        def raise_oserror(*_a):
            raise OSError("permission denied")

        with (
            patch("src.process_guard.os.kill", side_effect=raise_oserror),
            patch("src.process_guard._time.sleep"),
        ):
            process_guard.kill_active_ffmpeg()


class TestUniquePath:
    def test_returns_path_when_free(self, tmp_path):
        p = tmp_path / "a.mkv"
        assert path_utils._unique_path(p) == p

    def test_appends_counter_when_exists(self, tmp_path):
        p = tmp_path / "a.mkv"
        p.write_text("x")
        (tmp_path / "a.1.mkv").write_text("y")
        assert path_utils._unique_path(p) == tmp_path / "a.2.mkv"

    def test_increments_until_free(self, tmp_path):
        p = tmp_path / "a.mkv"
        p.write_text("x")
        for i in range(1, 5):
            (tmp_path / f"a.{i}.mkv").write_text("y")
        assert path_utils._unique_path(p) == tmp_path / "a.5.mkv"


class TestFormatSize:
    def test_bytes(self):
        assert path_utils._format_size(512) == "512.0 B"

    def test_kilobytes(self):
        assert path_utils._format_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert path_utils._format_size(5 * 1024**2) == "5.0 MB"

    def test_gigabytes(self):
        assert path_utils._format_size(3 * 1024**3) == "3.0 GB"

    def test_terabytes(self):
        assert path_utils._format_size(2 * 1024**4) == "2.0 TB"

    def test_negative_values(self):
        assert path_utils._format_size(-1024) == "-1.0 KB"


class TestRandomSuffix:
    def test_default_length(self):
        assert len(path_utils._random_suffix()) == 6

    def test_custom_length(self):
        assert len(path_utils._random_suffix(12)) == 12

    def test_charset_limited(self):
        import string

        suffix = path_utils._random_suffix(100)
        allowed = set(string.ascii_lowercase + string.digits)
        assert all(c in allowed for c in suffix)


class TestParseProgressTime:
    def test_parses_hms(self):
        assert ffmpeg_runner._parse_progress_time("time=00:01:30.5") == 90.5

    def test_parses_hours(self):
        assert ffmpeg_runner._parse_progress_time("time=01:00:00") == 3600.0

    def test_no_match_returns_none(self):
        assert ffmpeg_runner._parse_progress_time("frame=123 fps=30.0") is None

    def test_does_not_match_plain_seconds(self):
        assert ffmpeg_runner._parse_progress_time("time=123.5") is None


class TestParseProgressFps:
    def test_parses_fps(self):
        assert ffmpeg_runner._parse_progress_fps("fps=30.5") == 30.5

    def test_parses_whitespace(self):
        assert ffmpeg_runner._parse_progress_fps("fps=  15.0") == 15.0

    def test_no_match_returns_none(self):
        assert ffmpeg_runner._parse_progress_fps("time=00:00:01") is None


class TestParseFrameCountFromStderr:
    def test_takes_last_frame_line(self):
        stderr = "\n".join(
            [
                "Stream mapping:",
                "frame=  100 fps=150.0",
                "frame=  5000 fps=149.2",
                "frame=216315 fps=148.0 q=34.0 size=2487040",
                "video:216315kB audio:123kB",
            ]
        )
        assert ffmpeg_runner.parse_frame_count_from_stderr(stderr) == 216315

    def test_spaces_after_equals(self):
        assert ffmpeg_runner.parse_frame_count_from_stderr("frame= 12345 fps=30.0\n") == 12345

    def test_frame_na_ignored(self):
        # "frame=N/A"（avformat 统计）行不匹配数字，也不影响后续解析
        stderr = "frame=N/A\nframe=123\n"
        assert ffmpeg_runner.parse_frame_count_from_stderr(stderr) == 123

    def test_no_frame_line_returns_none(self):
        assert ffmpeg_runner.parse_frame_count_from_stderr("nothing here\n") is None

    def test_empty_stderr_returns_none(self):
        assert ffmpeg_runner.parse_frame_count_from_stderr("") is None


class TestRmtreeRobust:
    def test_removes_normal_dir(self, tmp_path):
        d = tmp_path / "work"
        d.mkdir()
        (d / "f").write_text("x")
        assert temp_cleanup._rmtree_robust(d) is True
        assert not d.exists()

    def test_falls_back_to_manual_removal(self, tmp_path):
        d = tmp_path / "work"
        d.mkdir()
        (d / "f").write_text("x")

        def fail_once(*_a, **_kw):
            raise OSError("busy")

        with patch("shutil.rmtree", side_effect=fail_once):
            assert temp_cleanup._rmtree_robust(d) is True
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
            assert temp_cleanup._rmtree_robust(d) is False
        assert d.exists()

    def test_file_entry_handled(self, tmp_path):
        d = tmp_path / "work"
        d.mkdir()
        (d / "f").write_text("x")
        with patch("shutil.rmtree", side_effect=[OSError("busy"), None]):
            assert temp_cleanup._rmtree_robust(d) is True


class TestSummarizeErrorStderr:
    def test_drops_frame_progress_lines(self):
        stderr = (
            "ffmpeg version n8.1.2\n"
            "frame=  101 fps=0.0 q=-0.0 size=       1KiB time=00:00:03.33\n"
            "frame=  185 fps=185 q=-0.0 size=     251KiB\n"
            "Error while decoding stream\n"
        )
        result = ffmpeg_runner._summarize_error_stderr(stderr)
        assert "frame=" not in result
        assert "ffmpeg version n8.1.2" in result
        assert "Error while decoding stream" in result

    def test_keeps_short_output_unchanged(self):
        stderr = "line one\nline two\n"
        assert ffmpeg_runner._summarize_error_stderr(stderr) == "line one\nline two"

    def test_truncates_long_output_keeps_head_and_tail(self):
        head = [f"head-{i}" for i in range(50)]
        tail = [f"tail-{i}" for i in range(80)]
        stderr = "\n".join(head + tail)
        result = ffmpeg_runner._summarize_error_stderr(stderr)
        lines = result.splitlines()
        assert len(lines) == 100
        assert lines[0] == "head-0"
        assert lines[-1] == "tail-79"
        assert any("truncated" in line for line in lines)

    def test_empty_stderr_returns_empty(self):
        assert ffmpeg_runner._summarize_error_stderr("") == ""

    def test_whitespace_only_stderr_returns_empty(self):
        assert ffmpeg_runner._summarize_error_stderr("   \n  \n") == ""

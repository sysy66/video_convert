import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from transcoder import transcode, _build_ffmpeg_cmd, _cleanup_temp, cleanup_stale_temp_dirs


class TestBuildFfmpegCmd:
    def test_uses_provided_paths(self):
        cmd = _build_ffmpeg_cmd("/my/ffmpeg", "/in.mp4", "/out.mkv")
        assert cmd[0] == "/my/ffmpeg"
        assert "/in.mp4" in cmd
        assert "/out.mkv" in cmd

    def test_contains_qsv_params(self):
        cmd = _build_ffmpeg_cmd("/ffmpeg", "/i.mp4", "/o.mkv")
        assert "-hwaccel" in cmd
        assert "qsv" in cmd
        assert "-hwaccel_output_format" in cmd

    def test_uses_hevc_qsv_encoder(self):
        cmd = _build_ffmpeg_cmd("/ffmpeg", "/i.mp4", "/o.mkv")
        idx = cmd.index("-c:v") if "-c:v" in cmd else -1
        assert idx >= 0
        assert cmd[idx + 1] == "hevc_qsv"

    def test_ends_with_output_path(self):
        cmd = _build_ffmpeg_cmd("/ffmpeg", "/i.mp4", "/o.mkv")
        assert cmd[-1] == "/o.mkv"

    def test_includes_audio_params(self):
        cmd = _build_ffmpeg_cmd("/ffmpeg", "/i.mp4", "/o.mkv")
        assert "-c:a" in cmd
        assert "aac" in cmd
        assert "-b:a" in cmd


class TestTranscode:
    def test_success_replaces_original(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        def fake_run(cmd, **kwargs):
            output_path = Path(cmd[-1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("transcoded")
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            result = transcode(str(input_file), history, "/ffmpeg")

        assert result is True
        assert not input_file.exists()
        assert (tmp_path / "test.mkv").exists()
        assert (tmp_path / "test.mkv").read_text() == "transcoded"

    def test_success_calls_mark_done(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_text("data")
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            transcode(str(input_file), history, "/ffmpeg")

        history.mark_started.assert_called_once()
        history.mark_done.assert_called_once()
        assert not history.mark_failed.called

    def test_ffmpeg_failure_cleans_up(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "encode error"
            result = transcode(str(input_file), history, "/ffmpeg")

        assert result is False
        assert input_file.exists()
        history.mark_failed.assert_called_once()
        work_dirs = [d for d in tmp_path.iterdir() if d.name.startswith(".work_")]
        assert len(work_dirs) == 0

    def test_timeout_cleans_up(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=3600)
            result = transcode(str(input_file), history, "/ffmpeg")

        assert result is False
        assert input_file.exists()
        history.mark_failed.assert_called_once()
        work_dirs = [d for d in tmp_path.iterdir() if d.name.startswith(".work_")]
        assert len(work_dirs) == 0

    def test_output_file_not_found(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            result = transcode(str(input_file), history, "/ffmpeg")

        assert result is False
        history.mark_failed.assert_called_once()

    def test_temp_dir_created_in_same_parent(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_text("data")
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            transcode(str(input_file), history, "/ffmpeg")

        work_dirs = [d for d in tmp_path.iterdir() if d.name.startswith(".work_")]
        assert len(work_dirs) == 0  # cleaned up after success

    def test_input_in_subdirectory(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        input_file = sub / "video.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_text("data")
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            result = transcode(str(input_file), history, "/ffmpeg")

        assert result is True
        assert not input_file.exists()
        assert (sub / "video.mkv").exists()
        work_dirs = [d for d in sub.iterdir() if d.name.startswith(".work_")]
        assert len(work_dirs) == 0


class TestCleanupTemp:
    def test_removes_existing_dir(self, tmp_path):
        d = tmp_path / ".work_abc123"
        d.mkdir()
        (d / "test.txt").write_text("x")
        _cleanup_temp(d)
        assert not d.exists()

    def test_no_error_on_nonexistent(self, tmp_path):
        d = tmp_path / ".work_nonexistent"
        _cleanup_temp(d)


class TestCleanupStaleTempDirs:
    def test_removes_work_dirs_in_tree(self, tmp_path):
        d1 = tmp_path / ".work_aaa"
        d1.mkdir()
        d2 = tmp_path / "sub" / ".work_bbb"
        d2.mkdir(parents=True)

        cleanup_stale_temp_dirs(str(tmp_path), ".work_")

        assert not d1.exists()
        assert not d2.exists()

    def test_skips_normal_dirs(self, tmp_path):
        (tmp_path / "movies").mkdir()
        (tmp_path / "tv").mkdir()
        cleanup_stale_temp_dirs(str(tmp_path), ".work_")
        assert (tmp_path / "movies").exists()
        assert (tmp_path / "tv").exists()

    def test_handles_nonexistent_watch_dir(self, tmp_path):
        cleanup_stale_temp_dirs(str(tmp_path / "nonexistent"), ".work_")

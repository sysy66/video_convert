import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.transcoder import transcode, _build_ffmpeg_cmd, _cleanup_temp, cleanup_stale_temp_dirs

_VIDEO_AAC = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
    ],
    "duration": 120.0,
}

_VIDEO_AC3 = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3"},
    ],
    "duration": 120.0,
}

_VIDEO_ONLY = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
    ],
    "duration": 60.0,
}


class TestBuildFfmpegCmd:
    def test_uses_provided_paths(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/my/ffmpeg", "/ffprobe", "/in.mp4", "/out.mkv")
        assert cmd[0] == "/my/ffmpeg"
        assert "/in.mp4" in cmd
        assert "/out.mkv" in cmd
        assert duration == 120.0

    def test_contains_qsv_params(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "-hwaccel" in cmd
        assert "qsv" in cmd
        assert "-hwaccel_output_format" in cmd

    def test_uses_hevc_qsv_encoder(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        idx = cmd.index("-c:v") if "-c:v" in cmd else -1
        assert idx >= 0
        assert cmd[idx + 1] == "hevc_qsv"

    def test_ends_with_output_path(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert cmd[-1] == "/o.mkv"

    def test_maps_first_video_stream_by_type(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:v:0" in cmd

    def test_maps_all_audio_streams(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:a?" in cmd

    def test_audio_passthrough_when_all_aac(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        a_idx = cmd.index("-c:a")
        assert cmd[a_idx + 1] == "copy"
        assert "-b:a" not in cmd

    def test_reencodes_audio_when_not_aac(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_AC3):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        a_idx = cmd.index("-c:a")
        assert cmd[a_idx + 1] == "aac"
        assert "-b:a" in cmd

    def test_handles_no_audio_gracefully(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_ONLY):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:a?" in cmd
        # Should still have audio codec params since no audio codecs to detect
        assert "-c:a" in cmd

    def test_includes_subtitle_mapping(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC):
            with patch("src.config.KEEP_SUBTITLES", True):
                cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:s?" in cmd
        assert "-c:s" in cmd
        assert "copy" in cmd[cmd.index("-c:s") + 1]

    def test_omits_subtitle_mapping_when_disabled(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC):
            with patch("src.config.KEEP_SUBTITLES", False):
                cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:s?" not in cmd

    def test_includes_attachment_mapping(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC):
            with patch("src.config.KEEP_ATTACHMENTS", True):
                cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:t?" in cmd
        assert "-c:t" in cmd

    def test_omits_attachment_mapping_when_disabled(self):
        with patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC):
            with patch("src.config.KEEP_ATTACHMENTS", False):
                cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:t?" not in cmd


class TestTranscode:
    def _fake_progress(self, cmd, total_duration, file_stem):
        output_path = Path(cmd[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("transcoded")
        return (0, "")

    def test_success_replaces_original(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with (
            patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", side_effect=self._fake_progress),
            patch("src.transcoder.compute_vmaf", return_value=95.0),
        ):
            result = transcode(str(input_file), history, "/ffmpeg", "/ffprobe")

        assert result is True
        assert not input_file.exists()
        assert (tmp_path / "test.mkv").exists()
        assert (tmp_path / "test.mkv").read_text() == "transcoded"

    def test_success_calls_mark_done(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with (
            patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", side_effect=self._fake_progress),
        ):
            transcode(str(input_file), history, "/ffmpeg", "/ffprobe")

        history.mark_started.assert_called_once()
        history.mark_done.assert_called_once()
        assert not history.mark_failed.called

    def test_ffmpeg_failure_cleans_up(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with (
            patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", return_value=(1, "encode error")),
        ):
            result = transcode(str(input_file), history, "/ffmpeg", "/ffprobe")

        assert result is False
        assert input_file.exists()
        history.mark_failed.assert_called_once()
        work_dirs = [d for d in tmp_path.iterdir() if d.name.startswith(".work_")]
        assert len(work_dirs) == 0

    def test_output_file_not_found(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with (
            patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", return_value=(0, "")),
        ):
            result = transcode(str(input_file), history, "/ffmpeg", "/ffprobe")

        assert result is False
        history.mark_failed.assert_called_once()

    def test_temp_dir_cleaned_after_success(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with (
            patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", side_effect=self._fake_progress),
        ):
            transcode(str(input_file), history, "/ffmpeg", "/ffprobe")

        work_dirs = [d for d in tmp_path.iterdir() if d.name.startswith(".work_")]
        assert len(work_dirs) == 0

    def test_input_in_subdirectory(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        input_file = sub / "video.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with (
            patch("src.transcoder.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", side_effect=self._fake_progress),
            patch("src.transcoder.compute_vmaf", return_value=95.0),
        ):
            result = transcode(str(input_file), history, "/ffmpeg", "/ffprobe")

        assert result is True
        assert not input_file.exists()
        assert (sub / "video.mkv").exists()
        work_dirs = [d for d in sub.iterdir() if d.name.startswith(".work_")]
        assert len(work_dirs) == 0

    def test_probe_failure_marks_failed(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with patch("src.transcoder.get_streams_info", return_value=None):
            result = transcode(str(input_file), history, "/ffmpeg", "/ffprobe")

        assert result is False
        history.mark_failed.assert_called_once()
        history.mark_done.assert_not_called()


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

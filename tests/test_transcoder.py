"""transcoder 模块单元测试：转码流程、参数构建与错误处理。

Unit tests for the transcoder module: transcode flow, argument building and errors.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src.ffmpeg_runner import _build_ffmpeg_cmd
from src.pipeline import TranscodePipeline
from src.temp_cleanup import (
    _active_temp_dirs,
    _active_temp_dirs_lock,
    _cleanup_temp,
    active_temp_dirs_snapshot,
    cleanup_stale_temp_dirs,
)
from src.transcoder import transcode_stage

pytestmark = pytest.mark.unit

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
    """测试 _build_ffmpeg_cmd（位于 ffmpeg_runner）：构建 QSV 转码命令行。

    Unit tests for _build_ffmpeg_cmd (defined in ffmpeg_runner): the
    QSV transcode command-line construction.
    """

    def test_uses_provided_paths(self):
        """使用调用方传入的 ffmpeg/ffprobe/输入/输出路径，并返回探测到的时长。

        Uses the caller-provided ffmpeg/ffprobe/input/output paths and returns
        the probed duration.
        """
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/my/ffmpeg", "/ffprobe", "/in.mp4", "/out.mkv")
        assert cmd[0] == "/my/ffmpeg"
        assert "/in.mp4" in cmd
        assert "/out.mkv" in cmd
        assert duration == 120.0

    def test_contains_qsv_params(self):
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "-hwaccel" in cmd
        assert "qsv" in cmd
        assert "-hwaccel_output_format" in cmd

    def test_uses_hevc_qsv_encoder(self):
        with (
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.config.VIDEO_ENCODER", "hevc"),
        ):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        idx = cmd.index("-c:v") if "-c:v" in cmd else -1
        assert idx >= 0
        assert cmd[idx + 1] == "hevc_qsv"
        assert "-bf" in cmd
        assert cmd[cmd.index("-bf") + 1] == "8"

    def test_uses_av1_qsv_encoder(self):
        with (
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.config.VIDEO_ENCODER", "av1"),
        ):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        idx = cmd.index("-c:v") if "-c:v" in cmd else -1
        assert idx >= 0
        assert cmd[idx + 1] == "av1_qsv"
        assert cmd[cmd.index("-profile:v") + 1] == "main"
        assert "-b_strategy" in cmd

    def test_ends_with_output_path(self):
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert cmd[-1] == "/o.mkv"

    def test_no_frame_rate_regularization(self):
        # 2026-08-13 全片实验：-r 导致帧序错位 VMAF 46 虚低、
        # -fps_mode cfr 产物双倍帧；转码命令不得插入任何帧率规整参数，
        # 对齐只发生在打分侧（帧序对齐）。
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC):
            cmd, _ = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "-r" not in cmd
        assert "-fps_mode" not in cmd
        assert cmd[-1] == "/o.mkv"

    def test_maps_first_video_stream_by_type(self):
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:v:0" in cmd

    def test_maps_all_audio_streams(self):
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:a?" in cmd

    def test_audio_passthrough_when_all_aac(self):
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        idx = cmd.index("-c:a:0")
        assert cmd[idx + 1] == "copy"
        assert not any(a.startswith("-b:a:") for a in cmd)

    def test_reencodes_audio_when_not_aac(self):
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AC3):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        idx = cmd.index("-c:a:0")
        assert cmd[idx + 1] == "aac"
        assert "-b:a:0" in cmd

    def test_handles_no_audio_gracefully(self):
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_ONLY):
            cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:a?" in cmd
        # 无音轨 → 不生成逐流音频参数
        assert not any(a.startswith("-c:a:") for a in cmd)
        assert not any(a.startswith("-b:a:") for a in cmd)

    def test_includes_subtitle_mapping(self):
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC):
            with patch("src.config.KEEP_SUBTITLES", True):
                cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:s?" in cmd
        assert "-c:s" in cmd
        assert "copy" in cmd[cmd.index("-c:s") + 1]

    def test_omits_subtitle_mapping_when_disabled(self):
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC):
            with patch("src.config.KEEP_SUBTITLES", False):
                cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:s?" not in cmd

    def test_includes_attachment_mapping(self):
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC):
            with patch("src.config.KEEP_ATTACHMENTS", True):
                cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:t?" in cmd
        assert "-c:t" in cmd

    def test_omits_attachment_mapping_when_disabled(self):
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC):
            with patch("src.config.KEEP_ATTACHMENTS", False):
                cmd, duration = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert "0:t?" not in cmd

    @pytest.mark.parametrize("codec", sorted(config.SW_DECODE_FALLBACK_CODECS))
    def test_sw_decode_fallback_forces_softdecode(self, codec):
        """sw_decode_fallback=True 时应强制软解：帧留系统内存（
        -hwaccel_output_format none）、在 -i 前加 -c:v <codec> 强制软解，
        并用软件 format 滤镜 + hwupload 上传。

        With sw_decode_fallback=True the command should force software decode:
        keep frames in system memory, force the input decoder with -c:v <codec>
        before -i, and upload via the software-format filter.
        """
        with (
            patch("src.ffmpeg_runner.get_video_codec", return_value=codec),
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
        ):
            cmd, _ = _build_ffmpeg_cmd(
                "/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv", sw_decode_fallback=True
            )
        assert "-hwaccel_output_format" in cmd
        assert "none" in cmd
        assert "-c:v" in cmd
        assert codec in cmd
        assert any(a.startswith("format=p010le,hwupload") for a in cmd)
        assert "vpp_qsv" not in cmd

    def test_sw_decode_fallback_with_unknown_codec(self):
        """sw_decode_fallback=True 但解码器探测失败（返回 None）时，
        仍走软解路径（hwupload），但无法强制 -c:v 解码器。

        With sw_decode_fallback=True but a failed codec probe (None), the
        software-decode path (hwupload) is still taken, but no -c:v decoder
        is forced.
        """
        with (
            patch("src.ffmpeg_runner.get_video_codec", return_value=None),
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
        ):
            cmd, _ = _build_ffmpeg_cmd(
                "/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv", sw_decode_fallback=True
            )
        assert "none" in cmd
        assert any(a.startswith("format=p010le,hwupload") for a in cmd)
        assert "vpp_qsv" not in cmd
        i_idx = cmd.index("-i")
        assert "-c:v" not in cmd[:i_idx]

    def test_sw_decode_fallback_not_triggered_when_disabled(self):
        """sw_decode_fallback 默认关闭时，即使探测到 fallback 编码（如 vc1）
        也不应触发软解，走 vpp_qsv 路径。

        With sw_decode_fallback off by default, a fallback codec (e.g. vc1)
        should not trigger software decode; the vpp_qsv path is used.
        """
        with (
            patch("src.ffmpeg_runner.get_video_codec", return_value="vc1"),
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
        ):
            cmd, _ = _build_ffmpeg_cmd("/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv")
        assert any(a.startswith("vpp_qsv") for a in cmd)
        assert not any(a.startswith("format=p010le,hwupload") for a in cmd)
        assert "vc1" not in cmd

    def test_sw_scale_fallback_uses_hwupload(self):
        """sw_scale_fallback=True 时应显式初始化 QSV 设备、帧留系统内存并用
        software format + hwupload 上传，绕开 vpp_qsv 缩放滤镜。

        With sw_scale_fallback=True the command should init the QSV device,
        keep frames in system memory and upload via software format + hwupload,
        bypassing the vpp_qsv scale filter.
        """
        with patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC):
            cmd, _ = _build_ffmpeg_cmd(
                "/ffmpeg", "/ffprobe", "/i.mp4", "/o.mkv", sw_scale_fallback=True
            )
        assert "none" in cmd
        assert "-init_hw_device" in cmd
        assert any(a.startswith("format=p010le,hwupload") for a in cmd)
        assert "vpp_qsv" not in cmd


class TestTranscode:
    def _cache(self, tmp_path):
        return str(tmp_path / "cache")

    def _fake_progress(self, cmd, total_duration, file_stem, **kwargs):
        output_path = Path(cmd[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("transcoded")
        return (0, "")

    def _work_dirs(self, tmp_path):
        return [d for d in (tmp_path / "cache").iterdir() if d.name.startswith(".work_")]

    def _run_transcode(self, tmp_path, history, input_file):
        pipeline = TranscodePipeline(history, "/ffmpeg", "/ffprobe", keep_source=True)
        job = pipeline.make_job(
            str(input_file),
            {
                "input": str(tmp_path),
                "output": str(tmp_path),
                "cache": self._cache(tmp_path),
            },
        )
        return transcode_stage(pipeline, job), job

    def test_success_keeps_output_in_cache(self, tmp_path):
        # 编码阶段成功后：产物保留在 cache 临时目录，源文件不动，返回三元组。
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with (
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", side_effect=self._fake_progress),
        ):
            result, job = self._run_transcode(tmp_path, history, input_file)

        assert result is True
        assert job.dist_frame_count is None
        assert job.elapsed is not None
        assert input_file.exists()
        work_dirs = self._work_dirs(tmp_path)
        assert len(work_dirs) == 1
        assert (work_dirs[0] / "test.mkv").read_text() == "transcoded"

    def test_success_calls_mark_pending_transcode(self, tmp_path):
        # 编码阶段只写 pending_transcode（含产物路径），不写 done，也不删源。
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with (
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", side_effect=self._fake_progress),
        ):
            self._run_transcode(tmp_path, history, input_file)

        history.mark_pending_transcode.assert_called_once()
        out_path = history.mark_pending_transcode.call_args[0][1]
        assert Path(out_path).parent.parent == Path(self._cache(tmp_path))
        assert Path(out_path).name == "test.mkv"
        history.mark_done.assert_not_called()
        assert not history.mark_failed.called

    def test_success_parses_frame_count_from_stderr(self, tmp_path):
        # 产物帧数取自 ffmpeg 最终进度行 frame=N。
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        def _progress_with_frames(cmd, total_duration, file_stem, **kwargs):
            output_path = Path(cmd[-1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("transcoded")
            return (0, "frame=  5000 fps=150.0\nframe=  5000 fps=149.2\n")

        with (
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", side_effect=_progress_with_frames),
        ):
            result, job = self._run_transcode(tmp_path, history, input_file)

        assert result is True
        assert job.dist_frame_count == 5000

    def test_ffmpeg_failure_cleans_up(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with (
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", return_value=(1, "encode error")),
        ):
            result, job = self._run_transcode(tmp_path, history, input_file)

        assert result is False
        assert input_file.exists()
        history.mark_failed.assert_called_once()
        assert len(self._work_dirs(tmp_path)) == 0

    def test_output_file_not_found(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with (
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", return_value=(0, "")),
        ):
            result, job = self._run_transcode(tmp_path, history, input_file)

        assert result is False
        history.mark_failed.assert_called_once()

    def test_probe_failure_marks_failed(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with patch("src.ffmpeg_runner.get_streams_info", return_value=None):
            result, job = self._run_transcode(tmp_path, history, input_file)

        assert result is False
        history.mark_failed.assert_called_once()
        history.mark_done.assert_not_called()

    def test_codec_timeout_is_passed(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")

        history = MagicMock()

        with (
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", return_value=(0, "")) as mock_run,
        ):
            self._run_transcode(tmp_path, history, input_file)

        assert mock_run.call_args[1]["timeout"] == config.CODEC_TIMEOUT

    def test_fallback_codec_forced_softdecode_first(self, tmp_path):
        """源编码在 SW_DECODE_FALLBACK_CODECS 内时，首轮即应传
        sw_decode_fallback=True（直接软解）。

        A source codec in SW_DECODE_FALLBACK_CODECS should make the first run
        use sw_decode_fallback=True (software decode from the start).
        """
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")
        history = MagicMock()

        with (
            patch("src.transcoder.get_video_codec", return_value="vc1"),
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch(
                "src.transcoder._build_ffmpeg_cmd", return_value=(["/ffmpeg"], 1.0)
            ) as mock_build,
            patch("src.transcoder._run_ffmpeg_with_progress", side_effect=self._fake_progress),
        ):
            self._run_transcode(tmp_path, history, input_file)

        _, kwargs = mock_build.call_args
        assert kwargs.get("sw_decode_fallback") is True

    def test_hardware_filter_failure_cascades_to_sw_decode(self, tmp_path):
        """硬解 filter 类错误应先用 sw_scale_fallback=True 重试，仍失败后再
        以 sw_decode_fallback=True 级联解码重试。

        A filter reinit error under hardware decode should first retry with
        sw_scale_fallback=True, then cascade to sw_decode_fallback=True if the
        scale retry also fails.
        """
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")
        history = MagicMock()

        with (
            patch("src.transcoder.get_video_codec", return_value="h264"),
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch(
                "src.transcoder._build_ffmpeg_cmd",
                return_value=(["/ffmpeg"], 1.0),
            ) as mock_build,
            patch(
                "src.transcoder._run_ffmpeg_with_progress",
                side_effect=[(1, "Error initializing filters"), (1, "still failing"), (1, "nope")],
            ),
        ):
            self._run_transcode(tmp_path, history, input_file)

        build_flags = [
            c.kwargs.get("sw_scale_fallback") or c.kwargs.get("sw_decode_fallback")
            for c in mock_build.call_args_list
        ]
        assert build_flags == [False, True, True]

    def test_non_filter_error_direct_sw_decode(self, tmp_path):
        """硬解非 filter 类错误应直接以 sw_decode_fallback=True 软解重试，
        不经过缩放重试。

        A non-filter hardware error should retry directly with
        sw_decode_fallback=True, skipping the scale retry.
        """
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")
        history = MagicMock()

        with (
            patch("src.transcoder.get_video_codec", return_value="h264"),
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch(
                "src.transcoder._build_ffmpeg_cmd",
                return_value=(["/ffmpeg"], 1.0),
            ) as mock_build,
            patch(
                "src.transcoder._run_ffmpeg_with_progress",
                side_effect=[(1, "some encode error"), (1, "still failing")],
            ),
        ):
            self._run_transcode(tmp_path, history, input_file)

        build_flags = [
            c.kwargs.get("sw_scale_fallback") or c.kwargs.get("sw_decode_fallback")
            for c in mock_build.call_args_list
        ]
        assert build_flags == [False, True]


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

    def test_skips_active_dirs(self, tmp_path):
        d1 = tmp_path / ".work_active"
        d1.mkdir()
        d2 = tmp_path / ".work_stale"
        d2.mkdir()

        cleanup_stale_temp_dirs(str(tmp_path), ".work_", active_dirs={str(d1)})

        assert d1.exists()
        assert not d2.exists()


class TestTranscodeActiveTempDir:
    """transcode_stage 成功后 temp_dir 必须仍留在活跃集，直到 post_transcode 搬移完成。

    After a successful transcode_stage the temp dir must remain in the active
    set (protected from the stale-cleanup scan pass) until post_transcode moves
    the product out — otherwise cleanup can delete the product while it sits
    queued in the post_pool.
    """

    def _run(self, tmp_path, history, input_file, run_return):
        with _active_temp_dirs_lock:
            _active_temp_dirs.clear()

        def _fake(cmd, total_duration, file_stem, **kwargs):
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("transcoded")
            return run_return

        with (
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", side_effect=_fake),
        ):
            pipeline = TranscodePipeline(history, "/ffmpeg", "/ffprobe", keep_source=True)
            job = pipeline.make_job(
                str(input_file),
                {
                    "input": str(tmp_path),
                    "output": str(tmp_path),
                    "cache": str(tmp_path / "cache"),
                },
            )
            return transcode_stage(pipeline, job), job

    def test_success_keeps_temp_dir_active(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")
        history = MagicMock()

        ok, job = self._run(tmp_path, history, input_file, (0, "transcoded"))
        assert ok is True
        assert str(job.output_path.parent) in active_temp_dirs_snapshot()

    def test_failure_removes_temp_dir_active(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")
        history = MagicMock()

        ok, job = self._run(tmp_path, history, input_file, (1, "encode error"))
        assert ok is False
        cache = str(tmp_path / "cache")
        assert not any(cache in p for p in active_temp_dirs_snapshot())

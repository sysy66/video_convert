"""pipeline / job 模块单元测试：工厂、委托与 Job 值对象。

Unit tests for the pipeline / job modules: factory, delegation and Job value object.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.application.job import TranscodeJob
from src.application.pipeline import TranscodePipeline
from src.application.transcoder import transcode_stage

_VIDEO_AAC = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
    ],
    "duration": 120.0,
}

pytestmark = pytest.mark.unit


@pytest.fixture
def history():
    return MagicMock()


@pytest.fixture
def pipeline(history):
    return TranscodePipeline(
        history=history,
        ffmpeg_path="/ffmpeg",
        ffprobe_path="/ffprobe",
        compute_vmaf=MagicMock(return_value=95.0),
        keep_source=False,
    )


@pytest.fixture
def lib_info(tmp_path):
    base = tmp_path / "lib"
    return {
        "input": str(base / "input"),
        "output": str(base / "output"),
        "cache": str(base / "cache"),
    }


class TestTranscodeJob:
    def test_creation_and_key(self, pipeline, tmp_path):
        fp = tmp_path / "a.mp4"
        fp.write_text("x")
        job = TranscodeJob(
            pipeline=pipeline,
            filepath=Path(fp),
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
        )
        assert job.filepath == Path(fp)
        assert job.key == str(Path(fp))
        assert job.output_path is None
        assert job.dist_frame_count is None
        assert job.transcode_elapsed is None

    def test_key_reflects_filepath(self, pipeline):
        job = TranscodeJob(
            pipeline=pipeline,
            filepath=Path("/a/b.mp4"),
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
        )
        assert job.key == "/a/b.mp4"

    def test_delegated_transcode_calls_pipeline(self, pipeline):
        job = TranscodeJob(
            pipeline=pipeline,
            filepath=Path("/a.mp4"),
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
        )
        pipeline.transcode = MagicMock(return_value=True)
        assert job.transcode() is True
        pipeline.transcode.assert_called_once_with(job)

    def test_delegated_post_process_calls_pipeline(self, pipeline):
        job = TranscodeJob(
            pipeline=pipeline,
            filepath=Path("/a.mp4"),
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
        )
        pipeline.post_process = MagicMock(return_value=True)
        assert job.post_process() is True
        pipeline.post_process.assert_called_once_with(job)

    def test_pipeline_back_reference(self, pipeline):
        job = TranscodeJob(
            pipeline=pipeline,
            filepath=Path("/a.mp4"),
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
        )
        assert job.pipeline is pipeline


class TestTranscodePipelineMakeJob:
    def test_make_job_builds_paths(self, pipeline, lib_info, tmp_path):
        fp = tmp_path / "video.mp4"
        fp.write_text("x")
        job = pipeline.make_job(str(fp), lib_info)
        assert isinstance(job, TranscodeJob)
        assert job.filepath == Path(str(fp))
        assert job.lib_input == Path(lib_info["input"])
        assert job.lib_output == Path(lib_info["output"])
        assert job.lib_cache == Path(lib_info["cache"])
        assert job.pipeline is pipeline

    def test_make_job_accepts_path_object(self, pipeline, lib_info, tmp_path):
        fp = tmp_path / "video2.mp4"
        fp.write_text("x")
        job = pipeline.make_job(Path(fp), lib_info)
        assert job.filepath == Path(fp)

    def test_make_job_key_matches_filepath(self, pipeline, lib_info, tmp_path):
        fp = tmp_path / "video3.mp4"
        fp.write_text("x")
        job = pipeline.make_job(str(fp), lib_info)
        assert job.key == str(Path(str(fp)))


class TestTranscodePipelineTranscode:
    """pipeline.transcode 必须真跑 transcode_stage 并把结果写回 job。

    delegation + write-back are verified against the REAL transcode_stage
    (only its heavy ffmpeg/ffprobe deps are mocked), so the assertions prove
    the real function populates job — not a self-assigned attribute.
    """

    @staticmethod
    def _run_real(pipeline, job, progress):
        def _fake_progress(cmd, total_duration, file_stem, **kwargs):
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("transcoded")
            return progress

        with (
            patch("src.application.pipeline.transcode_stage", side_effect=transcode_stage),
            patch(
                "src.application.transcoder._run_ffmpeg_with_progress", side_effect=_fake_progress
            ),
            patch("src.media.ffmpeg_runner.VideoProbe.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.media.probe.VideoProbe.get_codec", return_value="h264"),
        ):
            return pipeline.transcode(job)

    def _make_job(self, pipeline, tmp_path, name):
        fp = tmp_path / name
        fp.write_text("fake video")
        return TranscodeJob(
            pipeline=pipeline,
            filepath=fp,
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=tmp_path / "cache",
        )

    def test_transcode_delegates_and_writes_back_on_success(self, pipeline, tmp_path):
        job = self._make_job(pipeline, tmp_path, "a.mp4")
        ok = self._run_real(pipeline, job, (0, "frame=  5000 fps=150.0\nframe=  5000 fps=149.2\n"))
        assert ok is True
        # 真实 transcode_stage 写回：帧数来自 ffmpeg 进度行，耗时非空，产物路径已定
        assert job.dist_frame_count == 5000
        assert job.transcode_elapsed is not None
        assert job.output_path is not None
        assert Path(job.output_path).name == "a.mkv"

    def test_transcode_failure_returns_false_and_does_not_write_back(self, pipeline, tmp_path):
        job = self._make_job(pipeline, tmp_path, "b.mp4")
        ok = self._run_real(pipeline, job, (1, "encode error"))
        assert ok is False
        assert job.dist_frame_count is None
        assert job.transcode_elapsed is None

    def test_transcode_forwards_keep_source_and_compute_vmaf(self, tmp_path, history):
        sentinel = MagicMock(return_value=88.0)
        pl = TranscodePipeline(
            history=history,
            ffmpeg_path="/ffmpeg",
            ffprobe_path="/ffprobe",
            compute_vmaf=sentinel,
            keep_source=True,
        )
        job = self._make_job(pl, tmp_path, "c.mp4")
        ok = self._run_real(pl, job, (0, "frame=  100 fps=150.0\nframe=  100 fps=149.2\n"))
        assert ok is True
        # keep_source / compute_vmaf 承载在 pipeline 上，transcode_stage 经 pipeline 读取
        assert pl.keep_source is True
        assert pl.compute_vmaf is sentinel
        assert job.dist_frame_count == 100

    def test_job_transcode_shortcut(self, pipeline, tmp_path):
        fp = tmp_path / "d.mp4"
        fp.write_text("fake video")
        job = pipeline.make_job(
            str(fp), {"input": "/in", "output": "/out", "cache": str(tmp_path / "cache")}
        )
        ok = self._run_real(pipeline, job, (0, "frame=  123 fps=150.0\n"))
        assert ok is True
        assert job.dist_frame_count == 123


class TestTranscodePipelinePostProcess:
    def test_delegates_to_post_process_stage(self, pipeline, tmp_path):
        fp = tmp_path / "a.mp4"
        fp.write_text("x")
        job = TranscodeJob(
            pipeline=pipeline,
            filepath=fp,
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
            dist_frame_count=5000,
            transcode_elapsed=2.5,
        )
        with patch("src.application.pipeline.post_process_stage", return_value=True) as mock_stage:
            result = pipeline.post_process(job)
            mock_stage.assert_called_once_with(pipeline, job)
            assert result is True

    def test_post_process_passes_none_when_not_transcoded(self, pipeline, tmp_path):
        fp = tmp_path / "b.mp4"
        fp.write_text("x")
        job = TranscodeJob(
            pipeline=pipeline,
            filepath=fp,
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
        )
        with patch("src.application.pipeline.post_process_stage", return_value=False) as mock_stage:
            result = pipeline.post_process(job)
            mock_stage.assert_called_once_with(pipeline, job)
            assert job.dist_frame_count is None
            assert job.transcode_elapsed is None
            assert result is False

    def test_job_post_process_shortcut(self, pipeline, tmp_path):
        fp = tmp_path / "c.mp4"
        fp.write_text("x")
        job = pipeline.make_job(str(fp), {"input": "/in", "output": "/out", "cache": "/cache"})
        job.dist_frame_count = 100
        job.transcode_elapsed = 1.0
        with patch("src.application.pipeline.post_process_stage", return_value=True) as mock_stage:
            assert job.post_process() is True
            mock_stage.assert_called_once_with(pipeline, job)

    def test_post_process_forwards_keep_source(self, tmp_path, history):
        pl = TranscodePipeline(
            history=history,
            ffmpeg_path="/ffmpeg",
            ffprobe_path="/ffprobe",
            keep_source=True,
        )
        job = TranscodeJob(
            pipeline=pl,
            filepath=tmp_path / "x.mp4",
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
            dist_frame_count=10,
            transcode_elapsed=0.2,
        )
        with patch("src.application.pipeline.post_process_stage", return_value=True) as mock_stage:
            pl.post_process(job)
            passed_pipeline, passed_job = mock_stage.call_args[0]
            assert passed_pipeline is pl
            assert passed_pipeline.keep_source is True
            assert passed_job is job


class TestCircularImport:
    def test_import_job_does_not_trigger_circular_import(self):
        # job -> pipeline cycle must be broken via TYPE_CHECKING / string annotation
        import importlib

        import src.application.job as job_mod
        import src.application.pipeline as pipe_mod

        importlib.reload(job_mod)
        importlib.reload(pipe_mod)
        # re-import after reload should still work
        from src.application.job import TranscodeJob as TJ2
        from src.application.pipeline import TranscodePipeline as TP2

        h = MagicMock()
        pl = TP2(history=h, ffmpeg_path="/ffmpeg", ffprobe_path="/ffprobe")
        j = TJ2(
            pipeline=pl,
            filepath=Path("/a.mp4"),
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
        )
        assert j.pipeline is pl

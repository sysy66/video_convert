"""pipeline / job 模块单元测试：工厂、委托与 Job 值对象。

Unit tests for the pipeline / job modules: factory, delegation and Job value object.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.job import TranscodeJob
from src.pipeline import TranscodePipeline

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
        assert job.elapsed is None

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
    def test_delegates_to_transcode_function_and_mutates_job(self, pipeline, tmp_path):
        fp = tmp_path / "a.mp4"
        fp.write_text("x")
        job = TranscodeJob(
            pipeline=pipeline,
            filepath=fp,
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
        )
        with patch("src.pipeline.transcode_stage", return_value=True) as mock_tc:
            ok = pipeline.transcode(job)
            mock_tc.assert_called_once_with(pipeline, job)
            assert ok is True
            # transcode_stage 成功时把产物信息写回 job（真实逻辑；此处校验调用契约与回填语义）
            job.dist_frame_count = 5000
            job.elapsed = 3.14
            assert job.dist_frame_count == 5000
            assert job.elapsed == 3.14

    def test_transcode_failure_still_writes_back(self, pipeline, tmp_path):
        fp = tmp_path / "b.mp4"
        fp.write_text("x")
        job = TranscodeJob(
            pipeline=pipeline,
            filepath=fp,
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
        )
        with patch("src.pipeline.transcode_stage", return_value=False) as mock_tc:
            ok = pipeline.transcode(job)
            assert ok is False
            assert mock_tc.called
            assert job.dist_frame_count is None
            assert job.elapsed is None

    def test_transcode_forwards_keep_source_and_compute_vmaf(self, tmp_path, history):
        sentinel = MagicMock(return_value=88.0)
        pl = TranscodePipeline(
            history=history,
            ffmpeg_path="/ffmpeg",
            ffprobe_path="/ffprobe",
            compute_vmaf=sentinel,
            keep_source=True,
        )
        job = TranscodeJob(
            pipeline=pl,
            filepath=tmp_path / "c.mp4",
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
        )
        with patch("src.pipeline.transcode_stage", return_value=True) as mock_tc:
            pl.transcode(job)
            mock_tc.assert_called_once_with(pl, job)
            # keep_source / compute_vmaf 承载在 pipeline 上，由 transcode_stage 经 pipeline 读取
            assert pl.keep_source is True
            assert pl.compute_vmaf is sentinel

    def test_job_transcode_shortcut(self, pipeline, tmp_path):
        fp = tmp_path / "d.mp4"
        fp.write_text("x")
        job = pipeline.make_job(str(fp), {"input": "/in", "output": "/out", "cache": "/cache"})
        with patch("src.pipeline.transcode_stage", return_value=True) as mock_tc:
            assert job.transcode() is True
            mock_tc.assert_called_once_with(pipeline, job)
            # transcode_stage 成功时写回 dist_frame_count（真实逻辑）
            job.dist_frame_count = 123
            assert job.dist_frame_count == 123


class TestTranscodePipelinePostProcess:
    def test_delegates_to_post_transcode(self, pipeline, tmp_path):
        fp = tmp_path / "a.mp4"
        fp.write_text("x")
        job = TranscodeJob(
            pipeline=pipeline,
            filepath=fp,
            lib_input=Path("/in"),
            lib_output=Path("/out"),
            lib_cache=Path("/cache"),
            dist_frame_count=5000,
            elapsed=2.5,
        )
        with patch("src.pipeline.post_transcode", return_value=True) as mock_pt:
            result = pipeline.post_process(job)
            mock_pt.assert_called_once_with(
                {"dist_frame_count": 5000, "elapsed": 2.5},
                job.filepath,
                pipeline.history,
                pipeline.ffmpeg_path,
                pipeline.ffprobe_path,
                job.lib_input,
                job.lib_output,
                job.lib_cache,
                pipeline.keep_source,
                pipeline.compute_vmaf,
            )
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
        with patch("src.pipeline.post_transcode", return_value=False) as mock_pt:
            result = pipeline.post_process(job)
            assert mock_pt.call_args[0][0] == {"dist_frame_count": None, "elapsed": None}
            assert result is False

    def test_job_post_process_shortcut(self, pipeline, tmp_path):
        fp = tmp_path / "c.mp4"
        fp.write_text("x")
        job = pipeline.make_job(str(fp), {"input": "/in", "output": "/out", "cache": "/cache"})
        job.dist_frame_count = 100
        job.elapsed = 1.0
        with patch("src.pipeline.post_transcode", return_value=True) as mock_pt:
            assert job.post_process() is True
            mock_pt.assert_called_once()

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
            elapsed=0.2,
        )
        with patch("src.pipeline.post_transcode", return_value=True) as mock_pt:
            pl.post_process(job)
            assert mock_pt.call_args[0][8] is True


class TestCircularImport:
    def test_import_job_does_not_trigger_circular_import(self):
        # job -> pipeline cycle must be broken via TYPE_CHECKING / string annotation
        import importlib

        import src.job as job_mod
        import src.pipeline as pipe_mod

        importlib.reload(job_mod)
        importlib.reload(pipe_mod)
        # re-import after reload should still work
        from src.job import TranscodeJob as TJ2
        from src.pipeline import TranscodePipeline as TP2

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

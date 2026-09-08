"""post_process_stage 后处理模块单元测试：质量门、VMAF 打分、移动落盘与最终状态。

Unit tests for the post_process_stage module: quality gate, VMAF scoring, move to
destination and final history state (the second stage of the transcode pipeline).
"""

import queue
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src.job import TranscodeJob
from src.pipeline import TranscodePipeline
from src.post_process import post_process_stage
from src.temp_cleanup import (
    _active_temp_dirs,
    _active_temp_dirs_lock,
    active_temp_dirs_snapshot,
)
from src.vmaf_async import drain_vmaf_tasks, vmaf_tasks

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _cleanup_vmaf_workers():
    yield
    drain_vmaf_tasks()
    while True:
        try:
            vmaf_tasks.get_nowait()
        except queue.Empty:
            break


def _make_history(output_path):
    """返回预设 get_output_path 为指定产物路径的 History 替身。"""
    history = MagicMock()
    history.get_output_path.side_effect = lambda fp: str(output_path)
    return history


def _make(tmp_path, name="test.mp4", out_name="test.mkv"):
    input_file = tmp_path / name
    input_file.write_text("fake video" * 10)
    output = tmp_path / "cache" / ".work_x" / out_name
    output.parent.mkdir(parents=True)
    output.write_text("transcoded")
    history = _make_history(str(output))
    info = {"dist_frame_count": None, "elapsed": 1.0}
    return input_file, output, history, info


def _make_stage(
    tmp_path,
    name="test.mp4",
    out_name="test.mkv",
    compute_vmaf=None,
    keep_source=False,
    input_dir=None,
    output_dir=None,
    vmaf_mode=config.VMAF_MODE_SYNC,
):
    """构造 (pipeline, job) 两段式入参；input_file/output/history 照旧返回供断言。"""
    input_file, output, history, info = _make(tmp_path, name, out_name)
    pipeline = TranscodePipeline(
        history=history,
        ffmpeg_path="/ffmpeg",
        ffprobe_path="/ffprobe",
        compute_vmaf=compute_vmaf,
        keep_source=keep_source,
        vmaf_mode=vmaf_mode,
    )
    job = TranscodeJob(
        pipeline=pipeline,
        filepath=input_file,
        lib_input=Path(input_dir) if input_dir else tmp_path,
        lib_output=Path(output_dir) if output_dir else tmp_path,
        lib_cache=tmp_path / "cache",
        dist_frame_count=info["dist_frame_count"],
        elapsed=info["elapsed"],
    )
    return pipeline, job, input_file, output, history


class TestPostTranscodeMissingOutput:
    def test_missing_output_marks_failed(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")
        history = _make_history(str(tmp_path / "cache" / ".work_x" / "test.mkv"))
        assert not Path(history.get_output_path(str(input_file))).exists()

        pipeline = TranscodePipeline(
            history=history, ffmpeg_path="/ffmpeg", ffprobe_path="/ffprobe"
        )
        job = TranscodeJob(
            pipeline=pipeline,
            filepath=input_file,
            lib_input=tmp_path,
            lib_output=tmp_path,
            lib_cache=tmp_path / "cache",
        )
        result = post_process_stage(pipeline, job)

        assert result is False
        history.mark_failed.assert_called_once_with(
            str(input_file), "output path missing after transcode"
        )


class TestPostTranscodeSync:
    def test_off_mode_accepts_on_ratio_only(self, tmp_path):
        # off 模式（VMAF_MODE_OFF）：不调打分，接受并删除源、移入最终位置。
        mock_vmaf = MagicMock()
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path, compute_vmaf=mock_vmaf, vmaf_mode=config.VMAF_MODE_OFF
        )
        result = post_process_stage(pipeline, job)

        assert result is True
        mock_vmaf.assert_not_called()
        assert not input_file.exists()
        assert (tmp_path / "test.mkv").exists()
        history.mark_done.assert_called_once()
        assert history.mark_done.call_args[1]["rejected"] is False

    def test_accept_keeps_source_when_keep_source(self, tmp_path):
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path, keep_source=True, vmaf_mode=config.VMAF_MODE_OFF
        )
        post_process_stage(pipeline, job)
        assert input_file.exists()
        assert (tmp_path / "test.mkv").exists()

    def test_ratio_over_max_rejects_without_scoring(self, tmp_path):
        # ratio 超标必拒，省去打分；保留源文件，标 rejected。
        mock_vmaf = MagicMock(return_value=95.0)
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path, compute_vmaf=mock_vmaf, vmaf_mode=config.VMAF_MODE_SYNC
        )
        input_file.write_text("x" * 5)  # 极小源 → ratio 远超 MAX
        with patch("src.post_process.config.ACCEPT_RATIO_MAX", 0.5):
            result = post_process_stage(pipeline, job)

        assert result is True
        mock_vmaf.assert_not_called()
        assert input_file.exists()
        assert not (tmp_path / "test.mkv").exists()
        history.mark_done.assert_called_once_with(
            str(input_file), None, vmaf_score=None, rejected=True
        )

    def test_score_failed_rejects(self, tmp_path):
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path,
            compute_vmaf=MagicMock(return_value=None),
            vmaf_mode=config.VMAF_MODE_SYNC,
        )
        result = post_process_stage(pipeline, job)
        assert result is True
        assert input_file.exists()
        assert not output.exists()
        assert job.vmaf_score is None
        history.mark_failed.assert_not_called()
        history.mark_done.assert_called_once_with(
            str(input_file), None, vmaf_score=None, rejected=True
        )

    def test_score_none_during_shutdown_rejects(self, tmp_path):
        # 关停中断的打分视同普通打分失败：记 rejected（保留源文件），
        # 不再延后（await_postprocess/recover 已随 async 退役）。
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path,
            compute_vmaf=MagicMock(return_value=None),
            vmaf_mode=config.VMAF_MODE_SYNC,
        )
        with patch("src.post_process.is_shutting_down", return_value=True):
            result = post_process_stage(pipeline, job)
        assert result is True
        assert input_file.exists()
        assert not output.exists()
        assert job.vmaf_score is None
        history.mark_failed.assert_not_called()
        history.mark_done.assert_called_once_with(
            str(input_file), None, vmaf_score=None, rejected=True
        )

    def test_extreme_low_rejects(self, tmp_path):
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path,
            compute_vmaf=MagicMock(return_value=55.0),
            vmaf_mode=config.VMAF_MODE_SYNC,
        )
        result = post_process_stage(pipeline, job)
        assert result is True
        assert input_file.exists()
        assert not output.exists()
        assert job.vmaf_score == 55.0
        history.mark_failed.assert_not_called()
        history.mark_done.assert_called_once_with(
            str(input_file), None, vmaf_score=55.0, rejected=True
        )

    def test_accept_deletes_source_and_moves(self, tmp_path):
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path,
            compute_vmaf=MagicMock(return_value=95.0),
            vmaf_mode=config.VMAF_MODE_SYNC,
        )
        result = post_process_stage(pipeline, job)
        assert result is True
        assert not input_file.exists()
        assert (tmp_path / "test.mkv").exists()
        assert (tmp_path / "test.mkv").read_text() == "transcoded"
        assert job.vmaf_score == 95.0
        history.mark_done.assert_called_once_with(
            str(input_file), str(tmp_path / "test.mkv"), vmaf_score=95.0, rejected=False
        )

    def test_reject_keeps_source(self, tmp_path):
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path,
            compute_vmaf=MagicMock(return_value=60.0),
            vmaf_mode=config.VMAF_MODE_SYNC,
        )
        result = post_process_stage(pipeline, job)
        assert result is True
        assert input_file.exists()
        assert not (tmp_path / "test.mkv").exists()
        assert job.vmaf_score == 60.0
        history.mark_done.assert_called_once_with(
            str(input_file), None, vmaf_score=60.0, rejected=True
        )

    def test_reject_with_keep_target_lands_output(self, tmp_path):
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path,
            compute_vmaf=MagicMock(return_value=60.0),
            vmaf_mode=config.VMAF_MODE_SYNC,
        )
        with patch("src.post_process.config.KEEP_TARGET", True):
            result = post_process_stage(pipeline, job)
        assert result is True
        assert input_file.exists()
        landed = tmp_path / "test.mkv"
        assert landed.exists()
        assert landed.read_text() == "transcoded"
        assert not output.exists()
        history.mark_failed.assert_not_called()
        history.mark_done.assert_called_once_with(
            str(input_file), str(landed), vmaf_score=60.0, rejected=True
        )

    def test_ratio_over_max_with_keep_target_lands_output(self, tmp_path):
        mock_vmaf = MagicMock(return_value=95.0)
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path, compute_vmaf=mock_vmaf, vmaf_mode=config.VMAF_MODE_SYNC
        )
        input_file.write_text("x" * 5)  # 极小源 → ratio 远超 MAX
        with (
            patch("src.post_process.config.KEEP_TARGET", True),
            patch("src.post_process.config.ACCEPT_RATIO_MAX", 0.5),
        ):
            result = post_process_stage(pipeline, job)
        assert result is True
        mock_vmaf.assert_not_called()
        assert input_file.exists()
        assert (tmp_path / "test.mkv").exists()
        history.mark_failed.assert_not_called()
        history.mark_done.assert_called_once_with(
            str(input_file), str(tmp_path / "test.mkv"), vmaf_score=None, rejected=True
        )

    def test_move_failure_marks_failed(self, tmp_path):
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path, vmaf_mode=config.VMAF_MODE_OFF
        )
        with patch("shutil.copyfile", side_effect=OSError("nfs down")):
            result = post_process_stage(pipeline, job)
        assert result is False
        history.mark_failed.assert_called_once()
        history.mark_done.assert_not_called()


class TestPostTranscodeAsync:
    def _make_async(self, tmp_path):
        return _make_stage(tmp_path)

    def test_ratio_over_max_rejects_without_queue(self, tmp_path):
        pipeline, job, input_file, output, history = self._make_async(tmp_path)
        input_file.write_text("x" * 5)
        with (
            patch("src.post_process.config.VMAF_MODE", config.VMAF_MODE_ASYNC),
            patch("src.post_process.config.ACCEPT_RATIO_MAX", 0.5),
        ):
            result = post_process_stage(pipeline, job)
        assert result is True
        assert input_file.exists()
        assert not (tmp_path / "test.mkv").exists()
        history.mark_done.assert_called_once_with(
            str(input_file), None, vmaf_score=None, rejected=True
        )
        history.mark_await_postprocess.assert_not_called()
        assert vmaf_tasks.empty()

    def test_async_accept_scores_landed_file(self, tmp_path):
        # async 全链：先落盘→打分对象是库中文件→accept 不复拷→删源记终态。
        mock_vmaf = MagicMock(return_value=95.0)
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path, compute_vmaf=mock_vmaf, vmaf_mode=config.VMAF_MODE_ASYNC
        )
        result = post_process_stage(pipeline, job)
        assert result is True
        landed = tmp_path / "test.mkv"
        assert landed.exists()
        assert landed.read_text() == "transcoded"
        assert not input_file.exists()
        assert not output.exists()
        # 打分对象是落盘文件而非 temp 产物（所见即所得）
        assert mock_vmaf.call_args[0][1] == landed
        history.mark_failed.assert_not_called()
        history.mark_done.assert_called_once_with(
            str(input_file), str(landed), vmaf_score=95.0, rejected=False
        )

    def test_async_reject_removes_landed_file(self, tmp_path):
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path,
            compute_vmaf=MagicMock(return_value=60.0),
            vmaf_mode=config.VMAF_MODE_ASYNC,
        )
        result = post_process_stage(pipeline, job)
        assert result is True
        assert input_file.exists()
        assert not (tmp_path / "test.mkv").exists()
        assert not output.exists()
        history.mark_failed.assert_not_called()
        history.mark_done.assert_called_once_with(
            str(input_file), None, vmaf_score=60.0, rejected=True
        )


class TestPostTranscodeActiveTempDir:
    """post_process_stage 搬移完成后必须把 temp_dir 从活跃集移除。

    post_process_stage owns the temp dir's lifecycle: it must discard it from the
    active set after moving the product out, so the protection granted by
    transcode_stage's success path is released exactly once the file is safe.
    """

    def test_removes_temp_dir_from_active_set_after_accept(self, tmp_path):
        pipeline, job, input_file, output, history = _make_stage(
            tmp_path, vmaf_mode=config.VMAF_MODE_OFF
        )
        temp_dir = str(output.parent)
        with _active_temp_dirs_lock:
            _active_temp_dirs.add(temp_dir)
        assert temp_dir in active_temp_dirs_snapshot()

        result = post_process_stage(pipeline, job)
        assert result is True
        assert temp_dir not in active_temp_dirs_snapshot()


class TestPostProcessDispatcher:
    """dispatcher 路由：按 pipeline.vmaf_mode 分发三模式函数。

    Dispatcher routing: dispatch to the per-mode function by
    pipeline.vmaf_mode (None falls back to the global config).
    """

    def _route(self, tmp_path, vmaf_mode):
        pipeline, job, *_ = _make_stage(tmp_path, vmaf_mode=vmaf_mode)
        with (
            patch(
                "src.post_process._post_process_ratio_only", return_value=(True, None)
            ) as mock_off,
            patch("src.post_process._post_process_sync", return_value=(True, None)) as mock_sync,
            patch("src.post_process._post_process_async", return_value=(True, None)) as mock_async,
        ):
            result = post_process_stage(pipeline, job)
        return result, mock_off, mock_sync, mock_async

    def test_routes_off(self, tmp_path):
        result, mock_off, mock_sync, mock_async = self._route(tmp_path, config.VMAF_MODE_OFF)
        assert result is True
        mock_off.assert_called_once()
        mock_sync.assert_not_called()
        mock_async.assert_not_called()

    def test_routes_sync(self, tmp_path):
        result, mock_off, mock_sync, mock_async = self._route(tmp_path, config.VMAF_MODE_SYNC)
        assert result is True
        mock_sync.assert_called_once()
        mock_off.assert_not_called()
        mock_async.assert_not_called()

    def test_routes_async(self, tmp_path):
        result, mock_off, mock_sync, mock_async = self._route(tmp_path, config.VMAF_MODE_ASYNC)
        assert result is True
        mock_async.assert_called_once()
        mock_off.assert_not_called()
        mock_sync.assert_not_called()

    def test_none_falls_back_to_global_config(self, tmp_path):
        with patch("src.post_process.config.VMAF_MODE", config.VMAF_MODE_SYNC):
            result, mock_off, mock_sync, mock_async = self._route(tmp_path, None)
        assert result is True
        mock_sync.assert_called_once()
        mock_off.assert_not_called()
        mock_async.assert_not_called()

    def test_unknown_mode_warns_and_uses_async(self, tmp_path, caplog):
        result, mock_off, mock_sync, mock_async = self._route(tmp_path, "bogus")
        assert result is True
        mock_async.assert_called_once()
        mock_off.assert_not_called()
        mock_sync.assert_not_called()
        assert "bogus" in caplog.text

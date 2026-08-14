"""src.transcoder 异步 VMAF 管线单元测试：任务入队、worker 启停、任务处理、
重启恢复与 pending_vmaf 过滤。

Unit tests for the async VMAF pipeline in src.transcoder: task queueing,
worker start/drain, task handling, restart recovery and pending_vmaf filtering.
"""

import queue
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src.filter import should_process
from src.transcoder import (
    _handle_vmaf_task,
    drain_vmaf_tasks,
    recover_pending_vmaf,
    start_vmaf_workers,
    transcode,
    vmaf_tasks,
)

pytestmark = pytest.mark.unit

_VIDEO_AAC = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
    ],
    "duration": 120.0,
}


@pytest.fixture(autouse=True)
def _cleanup_vmaf_workers():
    yield
    drain_vmaf_tasks()
    while True:
        try:
            vmaf_tasks.get_nowait()
        except queue.Empty:
            break


class TestTranscodeAsync:
    def _fake_progress(self, cmd, total_duration, file_stem, **kwargs):
        output_path = Path(cmd[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("transcoded")
        return (0, "")

    def _base_patches(self, compute_vmaf_return=95.0):
        return (
            patch("src.ffmpeg_runner.get_streams_info", return_value=_VIDEO_AAC),
            patch("src.transcoder._run_ffmpeg_with_progress", side_effect=self._fake_progress),
            patch("src.transcoder.config.VMAF_ASYNC", True),
            patch("src.transcoder.compute_vmaf", return_value=compute_vmaf_return),
        )

    def test_keeps_source_moves_output_and_queues(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video" * 10)

        history = MagicMock()

        p1, p2, p3, p4 = self._base_patches()
        with p1, p2, p3, p4 as mock_vmaf:
            result = transcode(str(input_file), history, "/ffmpeg", "/ffprobe")
            mock_vmaf.assert_not_called()  # VMAF_ASYNC 下不执行内联打分

        assert result is True
        assert input_file.exists()
        assert (tmp_path / "test.mkv").exists()
        history.mark_pending_vmaf.assert_called_once()
        history.mark_done.assert_not_called()
        work_dirs = [d for d in tmp_path.iterdir() if d.name.startswith(".work_")]
        assert len(work_dirs) == 0

        task = vmaf_tasks.get_nowait()
        assert task[0] == str(input_file)
        assert task[1] == str(tmp_path / "test.mkv")
        assert task[2] == pytest.approx(0.1)
        assert task[3] == "test"
        assert task[4] is history
        assert task[8] == "/ffprobe"  # worker 内源帧数快路径探测的 ffprobe 路径

    def test_dist_frame_count_parsed_from_stderr_and_queued(self, tmp_path):
        # 转码进度 stderr 末尾 frame=N 即产物帧数，随任务入队，
        # worker 打分直接使用，不再对 k9 产物做全量解码计数
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video" * 10)

        history = MagicMock()

        def _progress_with_frames(cmd, total_duration, file_stem, **kwargs):
            output_path = Path(cmd[-1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("transcoded")
            return (0, "frame=  5000 fps=150.0\nframe=  5000 fps=149.2 size=2048KiB\n")

        p1, p2, p3, p4 = self._base_patches()
        with (
            p1,
            p2,
            p3,
            p4,
            patch("src.transcoder._run_ffmpeg_with_progress", side_effect=_progress_with_frames),
        ):
            result = transcode(str(input_file), history, "/ffmpeg", "/ffprobe")

        assert result is True
        task = vmaf_tasks.get_nowait()
        assert task[7] == 5000  # dist_frame_count 移至新 tuple 索引

    def test_ratio_over_max_rejects_without_queue(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("x" * 5)

        history = MagicMock()

        p1, p2, p3, p4 = self._base_patches()
        with (p1, p2, p3, p4 as mock_vmaf, patch("src.transcoder.config.VMAF_REQUIRED", True)):
            result = transcode(str(input_file), history, "/ffmpeg", "/ffprobe")
            mock_vmaf.assert_not_called()  # ratio 超标必拒，不打分

        assert result is True
        assert input_file.exists()
        assert not (tmp_path / "test.mkv").exists()
        history.mark_done.assert_called_once_with(
            str(input_file), None, vmaf_score=None, rejected=True
        )
        history.mark_pending_vmaf.assert_not_called()
        assert vmaf_tasks.empty()
        work_dirs = [d for d in tmp_path.iterdir() if d.name.startswith(".work_")]
        assert len(work_dirs) == 0

    def test_output_moved_into_output_dir_preserving_subdir(self, tmp_path):
        input_file = tmp_path / "input" / "sub" / "test.mp4"
        input_file.parent.mkdir(parents=True)
        input_file.write_text("fake video" * 10)
        output_dir = tmp_path / "output"

        history = MagicMock()

        p1, p2, p3, p4 = self._base_patches()
        with p1, p2, p3, p4:
            result = transcode(
                str(input_file), history, "/ffmpeg", "/ffprobe", tmp_path / "input", output_dir
            )

        assert result is True
        assert (output_dir / "sub" / "test.mkv").exists()
        assert input_file.exists()
        history.mark_pending_vmaf.assert_called_once_with(
            str(input_file), str(output_dir / "sub" / "test.mkv"), None
        )

    def test_move_failure_marks_failed(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video" * 10)

        history = MagicMock()

        p1, p2, p3, p4 = self._base_patches()
        with (
            p1,
            p2,
            p3,
            p4,
            patch("shutil.copyfile", side_effect=OSError("nfs down")),
        ):
            result = transcode(str(input_file), history, "/ffmpeg", "/ffprobe")

        assert result is False
        history.mark_failed.assert_called_once()
        history.mark_pending_vmaf.assert_not_called()


class TestHandleVmafTask:
    def _setup_files(self, tmp_path):
        source = tmp_path / "src" / "movie.mkv"
        source.parent.mkdir()
        source.write_text("source content")
        output = tmp_path / "out" / "movie.mkv"
        output.parent.mkdir()
        output.write_text("output content")
        history = MagicMock()
        task = (str(source), str(output), 0.9, "movie", history)
        return source, output, history, task

    def test_accept_deletes_source_keeps_output(self, tmp_path):
        source, output, history, task = self._setup_files(tmp_path)
        with patch("src.vmaf_async.compute_vmaf", return_value=95.0):
            _handle_vmaf_task(task)
        assert not source.exists()
        assert output.exists()
        history.mark_done.assert_called_once_with(
            str(source), str(output), vmaf_score=95.0, rejected=False
        )

    def test_six_tuple_task_with_codec_elapsed(self, tmp_path):
        source, output, history, task = self._setup_files(tmp_path)
        with patch("src.vmaf_async.compute_vmaf", return_value=95.0):
            _handle_vmaf_task(task + (123.4,))
        assert not source.exists()
        assert output.exists()
        history.mark_done.assert_called_once_with(
            str(source), str(output), vmaf_score=95.0, rejected=False
        )

    def test_reject_deletes_output_keeps_source(self, tmp_path):
        source, output, history, task = self._setup_files(tmp_path)
        with patch("src.vmaf_async.compute_vmaf", return_value=60.0):
            _handle_vmaf_task(task)
        assert source.exists()
        assert not output.exists()
        history.mark_done.assert_called_once_with(str(source), None, vmaf_score=60.0, rejected=True)

    def test_extreme_low_score_marks_failed_for_retry(self, tmp_path):
        source, output, history, task = self._setup_files(tmp_path)
        with patch("src.vmaf_async.compute_vmaf", return_value=45.0):
            _handle_vmaf_task(task)
        assert source.exists()
        assert not output.exists()
        history.mark_done.assert_not_called()
        history.mark_failed.assert_called_once_with(
            str(source), "VMAF 45.00 below 60, input may be incomplete"
        )

    def test_score_none_marks_failed_for_retry(self, tmp_path):
        # VMAF scoring failed (e.g. interrupted by shutdown / decode error) → retry,
        # NOT a permanent rejection which would block the file forever.
        # VMAF_REQUIRED=True forces the reject branch so the score-None path runs.
        source, output, history, task = self._setup_files(tmp_path)
        with (
            patch("src.vmaf_async.compute_vmaf", return_value=None),
            patch("src.transcoder.config.VMAF_REQUIRED", True),
        ):
            _handle_vmaf_task(task)
        assert source.exists()
        assert not output.exists()
        history.mark_done.assert_not_called()
        history.mark_failed.assert_called_once_with(str(source), "VMAF scoring failed")

    def test_accept_missing_output_marks_failed(self, tmp_path):
        source, output, history, task = self._setup_files(tmp_path)
        output.unlink()
        with patch("src.vmaf_async.compute_vmaf", return_value=95.0):
            _handle_vmaf_task(task)
        assert source.exists()
        history.mark_failed.assert_called_once_with(
            str(source), "VMAF accept failed: output missing"
        )
        history.mark_done.assert_not_called()

    def test_accept_source_unlink_failure_marks_failed(self, tmp_path):
        source, output, history, task = self._setup_files(tmp_path)
        with (
            patch("src.vmaf_async.compute_vmaf", return_value=95.0),
            patch("src.vmaf_async.Path.unlink", side_effect=OSError("nfs down")),
        ):
            _handle_vmaf_task(task)
        assert source.exists()
        assert output.exists()
        history.mark_failed.assert_called_once()
        history.mark_done.assert_not_called()

    def test_reject_output_unlink_failure_marks_failed(self, tmp_path):
        source, output, history, task = self._setup_files(tmp_path)
        with (
            patch("src.vmaf_async.compute_vmaf", return_value=60.0),
            patch("src.vmaf_async.Path.unlink", side_effect=OSError("nfs down")),
        ):
            _handle_vmaf_task(task)
        assert source.exists()
        assert output.exists()
        history.mark_failed.assert_called_once()
        history.mark_done.assert_not_called()

    def test_writes_vmaf_log_with_safe_stem(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "VMAF_LOG_DIR", str(tmp_path / "vmaf_logs"))
        source = tmp_path / "My Video! (2024).mkv"
        source.write_text("src")
        output = tmp_path / "out.mkv"
        output.write_text("out")
        history = MagicMock()
        task = (str(source), str(output), 0.9, "My_Video__2024_", history)
        with patch("src.vmaf_async.compute_vmaf", return_value=95.0) as mock_vmaf:
            _handle_vmaf_task(task)
        log_path = mock_vmaf.call_args[1]["log_path"]
        assert log_path.parent == tmp_path / "vmaf_logs"
        assert log_path.name.startswith("My_Video__2024_.")
        assert log_path.name.endswith(".json")


class TestVmafWorkers:
    def test_start_is_idempotent(self):
        with patch("src.transcoder.config.VMAF_WORKERS", 2):
            start_vmaf_workers()
            start_vmaf_workers()
        from src.transcoder import _vmaf_worker_threads

        assert len(_vmaf_worker_threads) == 2

    def test_drain_processes_queued_tasks(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_text("source content")
        output = tmp_path / "out.mkv"
        output.write_text("output content")
        history = MagicMock()

        with patch("src.vmaf_async.compute_vmaf", return_value=95.0):
            vmaf_tasks.put((str(source), str(output), 0.9, "movie", history))
            start_vmaf_workers()
            drain_vmaf_tasks()

        assert not source.exists()
        assert output.exists()
        history.mark_done.assert_called_once()

    def test_drain_without_start_is_noop(self):
        drain_vmaf_tasks()


class TestRecoverPendingVmaf:
    def test_requeues_when_both_files_exist(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_text("s" * 100)
        output = tmp_path / "out" / "movie.mkv"
        output.parent.mkdir()
        output.write_text("o" * 50)

        history = MagicMock()
        history.iter_pending_vmaf.return_value = [(str(source), str(output), 777)]

        recover_pending_vmaf(history)

        task = vmaf_tasks.get_nowait()
        assert task[0] == str(source)
        assert task[1] == str(output)
        assert task[2] == pytest.approx(0.5)
        assert task[3] == "movie"
        assert task[4] is history
        assert task[7] == 777  # 持久化的产物帧数随任务恢复，启用帧序对齐
        history.mark_failed.assert_not_called()
        history.mark_done.assert_not_called()

    def test_missing_output_marks_failed(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_text("src")

        history = MagicMock()
        history.iter_pending_vmaf.return_value = [
            (str(source), str(tmp_path / "gone" / "o.mkv"), None)
        ]

        recover_pending_vmaf(history)

        history.mark_failed.assert_called_once_with(
            str(source), "Pending VMAF output missing on recovery"
        )
        assert vmaf_tasks.empty()

    def test_missing_source_removes_orphan_output(self, tmp_path):
        output = tmp_path / "orphan.mkv"
        output.write_text("orphan")

        history = MagicMock()
        history.iter_pending_vmaf.return_value = [(str(tmp_path / "gone.mkv"), str(output), None)]

        recover_pending_vmaf(history)

        assert not output.exists()
        history.mark_done.assert_called_once_with(str(tmp_path / "gone.mkv"), None, rejected=True)
        assert vmaf_tasks.empty()


class TestShouldProcessPendingVmaf:
    def test_await_postprocess_skips(self):
        history = MagicMock()
        history.get_entry.return_value = {"status": "await_postprocess"}
        with patch("src.filter.get_video_codec") as mock_probe:
            assert should_process("/m/x.mkv", history, set(), "/ffprobe") is False
        mock_probe.assert_not_called()

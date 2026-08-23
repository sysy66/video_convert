"""post_transcode 后处理模块单元测试：质量门、VMAF 打分、移动落盘与最终状态。

Unit tests for the post_transcode module: quality gate, VMAF scoring, move to
destination and final history state (the second stage of the transcode pipeline).
"""

import queue
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src.post_transcode import post_transcode
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


class TestPostTranscodeMissingOutput:
    def test_missing_output_marks_failed(self, tmp_path):
        input_file = tmp_path / "test.mp4"
        input_file.write_text("fake video")
        history = _make_history(str(tmp_path / "cache" / ".work_x" / "test.mkv"))
        assert not Path(history.get_output_path(str(input_file))).exists()

        result = post_transcode(
            {"dist_frame_count": None, "elapsed": 1.0},
            str(input_file),
            history,
            "/ffmpeg",
            "/ffprobe",
            str(tmp_path),
            str(tmp_path),
            str(tmp_path / "cache"),
        )

        assert result is False
        history.mark_failed.assert_called_once_with(
            str(input_file), "output path missing after transcode"
        )


class TestPostTranscodeSync:
    def test_off_mode_accepts_on_ratio_only(self, tmp_path):
        # off 模式（VMAF_MODE_OFF）：不调打分，接受并删除源、移入最终位置。
        input_file, output, history, info = _make(tmp_path)
        with patch("src.post_transcode.config.VMAF_MODE", config.VMAF_MODE_OFF):
            mock_vmaf = MagicMock()
            result = post_transcode(
                info,
                str(input_file),
                history,
                "/ffmpeg",
                "/ffprobe",
                str(tmp_path),
                str(tmp_path),
                str(tmp_path / "cache"),
                compute_vmaf=mock_vmaf,
            )

        assert result is True
        mock_vmaf.assert_not_called()
        assert not input_file.exists()
        assert (tmp_path / "test.mkv").exists()
        history.mark_done.assert_called_once()
        assert history.mark_done.call_args[1]["rejected"] is False

    def test_accept_keeps_source_when_keep_source(self, tmp_path):
        input_file, output, history, info = _make(tmp_path)
        with patch("src.post_transcode.config.VMAF_MODE", config.VMAF_MODE_OFF):
            post_transcode(
                info,
                str(input_file),
                history,
                "/ffmpeg",
                "/ffprobe",
                str(tmp_path),
                str(tmp_path),
                str(tmp_path / "cache"),
                keep_source=True,
            )
        assert input_file.exists()
        assert (tmp_path / "test.mkv").exists()

    def test_ratio_over_max_rejects_without_scoring(self, tmp_path):
        # ratio 超标必拒，省去打分；保留源文件，标 rejected。
        input_file, output, history, info = _make(tmp_path)
        input_file.write_text("x" * 5)  # 极小源 → ratio 远超 MAX
        with (
            patch("src.post_transcode.config.VMAF_MODE", config.VMAF_MODE_SYNC),
            patch("src.post_transcode.config.ACCEPT_RATIO_MAX", 0.5),
        ):
            mock_vmaf = MagicMock(return_value=95.0)
            result = post_transcode(
                info,
                str(input_file),
                history,
                "/ffmpeg",
                "/ffprobe",
                str(tmp_path),
                str(tmp_path),
                str(tmp_path / "cache"),
                compute_vmaf=mock_vmaf,
            )

        assert result is True
        mock_vmaf.assert_not_called()
        assert input_file.exists()
        assert not (tmp_path / "test.mkv").exists()
        history.mark_done.assert_called_once_with(
            str(input_file), None, vmaf_score=None, rejected=True
        )

    def test_score_failed_marks_failed_for_retry(self, tmp_path):
        input_file, output, history, info = _make(tmp_path)
        with patch("src.post_transcode.config.VMAF_MODE", config.VMAF_MODE_SYNC):
            result = post_transcode(
                info,
                str(input_file),
                history,
                "/ffmpeg",
                "/ffprobe",
                str(tmp_path),
                str(tmp_path),
                str(tmp_path / "cache"),
                compute_vmaf=MagicMock(return_value=None),
            )
        assert result is False
        history.mark_failed.assert_called_once_with(str(input_file), "VMAF scoring failed")
        history.mark_done.assert_not_called()

    def test_score_failed_during_shutdown_defers(self, tmp_path):
        # 关停中打分失败（None）：转 await_postprocess 保留产物，不标 failed。
        input_file, output, history, info = _make(tmp_path)
        with (
            patch("src.post_transcode.config.VMAF_MODE", config.VMAF_MODE_SYNC),
            patch("src.post_transcode.is_shutting_down", return_value=True),
        ):
            result = post_transcode(
                info,
                str(input_file),
                history,
                "/ffmpeg",
                "/ffprobe",
                str(tmp_path),
                str(tmp_path),
                str(tmp_path / "cache"),
                compute_vmaf=MagicMock(return_value=None),
            )
        assert result is True
        history.mark_failed.assert_not_called()
        history.mark_done.assert_not_called()
        history.mark_await_postprocess.assert_called_once()
        assert not vmaf_tasks.empty()
        vmaf_tasks.get_nowait()

    def test_extreme_low_marks_failed_for_retry(self, tmp_path):
        input_file, output, history, info = _make(tmp_path)
        with (
            patch("src.post_transcode.config.VMAF_MODE", config.VMAF_MODE_SYNC),
            patch("src.post_transcode.config.VMAF_EXTREME_LOW", 60),
        ):
            result = post_transcode(
                info,
                str(input_file),
                history,
                "/ffmpeg",
                "/ffprobe",
                str(tmp_path),
                str(tmp_path),
                str(tmp_path / "cache"),
                compute_vmaf=MagicMock(return_value=55.0),
            )
        assert result is False
        history.mark_failed.assert_called_once()
        assert "below 60" in history.mark_failed.call_args[0][1]
        history.mark_done.assert_not_called()

    def test_accept_deletes_source_and_moves(self, tmp_path):
        input_file, output, history, info = _make(tmp_path)
        with patch("src.post_transcode.config.VMAF_MODE", config.VMAF_MODE_SYNC):
            result = post_transcode(
                info,
                str(input_file),
                history,
                "/ffmpeg",
                "/ffprobe",
                str(tmp_path),
                str(tmp_path),
                str(tmp_path / "cache"),
                compute_vmaf=MagicMock(return_value=95.0),
            )
        assert result is True
        assert not input_file.exists()
        assert (tmp_path / "test.mkv").exists()
        assert (tmp_path / "test.mkv").read_text() == "transcoded"
        history.mark_done.assert_called_once_with(
            str(input_file), str(tmp_path / "test.mkv"), vmaf_score=95.0, rejected=False
        )

    def test_reject_keeps_source(self, tmp_path):
        input_file, output, history, info = _make(tmp_path)
        with patch("src.post_transcode.config.VMAF_MODE", config.VMAF_MODE_SYNC):
            result = post_transcode(
                info,
                str(input_file),
                history,
                "/ffmpeg",
                "/ffprobe",
                str(tmp_path),
                str(tmp_path),
                str(tmp_path / "cache"),
                compute_vmaf=MagicMock(return_value=60.0),
            )
        assert result is True
        assert input_file.exists()
        assert not (tmp_path / "test.mkv").exists()
        history.mark_done.assert_called_once_with(
            str(input_file), None, vmaf_score=60.0, rejected=True
        )

    def test_move_failure_marks_failed(self, tmp_path):
        input_file, output, history, info = _make(tmp_path)
        with (
            patch("src.post_transcode.config.VMAF_MODE", config.VMAF_MODE_OFF),
            patch("shutil.copyfile", side_effect=OSError("nfs down")),
        ):
            result = post_transcode(
                info,
                str(input_file),
                history,
                "/ffmpeg",
                "/ffprobe",
                str(tmp_path),
                str(tmp_path),
                str(tmp_path / "cache"),
            )
        assert result is False
        history.mark_failed.assert_called_once()
        history.mark_done.assert_not_called()


class TestPostTranscodeAsync:
    def _make_async(self, tmp_path):
        input_file, output, history, info = _make(tmp_path)
        return input_file, output, history, info

    def test_keeps_source_moves_output_and_queues(self, tmp_path):
        input_file, output, history, info = self._make_async(tmp_path)
        with patch("src.post_transcode.config.VMAF_MODE", config.VMAF_MODE_ASYNC):
            result = post_transcode(
                info,
                str(input_file),
                history,
                "/ffmpeg",
                "/ffprobe",
                str(tmp_path),
                str(tmp_path),
                str(tmp_path / "cache"),
            )
        assert result is True
        assert input_file.exists()
        assert (tmp_path / "test.mkv").exists()
        history.mark_await_postprocess.assert_called_once()
        history.mark_done.assert_not_called()
        assert not (tmp_path / "cache" / ".work_x").exists()

        task = vmaf_tasks.get_nowait()
        assert task[0] == str(input_file)
        assert task[1] == str(tmp_path / "test.mkv")
        assert task[3] == "test"
        assert task[4] is history
        assert task[8] == "/ffprobe"

    def test_ratio_over_max_rejects_without_queue(self, tmp_path):
        input_file, output, history, info = self._make_async(tmp_path)
        input_file.write_text("x" * 5)
        with (
            patch("src.post_transcode.config.VMAF_MODE", config.VMAF_MODE_ASYNC),
            patch("src.post_transcode.config.ACCEPT_RATIO_MAX", 0.5),
        ):
            result = post_transcode(
                info,
                str(input_file),
                history,
                "/ffmpeg",
                "/ffprobe",
                str(tmp_path),
                str(tmp_path),
                str(tmp_path / "cache"),
            )
        assert result is True
        assert input_file.exists()
        assert not (tmp_path / "test.mkv").exists()
        history.mark_done.assert_called_once_with(
            str(input_file), None, vmaf_score=None, rejected=True
        )
        history.mark_await_postprocess.assert_not_called()
        assert vmaf_tasks.empty()

    def test_output_moved_into_output_dir_preserving_subdir(self, tmp_path):
        input_file = tmp_path / "input" / "sub" / "test.mp4"
        input_file.parent.mkdir(parents=True)
        input_file.write_text("fake video" * 10)
        output = tmp_path / "cache" / ".work_x" / "test.mkv"
        output.parent.mkdir(parents=True)
        output.write_text("transcoded")
        history = _make_history(str(output))
        output_dir = tmp_path / "output"

        with patch("src.post_transcode.config.VMAF_MODE", config.VMAF_MODE_ASYNC):
            result = post_transcode(
                {"dist_frame_count": None, "elapsed": 1.0},
                str(input_file),
                history,
                "/ffmpeg",
                "/ffprobe",
                str(tmp_path / "input"),
                str(output_dir),
                str(tmp_path / "cache"),
            )
        assert result is True
        assert (output_dir / "sub" / "test.mkv").exists()
        assert input_file.exists()
        history.mark_await_postprocess.assert_called_once_with(
            str(input_file), str(output_dir / "sub" / "test.mkv"), None
        )

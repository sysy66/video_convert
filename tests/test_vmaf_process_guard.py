"""VMAF 进程守卫与关停中断单元测试：
覆盖 _run_vmaf 中 Popen 进程注册/注销、超时与异常路径，以及 main_loop 关停序列中
kill_active_ffmpeg 优先于线程池 shutdown 的调用顺序。

Unit tests for VMAF process guard and shutdown interruption:
Covers Popen process registration/unregistration, timeout, and exception handling
in _run_vmaf, as well as kill_active_ffmpeg preceding thread pool shutdowns in main_loop.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src.media.ffmpeg_vmaf_scorer import FfmpegVMAFScorer

pytestmark = pytest.mark.unit


def _make_scorer(tmp_path=None, **overrides):
    defaults = dict(
        ffprobe_path="/ffprobe",
        vmaf_ffmpeg_path="/ffmpeg",
        vmaf_model_path=None,
        vmaf_log_dir=str(tmp_path) if tmp_path else "/tmp/vmaf_logs",
        n_threads=1,
        n_subsample=1,
        vmaf_timeout=config.VMAF_TIMEOUT,
        vmaf_hwaccel=False,
        vmaf_hw_fallback=True,
    )
    defaults.update(overrides)
    return FfmpegVMAFScorer(**defaults)


class TestVmafProcessGuard:
    """验证 _run_vmaf 在子进程生命周期中的 PID 注册与注销行为。"""

    def test_normal_completion_registers_and_unregisters(self, tmp_path):
        """正常完成路径：在 communicate 前注册 PID，完成后在 finally 中注销 PID。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        log_path = tmp_path / "out.json"
        log_path.write_text(json.dumps({"pooled_metrics": {"vmaf": {"mean": 95.5}}}))

        scorer = _make_scorer(tmp_path, vmaf_timeout=120)

        call_order = []

        mock_proc = MagicMock()
        mock_proc.pid = 4242
        mock_proc.returncode = 0

        def fake_communicate(timeout=None):
            call_order.append(("communicate", timeout))
            return b"", b""

        mock_proc.communicate.side_effect = fake_communicate

        def fake_register(pid):
            call_order.append(("register", pid))

        def fake_unregister(pid):
            call_order.append(("unregister", pid))

        with (
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch(
                "src.media.ffmpeg_vmaf_scorer.register_ffmpeg_pid", side_effect=fake_register
            ) as mock_reg,
            patch(
                "src.media.ffmpeg_vmaf_scorer.unregister_ffmpeg_pid", side_effect=fake_unregister
            ) as mock_unreg,
        ):
            score = scorer._run_vmaf(ref, dist, log_path)

        assert score == 95.5
        mock_popen.assert_called_once()
        mock_reg.assert_called_once_with(4242)
        mock_unreg.assert_called_once_with(4242)
        assert call_order == [
            ("register", 4242),
            ("communicate", 120),
            ("unregister", 4242),
        ]

    def test_timeout_registers_terminates_and_unregisters(self, tmp_path):
        """超时路径：communicate 抛 TimeoutExpired 时 terminate 并注销 PID，返回 None。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        scorer = _make_scorer(tmp_path, vmaf_timeout=30)

        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30)
        mock_proc.wait.return_value = 0

        call_order = []

        def fake_register(pid):
            call_order.append(("register", pid))

        def fake_unregister(pid):
            call_order.append(("unregister", pid))

        with (
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch(
                "src.media.ffmpeg_vmaf_scorer.register_ffmpeg_pid", side_effect=fake_register
            ) as mock_reg,
            patch(
                "src.media.ffmpeg_vmaf_scorer.unregister_ffmpeg_pid", side_effect=fake_unregister
            ) as mock_unreg,
        ):
            score = scorer._run_vmaf(ref, dist, None)

        assert score is None
        mock_popen.assert_called_once()
        mock_reg.assert_called_once_with(9999)
        mock_proc.terminate.assert_called_once()
        assert mock_unreg.call_count >= 1
        assert ("register", 9999) in call_order
        assert ("unregister", 9999) in call_order
        # 确保注册在注销之前发生
        assert call_order.index(("register", 9999)) < call_order.index(("unregister", 9999))

    def test_timeout_kill_if_wait_expires(self, tmp_path):
        """超时路径：若 terminate 后 wait(5) 再次超时，应调用 proc.kill()。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        scorer = _make_scorer(tmp_path, vmaf_timeout=30)

        mock_proc = MagicMock()
        mock_proc.pid = 8888
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30)
        # 第一次 wait(5) 超时，第二次 wait()（kill 后）正常返回
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5), 0]

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("src.media.ffmpeg_vmaf_scorer.register_ffmpeg_pid"),
            patch("src.media.ffmpeg_vmaf_scorer.unregister_ffmpeg_pid") as mock_unreg,
        ):
            score = scorer._run_vmaf(ref, dist, None)

        assert score is None
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()
        mock_unreg.assert_called_with(8888)

    def test_exception_unregisters_and_returns_none(self, tmp_path):
        """异常路径：communicate 抛 OSError/异常时注销 PID 并安全返回 None。"""
        ref = tmp_path / "ref.mkv"
        dist = tmp_path / "dist.mkv"
        ref.write_text("x")
        dist.write_text("x")
        scorer = _make_scorer(tmp_path)

        mock_proc = MagicMock()
        mock_proc.pid = 7777
        mock_proc.communicate.side_effect = OSError("Pipe broken")

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch("src.media.ffmpeg_vmaf_scorer.register_ffmpeg_pid") as mock_reg,
            patch("src.media.ffmpeg_vmaf_scorer.unregister_ffmpeg_pid") as mock_unreg,
        ):
            score = scorer._run_vmaf(ref, dist, None)

        assert score is None
        mock_reg.assert_called_once_with(7777)
        mock_unreg.assert_called_with(7777)


class TestMainLoopShutdownSequence:
    """验证 main_loop 关停序列中 kill_active_ffmpeg 在线程池 shutdown 前被调用。"""

    def test_kill_active_ffmpeg_before_pool_shutdown(self):
        """main_loop 退出时必须先调用 kill_active_ffmpeg，再调用 pool.shutdown。"""
        import main

        actions = []

        def fake_kill():
            actions.append("kill_active_ffmpeg")

        def fake_shutdown(self, *args, **kwargs):
            actions.append("pool_shutdown")

        main._running = False

        with (
            patch("main.kill_active_ffmpeg", side_effect=fake_kill),
            patch(
                "concurrent.futures.ThreadPoolExecutor.shutdown",
                autospec=True,
                side_effect=fake_shutdown,
            ),
            patch("main.History"),
            patch("main.validate_config", return_value=([], [])),
            patch("main.cleanup_stale_temp_dirs"),
            patch("main.cleanup_vmaf_logs"),
        ):
            main.main_loop()

        assert "kill_active_ffmpeg" in actions
        assert "pool_shutdown" in actions
        kill_idx = actions.index("kill_active_ffmpeg")
        first_shutdown_idx = actions.index("pool_shutdown")
        assert kill_idx < first_shutdown_idx, "kill_active_ffmpeg 必须在 pool.shutdown 之前调用"

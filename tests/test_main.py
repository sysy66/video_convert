"""main 模块单元测试：覆盖 CLI 覆盖解析、日志初始化、信号处理、
孤儿记录清理、主循环调度（扫描/转码/VMAF 生命周期/去重/关闭）与程序入口。

Unit tests for the main module: CLI override parsing, log setup, signal
handling, orphan-record cleanup, main-loop scheduling (scan/transcode/VMAF
lifecycle/dedup/shutdown) and the program entry point.
"""

import signal
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import main
from src import config
from src.history import History

pytestmark = pytest.mark.unit


def _reset_running():
    """将 main._running 置 True，并把 LIBRARIES 复位为一个固定的测试库。"""
    main._running = True
    config.LIBRARIES = {
        "test": {"input": "/tmp/vc-test", "output": "/tmp/vc-test", "cache": "/tmp/vc-test"}
    }


@pytest.fixture(autouse=True)
def _restore_config():
    saved = {
        "LOG_FILE": config.LOG_FILE,
        "HISTORY_FILE": config.HISTORY_FILE,
        "FFMPEG_PATH": config.FFMPEG_PATH,
        "FFPROBE_PATH": config.FFPROBE_PATH,
        "VMAF_FFMPEG_PATH": config.VMAF_FFMPEG_PATH,
        "VMAF_MODEL_PATH": config.VMAF_MODEL_PATH,
        "SCAN_INTERVAL": config.SCAN_INTERVAL,
        "MAX_WORKERS": config.MAX_WORKERS,
        "VIDEO_ENCODER": config.VIDEO_ENCODER,
        "SKIP_CODECS": config.SKIP_CODECS.copy(),
        "VMAF_HWACCEL": config.VMAF_HWACCEL,
        "VMAF_HW_FALLBACK": config.VMAF_HW_FALLBACK,
        "VMAF_MODE": config.VMAF_MODE,
        "LIBRARIES": config.LIBRARIES,
        "_LIBRARIES_RAW": config._LIBRARIES_RAW,
    }
    yield
    for name, value in saved.items():
        setattr(config, name, value)


class TestSetupLogging:
    """验证 _setup_logging 会创建日志目录并调用 logging.basicConfig。"""

    def test_creates_log_dir(self, tmp_path, monkeypatch):
        log_file = str(tmp_path / "logs" / "app.log")
        monkeypatch.setattr(config, "LOG_FILE", log_file)
        main._setup_logging()
        assert (tmp_path / "logs").is_dir()

    def test_basic_config_called(self, monkeypatch):
        """_setup_logging 应调用 logging.basicConfig 完成初始化。"""
        monkeypatch.setattr(config, "LOG_FILE", "/tmp/x/app.log")
        with patch("main.logging.basicConfig") as mock_basic:
            main._setup_logging()
        mock_basic.assert_called_once()


class TestSignalHandler:
    """验证信号处理器会把全局运行标志置为 False 以触发优雅退出。"""

    def test_sets_running_false(self):
        """收到信号后 main._running 应变为 False。"""
        import signal

        main._running = True
        with patch("main.signal_shutdown") as mock_shutdown:
            main.signal_handler(signal.SIGINT, None)
        assert main._running is False
        mock_shutdown.assert_called_once()


class TestCliOverrides:
    """验证 _apply_cli_overrides 对 VMAF 开关与编码器/skip-codecs 的覆盖。"""

    def test_vmaf_off_disables_required_and_async(self):
        """--vmaf off 应设置 VMAF_MODE 为 VMAF_MODE_OFF。"""
        original = config.VMAF_MODE
        try:
            config.VMAF_MODE = config.VMAF_MODE_SYNC
            with patch.object(sys, "argv", ["main", "--vmaf", "off"]):
                main._apply_cli_overrides()
            assert config.VMAF_MODE == config.VMAF_MODE_OFF
        finally:
            config.VMAF_MODE = original

    def test_vmaf_sync_enables_required_not_async(self):
        """--vmaf（无参数，即 sync）应设置 VMAF_MODE 为 VMAF_MODE_SYNC。"""
        original = config.VMAF_MODE
        try:
            config.VMAF_MODE = config.VMAF_MODE_OFF
            with patch.object(sys, "argv", ["main", "--vmaf"]):
                main._apply_cli_overrides()
            assert config.VMAF_MODE == config.VMAF_MODE_SYNC
        finally:
            config.VMAF_MODE = original

    def test_vmaf_async_enables_required_and_async(self):
        """--vmaf async 应设置 VMAF_MODE 为 VMAF_MODE_ASYNC。"""
        original = config.VMAF_MODE
        try:
            config.VMAF_MODE = config.VMAF_MODE_OFF
            with patch.object(sys, "argv", ["main", "--vmaf", "async"]):
                main._apply_cli_overrides()
            assert config.VMAF_MODE == config.VMAF_MODE_ASYNC
        finally:
            config.VMAF_MODE = original

    def test_default_leaves_vmaf_settings_unchanged(self):
        """不带 --vmaf 参数时，VMAF_MODE 应保持不变。"""
        original = config.VMAF_MODE
        try:
            config.VMAF_MODE = config.VMAF_MODE_OFF
            with patch.object(sys, "argv", ["main"]):
                main._apply_cli_overrides()
            assert config.VMAF_MODE == config.VMAF_MODE_OFF
        finally:
            config.VMAF_MODE = original

    def test_encoder_adds_to_skip_codecs(self):
        """--encoder hevc 应设置 VIDEO_ENCODER 并把该编码器加入 SKIP_CODECS。"""
        original_encoder = config.VIDEO_ENCODER
        original_skip = config.SKIP_CODECS.copy()
        try:
            with patch.object(sys, "argv", ["main", "--encoder", "hevc"]):
                main._apply_cli_overrides()
            assert config.VIDEO_ENCODER == "hevc"
            assert "hevc" in config.SKIP_CODECS
        finally:
            config.VIDEO_ENCODER = original_encoder
            config.SKIP_CODECS = original_skip

    def test_skip_codecs_appends(self):
        """--skip-codecs 提供的编码器应追加进 SKIP_CODECS。"""
        original = config.SKIP_CODECS.copy()
        try:
            with patch.object(sys, "argv", ["main", "--skip-codecs", "vc1", "vp9"]):
                main._apply_cli_overrides()
            assert "vc1" in config.SKIP_CODECS
            assert "vp9" in config.SKIP_CODECS
        finally:
            config.SKIP_CODECS = original


class TestCliOverridesPaths:
    """验证 _apply_cli_overrides 对路径与数值类选项的覆盖。"""

    def test_log_sets_file(self):
        """--log 应设置 config.LOG_FILE。"""
        with patch.object(sys, "argv", ["main", "--log", "/tmp/x.log"]):
            main._apply_cli_overrides()
        assert config.LOG_FILE == "/tmp/x.log"

    def test_history_sets_file(self):
        """--history 应设置 config.HISTORY_FILE。"""
        with patch.object(sys, "argv", ["main", "--history", "/tmp/h.db"]):
            main._apply_cli_overrides()
        assert config.HISTORY_FILE == "/tmp/h.db"

    def test_ffmpeg_sets_path(self):
        """--ffmpeg 应设置 config.FFMPEG_PATH。"""
        with patch.object(sys, "argv", ["main", "--ffmpeg", "/usr/bin/ffmpeg"]):
            main._apply_cli_overrides()
        assert config.FFMPEG_PATH == "/usr/bin/ffmpeg"

    def test_ffprobe_sets_path(self):
        """--ffprobe 应设置 config.FFPROBE_PATH。"""
        with patch.object(sys, "argv", ["main", "--ffprobe", "/usr/bin/ffprobe"]):
            main._apply_cli_overrides()
        assert config.FFPROBE_PATH == "/usr/bin/ffprobe"

    def test_scan_interval_sets_value(self):
        """--scan-interval 应设置 config.SCAN_INTERVAL。"""
        with patch.object(sys, "argv", ["main", "--scan-interval", "60"]):
            main._apply_cli_overrides()
        assert config.SCAN_INTERVAL == 60

    def test_max_workers_sets_value(self):
        """--max-workers 应设置 config.MAX_WORKERS。"""
        with patch.object(sys, "argv", ["main", "--max-workers", "4"]):
            main._apply_cli_overrides()
        assert config.MAX_WORKERS == 4

    def test_codec_timeout_sets_value(self):
        """--codec-timeout 应设置 config.CODEC_TIMEOUT。"""
        with patch.object(sys, "argv", ["main", "--codec-timeout", "7200"]):
            main._apply_cli_overrides()
        assert config.CODEC_TIMEOUT == 7200

    def test_vmaf_timeout_sets_value(self):
        """--vmaf-timeout 应设置 config.VMAF_TIMEOUT。"""
        with patch.object(sys, "argv", ["main", "--vmaf-timeout", "5400"]):
            main._apply_cli_overrides()
        assert config.VMAF_TIMEOUT == 5400

    def test_defaults_leave_timeouts_unchanged(self):
        """不带超时参数时，CODE_TIMEOUT 与 VMAF_TIMEOUT 应保持不变。"""
        codec_orig = config.CODEC_TIMEOUT
        vmaf_orig = config.VMAF_TIMEOUT
        with patch.object(sys, "argv", ["main"]):
            main._apply_cli_overrides()
        assert config.CODEC_TIMEOUT == codec_orig
        assert config.VMAF_TIMEOUT == vmaf_orig

    def test_vmaf_ffmpeg_sets_path(self):
        """--vmaf-ffmpeg 应设置 config.VMAF_FFMPEG_PATH。"""
        with patch.object(sys, "argv", ["main", "--vmaf-ffmpeg", "/opt/ffmpeg/ffmpeg"]):
            main._apply_cli_overrides()
        assert config.VMAF_FFMPEG_PATH == "/opt/ffmpeg/ffmpeg"

    def test_vmaf_model_sets_path(self):
        """--vmaf-model 应设置 config.VMAF_MODEL_PATH。"""
        with patch.object(sys, "argv", ["main", "--vmaf-model", "/tmp/model.json"]):
            main._apply_cli_overrides()
        assert config.VMAF_MODEL_PATH == "/tmp/model.json"

    def test_no_vmaf_hwaccel_disables(self):
        """--no-vmaf-hwaccel 应把 config.VMAF_HWACCEL 置 False。"""
        original = config.VMAF_HWACCEL
        try:
            config.VMAF_HWACCEL = True
            with patch.object(sys, "argv", ["main", "--no-vmaf-hwaccel"]):
                main._apply_cli_overrides()
            assert config.VMAF_HWACCEL is False
        finally:
            config.VMAF_HWACCEL = original

    def test_no_vmaf_hw_fallback_disables(self):
        """--no-vmaf-hw-fallback 应把 config.VMAF_HW_FALLBACK 置 False。"""
        original = config.VMAF_HW_FALLBACK
        try:
            config.VMAF_HW_FALLBACK = True
            with patch.object(sys, "argv", ["main", "--no-vmaf-hw-fallback"]):
                main._apply_cli_overrides()
            assert config.VMAF_HW_FALLBACK is False
        finally:
            config.VMAF_HW_FALLBACK = original


class TestCliOverridesLibraries:
    """验证 _apply_cli_overrides 对 --library 定义的解析与错误处理。"""

    def test_simple_path(self):
        """单段路径（NAME=PATH）应把 input/output/cache 都设为该路径。"""
        with patch.object(sys, "argv", ["main", "--library", "lib1=/mnt/lib1"]):
            main._apply_cli_overrides()
        assert config.LIBRARIES == {
            "lib1": {"input": "/mnt/lib1", "output": "/mnt/lib1", "cache": "/mnt/lib1"}
        }

    def test_three_part_path(self):
        """三段路径（NAME=in:out:cache）应分别解析 input/output/cache。"""
        with patch.object(sys, "argv", ["main", "--library", "lib1=/mnt/in:/mnt/out:/mnt/cache"]):
            main._apply_cli_overrides()
        assert config.LIBRARIES == {
            "lib1": {"input": "/mnt/in", "output": "/mnt/out", "cache": "/mnt/cache"}
        }

    def test_multiple_libraries(self):
        """多次传入 --library 应合并成多个库。"""
        with patch.object(sys, "argv", ["main", "--library", "a=/mnt/a", "--library", "b=/mnt/b"]):
            main._apply_cli_overrides()
        assert set(config.LIBRARIES) == {"a", "b"}

    def test_missing_equals_exits(self):
        """缺少 '=' 的库定义应调用 sys.exit(1)。"""
        with (
            patch.object(sys, "argv", ["main", "--library", "lib1"]),
            patch("main.sys.exit", side_effect=SystemExit) as mock_exit,
            pytest.raises(SystemExit),
        ):
            main._apply_cli_overrides()
        mock_exit.assert_called_once_with(1)

    def test_invalid_parts_exits(self):
        """路径段数非法（既非 1 也非 3）的库定义应调用 sys.exit(1)。"""
        with (
            patch.object(sys, "argv", ["main", "--library", "lib1=/mnt/in:/mnt/out"]),
            patch("main.sys.exit", side_effect=SystemExit) as mock_exit,
            pytest.raises(SystemExit),
        ):
            main._apply_cli_overrides()
        mock_exit.assert_called_once_with(1)


class TestPurgeOrphanRecords:
    """验证 History.purge_orphan_records 仅删除磁盘上已不存在的终态记录。"""

    def _seed(self, tmp_path, status, filepath):
        """写入一条指定状态的记录并落库。

        Insert one record of the given status and persist it.
        """
        h = History(str(tmp_path / "history.db"))
        if status == "failed":
            h.mark_failed(filepath, "boom")
        elif status == "rejected":
            h.mark_done(filepath, None, rejected=True)
        else:  # success
            h.mark_done(filepath, str(filepath) + ".out", rejected=False)
        return h

    def test_deletes_missing_file_records_only(self, tmp_path):
        existing = tmp_path / "still-here.mp4"
        existing.write_bytes(b"x")
        gone_rejected = str(tmp_path / "gone-rejected.mp4")
        gone_failed = str(tmp_path / "gone-failed.mp4")

        h = self._seed(tmp_path, "success", str(existing))
        h.mark_pending_transcode(gone_rejected)
        h._conn.execute("UPDATE files SET status='rejected' WHERE filepath=?", (gone_rejected,))
        h.mark_failed(gone_failed, "boom")
        # rejected/failed 记录的源已消失 → 可清理
        for p in (gone_rejected, gone_failed):
            assert Path(p).exists() is False

        h.purge_orphan_records()

        rows = {
            r["filepath"]: r["status"]
            for r in h._conn.execute("SELECT filepath, status FROM files").fetchall()
        }
        assert gone_rejected not in rows
        assert gone_failed not in rows
        assert rows[str(existing)] == "success"
        h.close()

    def test_no_op_when_nothing_missing(self, tmp_path):
        """所有终态记录对应文件仍存在时，不应删除任何记录。"""
        existing = tmp_path / "still-here.mp4"
        existing.write_bytes(b"x")

        h = self._seed(tmp_path, "rejected", str(existing))
        h.purge_orphan_records()

        rows = h._conn.execute("SELECT filepath FROM files").fetchall()
        assert [r["filepath"] for r in rows] == [str(existing)]
        h.close()


class TestMainLoop:
    """验证 main_loop 的扫描、转码调度、VMAF 生命周期、去重与关闭行为。"""

    def test_calls_walker(self):
        """主循环应调用 walk_directory，并在标志置 False 后退出。"""
        _reset_running()

        def stop_after_walk(*_a, **_kw):
            main._running = False
            return iter([])

        with (
            patch("main.walk_directory", side_effect=stop_after_walk),
            patch("main.should_submit"),
            patch("main.should_process"),
            patch("src.pipeline.transcode_stage"),
            patch("main.History"),
            patch("main.validate_config", return_value=([], [])),
            patch("main.cleanup_stale_temp_dirs"),
            patch("main.cleanup_vmaf_logs"),
        ):
            main.main_loop()

        assert main._running is False

    def test_transcodes_found_files(self):
        """扫描到的应处理文件应提交给 transcode。"""
        _reset_running()

        results = []

        def yield_file(*_a, **_kw):
            return iter([Path("/a.mp4")])

        def fake_transcode(pipeline, job, *args, **_kw):
            results.append(job.filepath)
            main._running = False
            return True

        with (
            patch("main.walk_directory", side_effect=yield_file),
            patch("main.should_submit", return_value=True),
            patch("main.should_process", return_value=True),
            patch("src.pipeline.transcode_stage", side_effect=fake_transcode) as mock_transcode,
            patch("main.History"),
            patch("main.validate_config", return_value=([], [])),
            patch("main.cleanup_stale_temp_dirs"),
            patch("main.cleanup_vmaf_logs"),
        ):
            main.main_loop()

        mock_transcode.assert_called_once()
        assert results[0].name == "a.mp4"

    def test_requeues_pending_transcode_at_startup(self):
        """启动时无条件重入队所有 pending_transcode 记录（不依赖 --retry-failed）。"""
        _reset_running()
        config.RETRY_FAILED = False
        submitted = []
        executor_cls = main.ThreadPoolExecutor

        class RecordingExecutor(executor_cls):
            def submit(self, fn, *args, **kwargs):
                submitted.append((fn, args[0].filepath.name))
                return super().submit(fn, *args, **kwargs)

        def stop_after_walk(*_a, **_kw):
            main._running = False
            return iter([])

        with (
            patch("main.History") as mock_history_cls,
            patch("main.walk_directory", side_effect=stop_after_walk),
            patch("main.should_submit"),
            patch("main.should_process"),
            patch("src.pipeline.transcode_stage", return_value=False),
            patch("main.validate_config", return_value=([], [])),
            patch("main.cleanup_stale_temp_dirs"),
            patch("main.cleanup_vmaf_logs"),
            patch("main.ThreadPoolExecutor", RecordingExecutor),
        ):
            history = mock_history_cls.return_value
            history.iter_started.return_value = [
                Path("/tmp/vc-test/a.mp4"),
                Path("/tmp/vc-test/b.mkv"),
            ]
            history.iter_failed.return_value = [Path("/tmp/vc-test/c.mp4")]
            main.main_loop()

        names = [name for _fn, name in submitted]
        assert names == ["a.mp4", "b.mkv"]
        assert "c.mp4" not in names

    def test_requeues_failed_only_with_retry_failed(self):
        """pending_transcode 始终重入队；failed 仅当 --retry-failed 开启时重入队。"""
        _reset_running()
        config.RETRY_FAILED = True
        submitted = []
        executor_cls = main.ThreadPoolExecutor

        class RecordingExecutor(executor_cls):
            def submit(self, fn, *args, **kwargs):
                submitted.append((fn, args[0].filepath.name))
                return super().submit(fn, *args, **kwargs)

        def stop_after_walk(*_a, **_kw):
            main._running = False
            return iter([])

        with (
            patch("main.History") as mock_history_cls,
            patch("main.walk_directory", side_effect=stop_after_walk),
            patch("main.should_submit"),
            patch("main.should_process"),
            patch("src.pipeline.transcode_stage", return_value=False),
            patch("main.validate_config", return_value=([], [])),
            patch("main.cleanup_stale_temp_dirs"),
            patch("main.cleanup_vmaf_logs"),
            patch("main.ThreadPoolExecutor", RecordingExecutor),
        ):
            history = mock_history_cls.return_value
            history.iter_started.return_value = [Path("/tmp/vc-test/a.mp4")]
            history.iter_failed.return_value = [Path("/tmp/vc-test/x.mp4")]
            main.main_loop()

        names = [name for _fn, name in submitted]
        assert names == ["a.mp4", "x.mp4"]

    def test_requeues_pending_postprocess_before_pending_transcode(self, tmp_path):
        """启动时应把已落盘的 async 任务送入 post_pool。

        At startup, submit landed async tasks to the post-process pool.
        """
        _reset_running()
        source = tmp_path / "source.mp4"
        final = tmp_path / "final.mkv"
        source.write_text("source")
        final.write_text("landed")
        submitted = []
        resume_called = threading.Event()
        executor_cls = main.ThreadPoolExecutor

        class RecordingExecutor(executor_cls):
            def submit(self, fn, *args, **kwargs):
                submitted.append((fn.__name__, args))
                return super().submit(fn, *args, **kwargs)

        def stop_after_walk(*_a, **_kw):
            assert resume_called.wait(2)
            main._running = False
            return iter([])

        def fake_resume(*_args):
            resume_called.set()

        with (
            patch("main.History") as mock_history_cls,
            patch("main.walk_directory", side_effect=stop_after_walk),
            patch("main.should_submit"),
            patch("main.should_process"),
            patch("main.resume_pending_postprocess", side_effect=fake_resume) as mock_resume,
            patch("main.validate_config", return_value=([], [])),
            patch("main.cleanup_stale_temp_dirs"),
            patch("main.cleanup_vmaf_logs"),
            patch("main.ThreadPoolExecutor", RecordingExecutor),
        ):
            history = mock_history_cls.return_value
            history.iter_pending_postprocess.return_value = [str(source)]
            history.get_output_path.return_value = str(final)
            history.iter_started.return_value = []
            main.main_loop()

        resume_submissions = [item for item in submitted if item[0] == "resume_post_process"]
        assert resume_submissions == [("resume_post_process", (str(source), str(final)))]
        mock_resume.assert_called_once()
        assert mock_resume.call_args.args[1:] == (str(source), str(final))
        calls = [call[0] for call in history.method_calls]
        assert calls.index("iter_pending_postprocess") < calls.index("iter_started")

    def test_next_scan_starts_while_transcode_running(self):
        """转码仍在运行期间即应开始下一轮扫描（SCAN_INTERVAL=0，扫描与转码解耦）。"""
        _reset_running()
        started = threading.Event()
        finished = threading.Event()
        second_scan_observed = []

        def fake_transcode(pipeline, job, *args, **_kw):
            started.set()
            time.sleep(0.2)
            finished.set()

        def fake_walk(*_a, **_kw):
            if not started.is_set():
                return iter([Path("/a.mp4")])
            second_scan_observed.append(finished.is_set())
            main._running = False
            return iter([])

        with (
            patch("main.config.SCAN_INTERVAL", 0),
            patch("main.walk_directory", side_effect=fake_walk),
            patch("main.should_submit", return_value=True),
            patch("main.should_process", return_value=True),
            patch("src.pipeline.transcode_stage", side_effect=fake_transcode),
            patch("main.History"),
            patch("main.validate_config", return_value=([], [])),
            patch("main.cleanup_stale_temp_dirs"),
            patch("main.cleanup_vmaf_logs"),
        ):
            main.main_loop()

        assert second_scan_observed == [False]

    def test_pending_dedup_prevents_resubmit(self):
        """同一文件在不同扫描周期重复出现时，_pending 去重应阻止重复提交转码。"""
        _reset_running()
        calls = []
        walk_calls = []

        def fake_transcode(pipeline, job, *args, **_kw):
            calls.append(job.filepath)
            time.sleep(0.2)

        def fake_walk(*_a, **_kw):
            walk_calls.append(1)
            if len(walk_calls) == 1:
                return iter([Path("/a.mp4")])
            if len(walk_calls) == 2:
                main._running = False
                return iter([Path("/a.mp4")])
            return iter([])

        with (
            patch("main.config.SCAN_INTERVAL", 0),
            patch("main.walk_directory", side_effect=fake_walk),
            patch("main.should_submit", return_value=True),
            patch("main.should_process", return_value=True),
            patch("src.pipeline.transcode_stage", side_effect=fake_transcode),
            patch("main.History"),
            patch("main.validate_config", return_value=([], [])),
            patch("main.cleanup_stale_temp_dirs"),
            patch("main.cleanup_vmaf_logs"),
        ):
            main.main_loop()

        assert len(calls) == 1

    def test_skips_submit_for_processed_files(self):
        """should_submit 返回 False（已处理文件）时不应调用 transcode。"""
        _reset_running()

        def fake_walk(*_a, **_kw):
            main._running = False
            return iter([Path("/a.mp4")])

        with (
            patch("main.walk_directory", side_effect=fake_walk),
            patch("main.should_submit", return_value=False),
            patch("main.should_process"),
            patch("src.pipeline.transcode_stage") as mock_transcode,
            patch("main.History"),
            patch("main.validate_config", return_value=([], [])),
            patch("main.cleanup_stale_temp_dirs"),
            patch("main.cleanup_vmaf_logs"),
        ):
            main.main_loop()

        mock_transcode.assert_not_called()

    def test_cancel_queued_tasks_on_shutdown(self):
        """MAX_WORKERS=1 时排队中的任务在关闭时应被取消，仅运行中的任务执行。"""
        _reset_running()
        calls = []
        started = threading.Event()
        walk_calls = []

        def fake_transcode(pipeline, job, *args, **_kw):
            calls.append(job.filepath)
            started.set()
            time.sleep(0.2)

        def fake_walk(*_a, **_kw):
            walk_calls.append(1)
            if len(walk_calls) == 1:
                return iter([Path("/a.mp4"), Path("/b.mp4")])
            started.wait(2)
            main._running = False
            return iter([])

        with (
            patch("main.config.MAX_WORKERS", 1),
            patch("main.config.SCAN_INTERVAL", 0),
            patch("main.walk_directory", side_effect=fake_walk),
            patch("main.should_submit", return_value=True),
            patch("main.should_process", return_value=True),
            patch("src.pipeline.transcode_stage", side_effect=fake_transcode),
            patch("main.History"),
            patch("main.validate_config", return_value=([], [])),
            patch("main.cleanup_stale_temp_dirs"),
            patch("main.cleanup_vmaf_logs"),
        ):
            main.main_loop()

        assert len(calls) == 1


class TestMainEntry:
    """验证 main() 的单实例锁、信号注册、主循环异常处理与收尾逻辑。"""

    def test_lock_acquired_registers_signals(self, tmp_path, monkeypatch):
        """成功加锁后应注册 SIGINT/SIGTERM，并关闭历史对象与清理活动 ffmpeg 进程。"""
        monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "logs" / "app.log"))
        (tmp_path / "logs").mkdir()
        history = MagicMock()
        with (
            patch("main._apply_cli_overrides"),
            patch("main._setup_logging"),
            patch("main.fcntl.flock"),
            patch("main.signal.signal") as mock_signal,
            patch("main.main_loop", return_value=history),
            patch("main.kill_active_ffmpeg") as mock_kill,
        ):
            main.main()

        mock_signal.assert_any_call(signal.SIGINT, main.signal_handler)
        mock_signal.assert_any_call(signal.SIGTERM, main.signal_handler)
        history.close.assert_called_once()
        mock_kill.assert_called_once()

    def test_lock_unavailable_exits(self, tmp_path, monkeypatch):
        """flock 抛 BlockingIOError（另一实例持有锁）时 main() 应退出码 1。"""
        monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "logs" / "app.log"))
        (tmp_path / "logs").mkdir()
        with (
            patch("main._apply_cli_overrides"),
            patch("main._setup_logging"),
            patch("main.fcntl.flock", side_effect=BlockingIOError),
            patch("main.sys.exit", side_effect=SystemExit) as mock_exit,
            pytest.raises(SystemExit),
        ):
            main.main()

        mock_exit.assert_called_once_with(1)

    def test_lock_oserror_exits(self, tmp_path, monkeypatch):
        """flock 抛 OSError 时 main() 应退出码 1。"""
        monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "logs" / "app.log"))
        (tmp_path / "logs").mkdir()
        with (
            patch("main._apply_cli_overrides"),
            patch("main._setup_logging"),
            patch("main.fcntl.flock", side_effect=OSError("no lock")),
            patch("main.sys.exit", side_effect=SystemExit) as mock_exit,
            pytest.raises(SystemExit),
        ):
            main.main()

        mock_exit.assert_called_once_with(1)

    def test_main_loop_exception_reraised_and_cleanup(self, tmp_path, monkeypatch):
        """main_loop 抛异常应重抛，并在退出时清理活动 ffmpeg 进程。"""
        monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "logs" / "app.log"))
        (tmp_path / "logs").mkdir()
        with (
            patch("main._apply_cli_overrides"),
            patch("main._setup_logging"),
            patch("main.fcntl.flock"),
            patch("main.signal.signal"),
            patch("main.main_loop", side_effect=RuntimeError("boom")),
            patch("main.kill_active_ffmpeg") as mock_kill,
            pytest.raises(RuntimeError, match="boom"),
        ):
            main.main()

        mock_kill.assert_called_once()

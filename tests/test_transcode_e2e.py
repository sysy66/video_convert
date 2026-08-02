"""Task 15: transcode() 端到端集成测试 — 真实 QSV 硬件编码（需要 --run-slow 且本机有 QSV）"""

from pathlib import Path

import pytest

from src import config
from src.transcoder import (
    _handle_vmaf_task,
    drain_vmaf_tasks,
    start_vmaf_workers,
    transcode,
    vmaf_tasks,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def transcode_env(tmp_path, monkeypatch, make_history):
    monkeypatch.setattr(config, "VMAF_REQUIRED", False)
    monkeypatch.setattr(config, "VMAF_LOG_DIR", str(tmp_path / "vmaf_logs"))
    monkeypatch.setattr(config, "ACCEPT_RATIO_MAX", 2.0)
    src_dir = tmp_path / "input"
    src_dir.mkdir()
    src_file = src_dir / "synth_320x240_1s_h264.mp4"
    src_file.write_bytes((FIXTURE_DIR / "synth_320x240_1s_h264.mp4").read_bytes())
    return {
        "src": src_file,
        "output_dir": tmp_path / "output",
        "cache_dir": tmp_path / "cache",
        "history": make_history(),
    }


def _skip_if_no_qsv(qsv_available):
    if not qsv_available:
        pytest.skip("QSV 不可用（无 /dev/dri 或驱动缺失），跳过真实硬件转码测试")


class TestTranscodeQsv:
    def test_hevc_success(
        self, transcode_env, transcode_ffmpeg, transcode_ffprobe, qsv_available, monkeypatch
    ):
        _skip_if_no_qsv(qsv_available)
        monkeypatch.setattr(config, "VIDEO_ENCODER", "hevc")
        env = transcode_env

        ok = transcode(
            str(env["src"]),
            env["history"],
            transcode_ffmpeg,
            transcode_ffprobe,
            output_dir=str(env["output_dir"]),
            cache_dir=str(env["cache_dir"]),
        )

        assert ok
        assert env["history"].is_success(str(env["src"]))
        outputs = list(env["output_dir"].glob("*.mkv"))
        assert len(outputs) == 1
        assert not env["src"].exists()
        assert list(env["cache_dir"].glob(config.TEMP_PREFIX + "*")) == []

    def test_av1_success_or_skip_on_unsupported_gpu(
        self, transcode_env, transcode_ffmpeg, transcode_ffprobe, qsv_available, monkeypatch
    ):
        _skip_if_no_qsv(qsv_available)
        monkeypatch.setattr(config, "VIDEO_ENCODER", "av1")
        env = transcode_env

        ok = transcode(
            str(env["src"]),
            env["history"],
            transcode_ffmpeg,
            transcode_ffprobe,
            output_dir=str(env["output_dir"]),
            cache_dir=str(env["cache_dir"]),
        )

        if ok:
            assert env["history"].is_success(str(env["src"]))
            assert not env["src"].exists()
            return
        entry = env["history"].get_entry(str(env["src"]))
        error = (entry or {}).get("error", "")
        if "unsupported" in error.lower():
            pytest.skip(f"GPU 不支持 av1_qsv 硬件编码: {error[:200]}")
        raise AssertionError(f"av1_qsv 转码意外失败: {error}")

    def test_transcode_keeps_output_duration(
        self, transcode_env, transcode_ffmpeg, transcode_ffprobe, qsv_available, monkeypatch
    ):
        _skip_if_no_qsv(qsv_available)
        monkeypatch.setattr(config, "VIDEO_ENCODER", "hevc")
        env = transcode_env

        ok = transcode(
            str(env["src"]),
            env["history"],
            transcode_ffmpeg,
            transcode_ffprobe,
            output_dir=str(env["output_dir"]),
            cache_dir=str(env["cache_dir"]),
        )
        assert ok

        from src.probe import get_streams_info

        out = list(env["output_dir"].glob("*.mkv"))[0]
        info = get_streams_info(str(out), transcode_ffprobe)
        assert info is not None
        assert info["duration"] == pytest.approx(1.0, abs=0.1)
        assert info["streams"][0]["codec_name"] == "hevc"


class TestTranscodeVmafAsync:
    """异步 VMAF 打分全流程：转码立即返回（源保留 + pending_vmaf），worker 打分后终态化。"""

    def test_async_worker_full_flow(
        self,
        transcode_env,
        transcode_ffmpeg,
        transcode_ffprobe,
        qsv_available,
        vmaf_ffmpeg,
        monkeypatch,
    ):
        """真实 QSV 转码 → 真实后台 worker + 真实 libvmaf 打分 → accept 删源终态化"""
        _skip_if_no_qsv(qsv_available)
        monkeypatch.setattr(config, "VIDEO_ENCODER", "hevc")
        monkeypatch.setattr(config, "VMAF_ASYNC", True)
        monkeypatch.setattr(config, "VMAF_FFMPEG_PATH", vmaf_ffmpeg)
        env = transcode_env

        start_vmaf_workers()
        try:
            ok = transcode(
                str(env["src"]),
                env["history"],
                transcode_ffmpeg,
                transcode_ffprobe,
                output_dir=str(env["output_dir"]),
                cache_dir=str(env["cache_dir"]),
            )
            assert ok
            # 转码返回时打分尚未进行：源保留、输出已在 output 区、状态 pending_vmaf
            assert env["src"].exists()
            outputs = list(env["output_dir"].glob("*.mkv"))
            assert len(outputs) == 1
            assert env["history"].is_pending_vmaf(str(env["src"]))
            assert not env["history"].is_success(str(env["src"]))

            drain_vmaf_tasks()

            # worker 打分完成：accept（hevc 转码输出远小于 2.0 上限）→ 源删除、输出保留
            assert env["history"].is_success(str(env["src"]))
            assert not env["src"].exists()
            assert outputs[0].exists()
            entry = env["history"].get_entry(str(env["src"]))
            assert entry["vmaf_score"] is not None
            assert list(env["cache_dir"].glob(config.TEMP_PREFIX + "*")) == []
            assert list(Path(config.VMAF_LOG_DIR).glob("*.json")) != []
        finally:
            drain_vmaf_tasks()

    def test_async_handle_task_real_vmaf(
        self,
        transcode_env,
        transcode_ffmpeg,
        transcode_ffprobe,
        qsv_available,
        vmaf_ffmpeg,
        monkeypatch,
    ):
        """直接消费队列任务做真实打分（不经线程，验证 _handle_vmaf_task 真实路径）"""
        _skip_if_no_qsv(qsv_available)
        monkeypatch.setattr(config, "VIDEO_ENCODER", "hevc")
        monkeypatch.setattr(config, "VMAF_ASYNC", True)
        monkeypatch.setattr(config, "VMAF_FFMPEG_PATH", vmaf_ffmpeg)
        env = transcode_env

        ok = transcode(
            str(env["src"]),
            env["history"],
            transcode_ffmpeg,
            transcode_ffprobe,
            output_dir=str(env["output_dir"]),
            cache_dir=str(env["cache_dir"]),
        )
        assert ok

        task = vmaf_tasks.get_nowait()
        assert task[0] == str(env["src"])
        assert task[1] == str(list(env["output_dir"].glob("*.mkv"))[0])
        assert task[3] == "synth_320x240_1s_h264"
        assert task[4] is env["history"]

        _handle_vmaf_task(task)

        assert env["history"].is_success(str(env["src"]))
        assert not env["src"].exists()
        entry = env["history"].get_entry(str(env["src"]))
        assert entry["vmaf_score"] is not None


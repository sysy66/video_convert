"""transcode() + post_transcode() 端到端集成测试：真实 QSV 硬件编码
（需 --run-slow 且本机有 QSV）。

End-to-end integration tests for the two-stage pipeline (transcode then
post_transcode): real QSV hardware encoding (requires --run-slow and local QSV).
"""

from pathlib import Path

import pytest

from src import config
from src.ffmpeg_vmaf_scorer import FfmpegVMAFScorer
from src.pipeline import TranscodePipeline
from src.post_process import post_transcode
from src.vmaf_async import (
    _handle_vmaf_task,
    drain_vmaf_tasks,
    start_vmaf_workers,
    vmaf_tasks,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def transcode_env(tmp_path, monkeypatch, make_history):
    monkeypatch.setattr(config, "VMAF_MODE", config.VMAF_MODE_OFF)
    monkeypatch.setattr(config, "VMAF_LOG_DIR", str(tmp_path / "vmaf_logs"))
    monkeypatch.setattr(config, "ACCEPT_RATIO_MAX", 2.0)
    src_dir = tmp_path / "input"
    src_dir.mkdir()
    src_file = src_dir / "synth_320x240_1s_h264.mp4"
    src_file.write_bytes((FIXTURE_DIR / "synth_320x240_1s_h264.mp4").read_bytes())
    return {
        "src": src_file,
        "input_dir": src_dir,
        "output_dir": tmp_path / "output",
        "cache_dir": tmp_path / "cache",
        "history": make_history(),
    }


@pytest.fixture
def compute_vmaf(tmp_path, monkeypatch, vmaf_ffmpeg):
    """真实 FfmpegVMAFScorer 打分回调（async worker / _handle_vmaf_task 使用）。"""
    monkeypatch.setattr(config, "VMAF_FFMPEG_PATH", vmaf_ffmpeg)
    ffscorer = FfmpegVMAFScorer(
        ffprobe_path=config.FFPROBE_PATH,
        vmaf_ffmpeg_path=vmaf_ffmpeg,
        vmaf_model_path=config.VMAF_MODEL_PATH,
        vmaf_log_dir=config.VMAF_LOG_DIR,
        n_threads=config.VMAF_N_THREADS,
        n_subsample=config.VMAF_N_SUBSAMPLE,
        vmaf_timeout=config.VMAF_TIMEOUT,
        vmaf_hwaccel=config.VMAF_HWACCEL,
        vmaf_hw_fallback=config.VMAF_HW_FALLBACK,
    )
    return ffscorer.compute_vmaf


def _skip_if_no_qsv(qsv_available):
    if not qsv_available:
        pytest.skip("QSV 不可用（无 /dev/dri 或驱动缺失），跳过真实硬件转码测试")


def _run_pipeline(env, ffmpeg, ffprobe, compute_vmaf=None):
    """两段式流水线：transcode 编码 → post_transcode 质量门/落盘/终态。

    Run the two-stage pipeline: transcode (encoding only) then post_transcode
    (quality gate, placement, final state).
    """
    pipeline = TranscodePipeline(
        env["history"],
        ffmpeg,
        ffprobe,
        compute_vmaf=compute_vmaf,
    )
    job = pipeline.make_job(
        str(env["src"]),
        {
            "input": str(env["input_dir"]),
            "output": str(env["output_dir"]),
            "cache": str(env["cache_dir"]),
        },
    )
    ok = job.transcode()
    assert ok, "transcode 编码阶段失败"
    done = post_transcode(
        {"dist_frame_count": job.dist_frame_count, "elapsed": job.elapsed},
        str(env["src"]),
        env["history"],
        ffmpeg,
        ffprobe,
        str(env["input_dir"]),
        str(env["output_dir"]),
        str(env["cache_dir"]),
        compute_vmaf=compute_vmaf,
    )
    assert done, "post_transcode 后处理阶段失败"
    return job.dist_frame_count


class TestTranscodeQsv:
    def test_hevc_success(
        self, transcode_env, transcode_ffmpeg, transcode_ffprobe, qsv_available, monkeypatch
    ):
        _skip_if_no_qsv(qsv_available)
        monkeypatch.setattr(config, "VIDEO_ENCODER", "hevc")
        env = transcode_env

        _run_pipeline(env, transcode_ffmpeg, transcode_ffprobe)

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

        try:
            _run_pipeline(env, transcode_ffmpeg, transcode_ffprobe)
        except AssertionError:
            entry = env["history"].get_entry(str(env["src"]))
            error = (entry or {}).get("error", "")
            if "unsupported" in error.lower():
                pytest.skip(f"GPU 不支持 av1_qsv 硬件编码: {error[:200]}")
            raise

        assert env["history"].is_success(str(env["src"]))
        assert not env["src"].exists()

    def test_transcode_keeps_output_duration(
        self, transcode_env, transcode_ffmpeg, transcode_ffprobe, qsv_available, monkeypatch
    ):
        _skip_if_no_qsv(qsv_available)
        monkeypatch.setattr(config, "VIDEO_ENCODER", "hevc")
        env = transcode_env

        _run_pipeline(env, transcode_ffmpeg, transcode_ffprobe)

        from src.probe import get_streams_info

        out = list(env["output_dir"].glob("*.mkv"))[0]
        info = get_streams_info(str(out), transcode_ffprobe)
        assert info is not None
        assert info["duration"] == pytest.approx(1.0, abs=0.1)
        assert info["streams"][0]["codec_name"] == "hevc"


class TestTranscodeVmafAsync:
    """异步 VMAF 打分全流程：转码返回（源保留 + await_postprocess），worker 打分后终态化。"""

    def test_async_worker_full_flow(
        self,
        transcode_env,
        transcode_ffmpeg,
        transcode_ffprobe,
        qsv_available,
        compute_vmaf,
        monkeypatch,
    ):
        """真实 QSV 转码 → post_transcode 排队 → 真实后台 worker + libvmaf 打分 → accept 删源"""
        _skip_if_no_qsv(qsv_available)
        monkeypatch.setattr(config, "VIDEO_ENCODER", "hevc")
        monkeypatch.setattr(config, "VMAF_MODE", config.VMAF_MODE_ASYNC)
        env = transcode_env

        start_vmaf_workers(compute_vmaf=compute_vmaf)
        try:
            _run_pipeline(env, transcode_ffmpeg, transcode_ffprobe)
            # post_transcode 异步排队：源保留、输出已在 output 区、状态 await_postprocess
            assert env["src"].exists()
            outputs = list(env["output_dir"].glob("*.mkv"))
            assert len(outputs) == 1
            assert env["history"].is_pending_vmaf(str(env["src"]))
            assert not env["history"].is_success(str(env["src"]))

            drain_vmaf_tasks()

            # worker 打分完成：accept（hevc 输出远小于 2.0 上限）→ 源删除、输出保留
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
        compute_vmaf,
        monkeypatch,
    ):
        """直接消费队列任务做真实打分（不经线程，验证 _handle_vmaf_task 真实路径）"""
        _skip_if_no_qsv(qsv_available)
        monkeypatch.setattr(config, "VIDEO_ENCODER", "hevc")
        monkeypatch.setattr(config, "VMAF_MODE", config.VMAF_MODE_ASYNC)
        env = transcode_env

        _run_pipeline(env, transcode_ffmpeg, transcode_ffprobe)

        task = vmaf_tasks.get_nowait()
        assert task[0] == str(env["src"])
        assert task[1] == str(list(env["output_dir"].glob("*.mkv"))[0])
        assert task[3] == "synth_320x240_1s_h264"
        assert task[4] is env["history"]
        assert task[5] >= 0

        _handle_vmaf_task(task, compute_vmaf=compute_vmaf)

        assert env["history"].is_success(str(env["src"]))
        assert not env["src"].exists()
        entry = env["history"].get_entry(str(env["src"]))
        assert entry["vmaf_score"] is not None

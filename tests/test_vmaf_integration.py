"""VMAF 集成测试：真实 libvmaf 计算（需 --run-slow 且存在带 libvmaf 的 ffmpeg）。

VMAF integration tests: real libvmaf computation (requires --run-slow and an
ffmpeg built with libvmaf).
"""

import json
from pathlib import Path

import pytest

from src import config
from src.media.ffmpeg_vmaf_scorer import FfmpegVMAFScorer

pytestmark = [pytest.mark.integration, pytest.mark.slow]

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _make_scorer(vmaf_ffmpeg):
    """以显式注入构造 scorer，打分二进制用 vmaf_ffmpeg 夹具。

    Build a scorer with explicit injection; the scoring binary comes from the
    vmaf_ffmpeg fixture.
    """
    return FfmpegVMAFScorer(
        ffprobe_path=config.FFPROBE_PATH,
        vmaf_ffmpeg_path=vmaf_ffmpeg,
        vmaf_model_path=config.VMAF_MODEL_PATH,
        vmaf_log_dir=config.VMAF_LOG_DIR,
        n_threads=config.VMAF_N_THREADS,
        n_subsample=config.VMAF_N_SUBSAMPLE,
        vmaf_timeout=config.VMAF_TIMEOUT,
        vmaf_hwaccel=False,
        vmaf_hw_fallback=True,
    )


class TestComputeVmafIntegration:
    def test_score_between_0_and_100(self, vmaf_ffmpeg, tmp_path):
        log_path = tmp_path / "vmaf.json"
        scorer = _make_scorer(vmaf_ffmpeg)
        score = scorer.compute_vmaf(
            FIXTURE_DIR / "synth_320x240_1s_h264.mp4",
            FIXTURE_DIR / "synth_320x240_1s_hevc.mp4",
            log_path=str(log_path),
        )
        assert score is not None
        assert 0 < score <= 100

    def test_json_log_written(self, vmaf_ffmpeg, tmp_path):
        log_path = tmp_path / "vmaf.json"
        scorer = _make_scorer(vmaf_ffmpeg)
        scorer.compute_vmaf(
            FIXTURE_DIR / "synth_320x240_1s_h264.mp4",
            FIXTURE_DIR / "synth_320x240_1s_hevc.mp4",
            log_path=str(log_path),
        )
        assert log_path.exists()
        data = json.loads(log_path.read_text())
        assert "frames" in data
        assert "pooled_metrics" in data

    def test_identical_inputs_high_score(self, vmaf_ffmpeg):
        scorer = _make_scorer(vmaf_ffmpeg)
        score = scorer.compute_vmaf(
            FIXTURE_DIR / "synth_320x240_1s_h264.mp4",
            FIXTURE_DIR / "synth_320x240_1s_h264.mp4",
        )
        assert score is not None
        assert score > 95

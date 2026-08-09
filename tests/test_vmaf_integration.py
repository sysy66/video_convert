"""VMAF 集成测试：真实 libvmaf 计算（需 --run-slow 且存在带 libvmaf 的 ffmpeg）。

VMAF integration tests: real libvmaf computation (requires --run-slow and an
ffmpeg built with libvmaf).
"""

import json
from pathlib import Path

import pytest

from src.quality import compute_vmaf

pytestmark = [pytest.mark.integration, pytest.mark.slow]

FIXTURE_DIR = Path(__file__).parent / "fixtures"


class TestComputeVmafIntegration:
    def test_score_between_0_and_100(self, vmaf_ffmpeg, tmp_path):
        log_path = tmp_path / "vmaf.json"
        score = compute_vmaf(
            FIXTURE_DIR / "synth_320x240_1s_h264.mp4",
            FIXTURE_DIR / "synth_320x240_1s_hevc.mp4",
            ffmpeg_path=vmaf_ffmpeg,
            log_path=str(log_path),
        )
        assert score is not None
        assert 0 < score <= 100

    def test_json_log_written(self, vmaf_ffmpeg, tmp_path):
        log_path = tmp_path / "vmaf.json"
        compute_vmaf(
            FIXTURE_DIR / "synth_320x240_1s_h264.mp4",
            FIXTURE_DIR / "synth_320x240_1s_hevc.mp4",
            ffmpeg_path=vmaf_ffmpeg,
            log_path=str(log_path),
        )
        assert log_path.exists()
        data = json.loads(log_path.read_text())
        assert "frames" in data
        assert "pooled_metrics" in data

    def test_identical_inputs_high_score(self, vmaf_ffmpeg):
        score = compute_vmaf(
            FIXTURE_DIR / "synth_320x240_1s_h264.mp4",
            FIXTURE_DIR / "synth_320x240_1s_h264.mp4",
            ffmpeg_path=vmaf_ffmpeg,
        )
        assert score is not None
        assert score > 95

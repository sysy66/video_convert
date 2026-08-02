"""Task 19: 性能基准 — 真实编码速度与 VMAF 计算耗时（pytest-benchmark）

默认被 --benchmark-disable 跳过；手动运行：
  pytest --benchmark-enable --run-slow tests/test_benchmarks.py
"""

import subprocess
from pathlib import Path

import pytest

from src.quality import compute_vmaf

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _encode(ffmpeg_bin, input_path, output_path):
    subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-crf",
            "23",
            str(output_path),
        ],
        capture_output=True,
        check=True,
    )
    output_path.unlink(missing_ok=True)


def test_encode_speed_h264(ffmpeg_bin, benchmark, tmp_path):
    src = FIXTURE_DIR / "synth_320x240_1s_h264_aac.mp4"
    out = tmp_path / "out.mp4"
    benchmark(_encode, ffmpeg_bin, src, out)


def test_vmaf_compute_time(vmaf_ffmpeg, benchmark):
    ref = FIXTURE_DIR / "synth_320x240_1s_h264.mp4"
    dist = FIXTURE_DIR / "synth_320x240_1s_hevc.mp4"
    benchmark(compute_vmaf, ref, dist, ffmpeg_path=vmaf_ffmpeg)

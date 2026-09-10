"""Task 19: 性能基准 — 真实编码速度与 VMAF 计算耗时（pytest-benchmark）

默认被 --benchmark-disable 跳过；手动运行：
  pytest --benchmark-enable --run-slow tests/test_benchmarks.py
"""

import subprocess
from pathlib import Path

import pytest

from src import config
from src.media.ffmpeg_vmaf_scorer import FfmpegVMAFScorer

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]

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
        vmaf_hwaccel=config.VMAF_HWACCEL,
        vmaf_hw_fallback=config.VMAF_HW_FALLBACK,
    )


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
    scorer = _make_scorer(vmaf_ffmpeg)
    benchmark(scorer.compute_vmaf, ref, dist)

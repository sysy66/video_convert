"""回归测试：锁定 fixtures 哈希与重编码输出确定性（需 --run-slow）。

Regression tests: lock fixture SHA256 hashes and deterministic re-encoding (requires --run-slow).
"""

import hashlib
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.regression, pytest.mark.slow]

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# 提交时记录的 SHA256 基线：fixture 被意外修改/重新生成时会失败。
# 有意重新生成 fixtures 时：重新生成 → 更新此表 → 单独 commit 记录变更。
FIXTURE_HASHES = {
    name: digest
    for name, digest in [
        (
            "synth_320x240_1s_h264.mp4",
            "6be60c03ff4ea5950829fa41eaa24c372442b90c5bd7c030bc1ec07fae3cc307",
        ),
        (
            "synth_320x240_1s_h264_aac.mp4",
            "b8f86ea8eadc6719d129464a0477cacbb004e9ccb3227c5e086c37cf848245ec",
        ),
        (
            "synth_320x240_1s_hevc.mp4",
            "3334c942641e46c5ac6d163519bf80c2eca16978df0ded4436e538c6f4e698c5",
        ),
        (
            "synth_320x240_1s_hevc10bit.mkv",
            "630badb3439c55b767127d23153fa6a27743d9ec69d5fad7710594486b35ef9b",
        ),
        (
            "synth_320x240_1s_mpeg4.avi",
            "d8abf1791acc77e09e6f592c1d400afea9b0ba18a15d375df0c92f6802ba5819",
        ),
        (
            "synth_320x240_1s_vp9.webm",
            "0124dfb4b7efb10b21743e1cf23c67d638af70f91e024872055857f97225284b",
        ),
    ]
}


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_fixture_hashes_stable():
    videos = {
        p.name: _sha256(p)
        for p in FIXTURE_DIR.iterdir()
        if p.is_file() and p.suffix in {".mp4", ".mkv", ".avi", ".webm"}
    }
    assert videos == FIXTURE_HASHES


def _encode(ffmpeg_bin, input_path, output_path):
    return subprocess.run(
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


def test_reencode_output_deterministic(ffmpeg_bin, tmp_path):
    """同一输入 + 同一参数两次编码，输出字节必须一致（锁定编码行为）"""
    src = FIXTURE_DIR / "synth_320x240_1s_h264_aac.mp4"
    out1 = tmp_path / "out1.mp4"
    out2 = tmp_path / "out2.mp4"
    _encode(ffmpeg_bin, src, out1)
    _encode(ffmpeg_bin, src, out2)
    assert out1.read_bytes() == out2.read_bytes()

import os
import shutil
import subprocess

import pytest

from src import config
from src.history import History


def pytest_addoption(parser):
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="运行标记为 slow 的集成测试（真实 ffmpeg/ffprobe/QSV/VMAF）",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-slow"):
        return
    skip_slow = pytest.mark.skip(reason="slow 集成测试，需要 --run-slow 启用")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


@pytest.fixture(scope="session")
def ffmpeg_bin():
    path = shutil.which("ffmpeg")
    if not path:
        pytest.skip("ffmpeg 不在 PATH 中，跳过集成测试")
    return path


@pytest.fixture(scope="session")
def ffprobe_bin():
    path = shutil.which("ffprobe")
    if not path:
        pytest.skip("ffprobe 不在 PATH 中，跳过集成测试")
    return path


@pytest.fixture(scope="session")
def transcode_ffmpeg():
    path = config.FFMPEG_PATH
    if not os.path.exists(path):
        pytest.skip(f"转码 ffmpeg 不存在: {path}（config.FFMPEG_PATH）")
    return path


@pytest.fixture(scope="session")
def transcode_ffprobe():
    path = config.FFPROBE_PATH
    if not os.path.exists(path):
        pytest.skip(f"转码 ffprobe 不存在: {path}（config.FFPROBE_PATH）")
    return path


@pytest.fixture(scope="session")
def qsv_available(transcode_ffmpeg):
    """QSV 是否可用：试跑设备初始化（CI 无 /dev/dri 时返回 False，测试自行 skip）"""
    result = subprocess.run(
        [
            transcode_ffmpeg,
            "-hide_banner",
            "-init_hw_device",
            "qsv=probe",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x64:rate=10:duration=0.1",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        timeout=60,
    )
    return result.returncode == 0


@pytest.fixture(scope="session")
def vmaf_ffmpeg():
    """带 libvmaf 的 ffmpeg 路径；候选自编译统一版 → config.VMAF_FFMPEG_PATH → PATH，均无则 skip。

    注意：2024 版 johnvansickle 静态构建有 scale2ref 断言 bug
    （Assertion best_input >= 0 failed at fftools/ffmpeg_filter.c:2075），
    故优先使用 /opt/ffmpeg-qsv-vmaf 自编译统一版（SETUP.md 1.3）。
    """
    candidates = (
        "/opt/ffmpeg-qsv-vmaf/bin/ffmpeg",
        config.VMAF_FFMPEG_PATH,
        shutil.which("ffmpeg"),
    )
    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        if not os.path.exists(cand):
            continue
        try:
            output = subprocess.run(
                [cand, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30
            ).stdout
        except OSError:
            continue
        if "libvmaf" in output:
            return cand
    pytest.skip("未找到带 libvmaf 的 ffmpeg（如 /opt/johnvansickle-ffmpeg/ffmpeg）")


@pytest.fixture
def make_history(tmp_path):
    def _make_history(name="history.db"):
        return History(str(tmp_path / name))

    return _make_history


@pytest.fixture
def make_video_file(tmp_path):
    def _make_video_file(name="test.mp4"):
        f = tmp_path / name
        f.write_text("fake video content")
        return f

    return _make_video_file


@pytest.fixture
def make_video_tree(tmp_path):
    def _make_video_tree(structure):
        for path, codec in structure.items():
            p = tmp_path / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(codec if codec else "fake content")
        return tmp_path

    return _make_video_tree

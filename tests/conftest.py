"""共享 pytest 设施：注册 CLI 选项与 slow 测试跳过钩子，并提供临时目录、
假视频、ffmpeg/ffprobe、QSV 与 VMAF 可用性夹具。

Shared pytest facilities: CLI option registration, slow-test skip hook, and
fixtures for temp dirs, fake videos, ffmpeg/ffprobe, QSV and VMAF availability.
"""

import os
import shutil
import subprocess

import pytest

from src import config
from src.storage.history import History


def pytest_addoption(parser):
    """注册 --run-slow 命令行选项，用于启用标记为 slow 的集成测试。

    Register the --run-slow CLI option that enables slow-marked integration tests.
    """
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="运行标记为 slow 的集成测试（真实 ffmpeg/ffprobe/QSV/VMAF）",
    )


def pytest_collection_modifyitems(config, items):
    """未指定 --run-slow 时，给所有带 slow 关键字的测试打上 skip 标记。

    Mark every slow-keyworded test to skip unless --run-slow is given.
    """
    if config.getoption("--run-slow"):
        return
    skip_slow = pytest.mark.skip(reason="slow 集成测试，需要 --run-slow 启用")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


@pytest.fixture(scope="session")
def ffmpeg_bin():
    """PATH 中的通用 ffmpeg 路径；未找到时跳过集成测试。

    A plain ffmpeg located on PATH; skips integration tests if absent.
    """
    path = shutil.which("ffmpeg")
    if not path:
        pytest.skip("ffmpeg 不在 PATH 中，跳过集成测试")
    return path


@pytest.fixture(scope="session")
def ffprobe_bin():
    """PATH 中的通用 ffprobe 路径；未找到时跳过集成测试。

    A plain ffprobe located on PATH; skips integration tests if absent.
    """
    path = shutil.which("ffprobe")
    if not path:
        pytest.skip("ffprobe 不在 PATH 中，跳过集成测试")
    return path


@pytest.fixture(scope="session")
def transcode_ffmpeg():
    """由 config.FFMPEG_PATH 指定的转码 ffmpeg；不存在时跳过。

    The transcode ffmpeg from config.FFMPEG_PATH; skips if it does not exist.
    """
    path = config.FFMPEG_PATH
    if not os.path.exists(path):
        pytest.skip(f"转码 ffmpeg 不存在: {path}（config.FFMPEG_PATH）")
    return path


@pytest.fixture(scope="session")
def transcode_ffprobe():
    """由 config.FFPROBE_PATH 指定的转码 ffprobe；不存在时跳过。

    The transcode ffprobe from config.FFPROBE_PATH; skips if it does not exist.
    """
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

    优先使用 /opt/ffmpeg-qsv-vmaf 自编译统一版（SETUP.md 1.2）：
    旧版静态构建在 fps+libvmaf 组合下有 scale2ref 断言 bug，可能影响测试。
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
    pytest.skip("未找到带 libvmaf 的 ffmpeg（如 /opt/ffmpeg-qsv-vmaf/bin/ffmpeg）")


@pytest.fixture
def make_history(tmp_path):
    """工厂夹具：在 tmp_path 下新建一个 History（默认文件名 history.db）。

    Factory fixture: create a new History under tmp_path (default name history.db).
    """

    def _make_history(name="history.db"):
        return History(str(tmp_path / name))

    return _make_history


@pytest.fixture
def make_video_file(tmp_path):
    """工厂夹具：在 tmp_path 下写一个假视频文件并返回其路径。

    Factory fixture: write a fake video file under tmp_path and return its path.
    """

    def _make_video_file(name="test.mp4"):
        f = tmp_path / name
        f.write_text("fake video content")
        return f

    return _make_video_file


@pytest.fixture
def make_video_tree(tmp_path):
    """工厂夹具：按 {相对路径: 编解码器} 结构在 tmp_path 下建目录树。

    Factory fixture: build a directory tree under tmp_path from a
    {relative path: codec} structure.
    """

    def _make_video_tree(structure):
        for path, codec in structure.items():
            p = tmp_path / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(codec if codec else "fake content")
        return tmp_path

    return _make_video_tree

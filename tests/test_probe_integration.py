"""Task 14: ffprobe 集成测试 — 对真实合成视频调用 probe 函数（需要 --run-slow）"""

from pathlib import Path

import pytest

from src.probe import get_streams_info, get_video_codec, get_video_pix_fmt

pytestmark = [pytest.mark.integration, pytest.mark.slow]

FIXTURE_DIR = Path(__file__).parent / "fixtures"


class TestGetVideoCodec:
    def test_h264(self, ffprobe_bin):
        assert get_video_codec(FIXTURE_DIR / "synth_320x240_1s_h264.mp4", ffprobe_bin) == "h264"

    def test_hevc(self, ffprobe_bin):
        assert get_video_codec(FIXTURE_DIR / "synth_320x240_1s_hevc.mp4", ffprobe_bin) == "hevc"

    def test_hevc10bit(self, ffprobe_bin):
        path = FIXTURE_DIR / "synth_320x240_1s_hevc10bit.mkv"
        assert get_video_codec(path, ffprobe_bin) == "hevc"

    def test_mpeg4(self, ffprobe_bin):
        assert get_video_codec(FIXTURE_DIR / "synth_320x240_1s_mpeg4.avi", ffprobe_bin) == "mpeg4"

    def test_vp9(self, ffprobe_bin):
        assert get_video_codec(FIXTURE_DIR / "synth_320x240_1s_vp9.webm", ffprobe_bin) == "vp9"

    def test_missing_file_returns_none(self, ffprobe_bin):
        assert get_video_codec(FIXTURE_DIR / "missing.mp4", ffprobe_bin) is None

    def test_non_video_file_returns_none(self, ffprobe_bin, tmp_path):
        f = tmp_path / "not_video.txt"
        f.write_text("hello")
        assert get_video_codec(f, ffprobe_bin) is None


class TestGetVideoPixFmt:
    def test_yuv420p(self, ffprobe_bin):
        path = FIXTURE_DIR / "synth_320x240_1s_h264.mp4"
        assert get_video_pix_fmt(path, ffprobe_bin) == "yuv420p"

    def test_yuv420p10le(self, ffprobe_bin):
        assert (
            get_video_pix_fmt(FIXTURE_DIR / "synth_320x240_1s_hevc10bit.mkv", ffprobe_bin)
            == "yuv420p10le"
        )

    def test_non_video_file_returns_none(self, ffprobe_bin, tmp_path):
        f = tmp_path / "not_video.txt"
        f.write_text("hello")
        assert get_video_pix_fmt(f, ffprobe_bin) is None


class TestGetStreamsInfo:
    def test_duration_and_stream(self, ffprobe_bin):
        info = get_streams_info(FIXTURE_DIR / "synth_320x240_1s_h264.mp4", ffprobe_bin)
        assert info is not None
        assert info["duration"] == pytest.approx(1.0, abs=0.1)
        streams = info["streams"]
        assert len(streams) == 1
        assert streams[0]["codec_name"] == "h264"
        assert streams[0]["codec_type"] == "video"

    def test_audio_stream_present(self, ffprobe_bin):
        info = get_streams_info(FIXTURE_DIR / "synth_320x240_1s_h264_aac.mp4", ffprobe_bin)
        assert info is not None
        assert [s["codec_type"] for s in info["streams"]] == ["video", "audio"]

    def test_non_video_file_returns_none(self, ffprobe_bin, tmp_path):
        f = tmp_path / "not_video.txt"
        f.write_text("hello")
        assert get_streams_info(f, ffprobe_bin) is None

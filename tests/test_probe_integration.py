"""ffprobe 集成测试：对真实合成视频调用 probe 函数（需 --run-slow）。

Integration tests for ffprobe: call probe functions on real synthetic videos (requires --run-slow).
"""

from pathlib import Path

import pytest

from src.media.probe import VideoProbe

pytestmark = [pytest.mark.integration, pytest.mark.slow]

FIXTURE_DIR = Path(__file__).parent / "fixtures"


class TestGetVideoCodec:
    def test_h264(self, ffprobe_bin):
        assert (
            VideoProbe(ffprobe_bin).get_codec(FIXTURE_DIR / "synth_320x240_1s_h264.mp4") == "h264"
        )

    def test_hevc(self, ffprobe_bin):
        assert (
            VideoProbe(ffprobe_bin).get_codec(FIXTURE_DIR / "synth_320x240_1s_hevc.mp4") == "hevc"
        )

    def test_hevc10bit(self, ffprobe_bin):
        path = FIXTURE_DIR / "synth_320x240_1s_hevc10bit.mkv"
        assert VideoProbe(ffprobe_bin).get_codec(path) == "hevc"

    def test_mpeg4(self, ffprobe_bin):
        assert (
            VideoProbe(ffprobe_bin).get_codec(FIXTURE_DIR / "synth_320x240_1s_mpeg4.avi") == "mpeg4"
        )

    def test_vp9(self, ffprobe_bin):
        assert VideoProbe(ffprobe_bin).get_codec(FIXTURE_DIR / "synth_320x240_1s_vp9.webm") == "vp9"

    def test_missing_file_returns_none(self, ffprobe_bin):
        assert VideoProbe(ffprobe_bin).get_codec(FIXTURE_DIR / "missing.mp4") is None

    def test_non_video_file_returns_none(self, ffprobe_bin, tmp_path):
        f = tmp_path / "not_video.txt"
        f.write_text("hello")
        assert VideoProbe(ffprobe_bin).get_codec(f) is None


class TestGetVideoPixFmt:
    def test_yuv420p(self, ffprobe_bin):
        path = FIXTURE_DIR / "synth_320x240_1s_h264.mp4"
        assert VideoProbe(ffprobe_bin).get_pix_fmt(path) == "yuv420p"

    def test_yuv420p10le(self, ffprobe_bin):
        assert (
            VideoProbe(ffprobe_bin).get_pix_fmt(FIXTURE_DIR / "synth_320x240_1s_hevc10bit.mkv")
            == "yuv420p10le"
        )

    def test_non_video_file_returns_none(self, ffprobe_bin, tmp_path):
        f = tmp_path / "not_video.txt"
        f.write_text("hello")
        assert VideoProbe(ffprobe_bin).get_pix_fmt(f) is None


class TestGetStreamsInfo:
    def test_duration_and_stream(self, ffprobe_bin):
        info = VideoProbe(ffprobe_bin).get_streams_info(FIXTURE_DIR / "synth_320x240_1s_h264.mp4")
        assert info is not None
        assert info["duration"] == pytest.approx(1.0, abs=0.1)
        streams = info["streams"]
        assert len(streams) == 1
        assert streams[0]["codec_name"] == "h264"
        assert streams[0]["codec_type"] == "video"

    def test_audio_stream_present(self, ffprobe_bin):
        info = VideoProbe(ffprobe_bin).get_streams_info(
            FIXTURE_DIR / "synth_320x240_1s_h264_aac.mp4"
        )
        assert info is not None
        assert [s["codec_type"] for s in info["streams"]] == ["video", "audio"]

    def test_non_video_file_returns_none(self, ffprobe_bin, tmp_path):
        f = tmp_path / "not_video.txt"
        f.write_text("hello")
        assert VideoProbe(ffprobe_bin).get_streams_info(f) is None

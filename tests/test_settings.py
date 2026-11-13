"""Settings 模型与 build_settings 单元测试。

Unit tests for the Settings dataclasses and build_settings factory.
"""

import dataclasses

import pytest

from src import config
from src.config import build_settings, parse_cli_libraries
from src.domain.settings import (
    VMAF_MODE_ASYNC,
    VMAF_MODE_SYNC,
)

pytestmark = pytest.mark.unit


class TestSettingsFrozen:
    """AC1：frozen=True，集合类型为 frozenset，映射类型修改时抛 TypeError。"""

    def test_settings_frozen(self):
        s = build_settings()
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.paths = None  # type: ignore

    def test_paths_frozen(self):
        s = build_settings()
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.paths.ffmpeg_path = "/other"

    def test_runtime_video_extensions_is_frozenset(self):
        s = build_settings()
        assert isinstance(s.runtime.video_extensions, frozenset)

    def test_runtime_skip_codecs_is_frozenset(self):
        s = build_settings()
        assert isinstance(s.runtime.skip_codecs, frozenset)

    def test_encoding_passthrough_codecs_is_frozenset(self):
        s = build_settings()
        assert isinstance(s.encoding.audio_passthrough_codecs, frozenset)

    def test_encoder_params_is_immutable_mapping(self):
        s = build_settings()
        with pytest.raises(TypeError):
            s.encoding.encoder_params["new_key"] = "x"  # type: ignore

    def test_libraries_is_immutable_mapping(self):
        s = build_settings()
        with pytest.raises(TypeError):
            s.libraries["injected"] = None  # type: ignore


class TestBuildSettingsDefaults:
    """AC2：无 CLI 时 build_settings() 的每个字段等于 config 中对应常量。"""

    def test_paths_defaults(self):
        s = build_settings()
        assert s.paths.ffmpeg_path == config.FFMPEG_PATH
        assert s.paths.ffprobe_path == config.FFPROBE_PATH
        assert s.paths.vmaf_ffmpeg_path == config.VMAF_FFMPEG_PATH
        assert s.paths.vmaf_model_path == config.VMAF_MODEL_PATH
        assert s.paths.log_file == config.LOG_FILE
        assert s.paths.history_file == config.HISTORY_FILE
        assert s.paths.vmaf_log_dir == config.VMAF_LOG_DIR

    def test_runtime_defaults(self):
        s = build_settings()
        assert s.runtime.scan_interval == config.SCAN_INTERVAL
        assert s.runtime.max_workers == config.MAX_WORKERS
        assert s.runtime.codec_timeout == config.CODEC_TIMEOUT
        assert s.runtime.vmaf_timeout == config.VMAF_TIMEOUT
        assert s.runtime.frame_count_timeout == config.FRAME_COUNT_TIMEOUT
        assert s.runtime.file_stable_window == config.FILE_STABLE_WINDOW
        assert s.runtime.temp_prefix == config.TEMP_PREFIX
        assert s.runtime.retry_failed == config.RETRY_FAILED
        assert s.runtime.video_extensions == frozenset(config.VIDEO_EXTENSIONS)

    def test_encoding_defaults(self):
        s = build_settings()
        assert s.encoding.video_encoder == config.VIDEO_ENCODER
        assert s.encoding.video_gop == config.VIDEO_GOP
        assert s.encoding.audio_codec == config.AUDIO_CODEC
        assert s.encoding.audio_bitrate == config.AUDIO_BITRATE
        assert s.encoding.keep_subtitles == config.KEEP_SUBTITLES
        assert s.encoding.keep_attachments == config.KEEP_ATTACHMENTS
        assert s.encoding.sw_decode_fallback_codecs == frozenset(config.SW_DECODE_FALLBACK_CODECS)

    def test_quality_defaults(self):
        s = build_settings()
        assert s.quality.vmaf_mode == config.VMAF_MODE
        assert s.quality.vmaf_n_threads == config.VMAF_N_THREADS
        assert s.quality.vmaf_n_subsample == config.VMAF_N_SUBSAMPLE
        assert s.quality.vmaf_hwaccel == config.VMAF_HWACCEL
        assert s.quality.vmaf_hw_fallback == config.VMAF_HW_FALLBACK
        assert s.quality.vmaf_log_retention_days == config.VMAF_LOG_RETENTION_DAYS
        assert s.quality.keep_source == config.KEEP_SOURCE
        assert s.quality.keep_target == config.KEEP_TARGET
        assert s.quality.vmaf_accept_high == config.VMAF_ACCEPT_HIGH
        assert s.quality.vmaf_accept_low == config.VMAF_ACCEPT_LOW
        assert s.quality.accept_ratio_max == config.ACCEPT_RATIO_MAX
        assert s.quality.accept_ratio_low == config.ACCEPT_RATIO_LOW


class TestBuildSettingsCliOverrides:
    """AC2：每个 CLI 参数各有一条覆盖用例。"""

    def _args(self, **kwargs):
        """构造最小化的 argparse.Namespace。"""
        import argparse

        defaults = dict(
            ffmpeg=None,
            ffprobe=None,
            vmaf_ffmpeg=None,
            vmaf_model=None,
            log=None,
            history=None,
            scan_interval=None,
            max_workers=None,
            codec_timeout=None,
            vmaf_timeout=None,
            encoder=None,
            skip_codecs=[],
            vmaf=None,
            no_vmaf_hwaccel=False,
            no_vmaf_hw_fallback=False,
            keep_source=False,
            keep_target=False,
            vmaf_threads=None,
            post_process_workers=None,
            retry_failed=False,
            libraries=None,
        )
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_ffmpeg_override(self):
        s = build_settings(self._args(ffmpeg="/custom/ffmpeg"))
        assert s.paths.ffmpeg_path == "/custom/ffmpeg"

    def test_ffprobe_override(self):
        s = build_settings(self._args(ffprobe="/custom/ffprobe"))
        assert s.paths.ffprobe_path == "/custom/ffprobe"

    def test_vmaf_ffmpeg_override(self):
        s = build_settings(self._args(vmaf_ffmpeg="/custom/vmaf_ffmpeg"))
        assert s.paths.vmaf_ffmpeg_path == "/custom/vmaf_ffmpeg"

    def test_vmaf_model_override(self):
        s = build_settings(self._args(vmaf_model="/custom/vmaf.json"))
        assert s.paths.vmaf_model_path == "/custom/vmaf.json"

    def test_log_override(self):
        s = build_settings(self._args(log="/custom/app.log"))
        assert s.paths.log_file == "/custom/app.log"

    def test_history_override(self):
        s = build_settings(self._args(history="/custom/history.db"))
        assert s.paths.history_file == "/custom/history.db"

    def test_scan_interval_override(self):
        s = build_settings(self._args(scan_interval=99))
        assert s.runtime.scan_interval == 99

    def test_max_workers_override(self):
        s = build_settings(self._args(max_workers=5))
        assert s.runtime.max_workers == 5

    def test_codec_timeout_override(self):
        s = build_settings(self._args(codec_timeout=9999))
        assert s.runtime.codec_timeout == 9999

    def test_vmaf_timeout_override(self):
        s = build_settings(self._args(vmaf_timeout=1234))
        assert s.runtime.vmaf_timeout == 1234

    def test_encoder_override(self):
        s = build_settings(self._args(encoder="hevc"))
        assert s.encoding.video_encoder == "hevc"

    def test_encoder_added_to_skip_codecs(self):
        s = build_settings(self._args(encoder="hevc"))
        assert "hevc" in s.runtime.skip_codecs

    def test_skip_codecs_override(self):
        s = build_settings(self._args(skip_codecs=["vp9", "h264"]))
        assert "vp9" in s.runtime.skip_codecs
        assert "h264" in s.runtime.skip_codecs

    def test_vmaf_sync_override(self):
        s = build_settings(self._args(vmaf="sync"))
        assert s.quality.vmaf_mode == VMAF_MODE_SYNC

    def test_vmaf_async_override(self):
        s = build_settings(self._args(vmaf="async"))
        assert s.quality.vmaf_mode == VMAF_MODE_ASYNC

    def test_no_vmaf_hwaccel_override(self):
        s = build_settings(self._args(no_vmaf_hwaccel=True))
        assert s.quality.vmaf_hwaccel is False

    def test_no_vmaf_hw_fallback_override(self):
        s = build_settings(self._args(no_vmaf_hw_fallback=True))
        assert s.quality.vmaf_hw_fallback is False

    def test_keep_source_override(self):
        s = build_settings(self._args(keep_source=True))
        assert s.quality.keep_source is True

    def test_keep_target_override(self):
        s = build_settings(self._args(keep_target=True))
        assert s.quality.keep_target is True

    def test_post_process_workers_override(self):
        s = build_settings(self._args(post_process_workers=3))
        assert s.runtime.post_process_workers == 3

    def test_retry_failed_override(self):
        s = build_settings(self._args(retry_failed=True))
        assert s.runtime.retry_failed is True

    def test_vmaf_threads_override(self):
        s = build_settings(self._args(vmaf_threads=4))
        assert s.quality.vmaf_n_threads == 4

    def test_vmaf_threads_zero_is_explicit(self):
        """--vmaf-threads 0 是合法的显式值（不应被 `is not None` 以外的真值判断吞掉）。"""
        s = build_settings(self._args(vmaf_threads=0))
        assert s.quality.vmaf_n_threads == 0

    def test_library_one_part(self):
        s = build_settings(self._args(libraries=["movies=/mnt/movies"]))
        assert "movies" in s.libraries
        assert s.libraries["movies"].input == "/mnt/movies"
        assert s.libraries["movies"].output == "/mnt/movies"
        assert s.libraries["movies"].cache == "/mnt/movies"

    def test_library_two_parts(self):
        s = build_settings(self._args(libraries=["shows=/mnt/in:/mnt/out"]))
        assert s.libraries["shows"].input == "/mnt/in"
        assert s.libraries["shows"].output == "/mnt/out"
        assert s.libraries["shows"].cache == "/mnt/out"

    def test_library_three_parts(self):
        s = build_settings(self._args(libraries=["shows=/mnt/in:/mnt/out:/mnt/cache"]))
        assert s.libraries["shows"].cache == "/mnt/cache"


class TestParseCliLibraries:
    """AC2：--library 的错误用例（错误文本与旧实现逐字一致）。"""

    def test_no_equals_raises(self):
        with pytest.raises(ValueError) as exc:
            parse_cli_libraries(["noeq"])
        assert str(exc.value) == (
            "error: --library must be NAME=PATH, NAME=input:output, "
            "or NAME=input:output:cache, got 'noeq'"
        )

    def test_empty_name_raises(self):
        with pytest.raises(ValueError) as exc:
            parse_cli_libraries(["=/mnt/x"])
        assert (
            str(exc.value)
            == "error: invalid library format '=/mnt/x'; library name cannot be empty"
        )  # noqa: E501

    def test_four_parts_raises(self):
        with pytest.raises(ValueError) as exc:
            parse_cli_libraries(["lib=/a:/b:/c:/d"])
        assert str(exc.value) == (
            "error: invalid library format 'lib=/a:/b:/c:/d'; "
            "use NAME=PATH, NAME=input:output, or NAME=input:output:cache"
        )

    def test_empty_component_raises(self):
        with pytest.raises(ValueError) as exc:
            parse_cli_libraries(["lib=/a::"])
        assert (
            str(exc.value)
            == "error: invalid library format 'lib=/a::'; path components cannot be empty"
        )  # noqa: E501


class TestEndToEnd:
    """端到端：_parse_args → build_settings，确保 argparse dest 名称与 build_settings 映射一致。"""

    def test_parse_args_then_build_settings_encoder(self):
        """通过真实 argparse 解析 --encoder，确保 dest='encoder' 正确流转。"""
        import main as main_mod

        args = main_mod._parse_args(["--encoder", "hevc"])
        s = build_settings(args)
        assert s.encoding.video_encoder == "hevc"
        assert "hevc" in s.runtime.skip_codecs

    def test_parse_args_then_build_settings_vmaf_threads_zero(self):
        """--vmaf-threads 0 经 argparse 解析后，dest='vmaf_threads'=0，
        build_settings 中 `is not None` 判断不应将 0 丢弃。"""
        import main as main_mod

        args = main_mod._parse_args(["--vmaf-threads", "0"])
        s = build_settings(args)
        assert s.quality.vmaf_n_threads == 0

    def test_parse_args_then_build_settings_scan_interval(self):
        """--scan-interval 经 argparse 解析后 dest='scan_interval'，验证命名链路完整。"""
        import main as main_mod

        args = main_mod._parse_args(["--scan-interval", "120"])
        s = build_settings(args)
        assert s.runtime.scan_interval == 120


class TestSkipCodecsMutation:
    """AC3：连续调用 build_settings 不改变 config.SKIP_CODECS。"""

    def test_repeated_calls_do_not_mutate_global(self):
        original = set(config.SKIP_CODECS)
        import argparse

        args = argparse.Namespace(
            ffmpeg=None,
            ffprobe=None,
            vmaf_ffmpeg=None,
            vmaf_model=None,
            log=None,
            history=None,
            scan_interval=None,
            max_workers=None,
            codec_timeout=None,
            vmaf_timeout=None,
            encoder="hevc",
            skip_codecs=[],
            vmaf=None,
            no_vmaf_hwaccel=False,
            no_vmaf_hw_fallback=False,
            keep_source=False,
            keep_target=False,
            vmaf_threads=None,
            post_process_workers=None,
            retry_failed=False,
            libraries=None,
        )
        build_settings(args)
        build_settings(args)
        build_settings(args)
        assert config.SKIP_CODECS == original

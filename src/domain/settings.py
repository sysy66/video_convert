from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

# VMAF 模式常量
VMAF_MODE_OFF = "off"
VMAF_MODE_SYNC = "sync"
VMAF_MODE_ASYNC = "async"


@dataclasses.dataclass(frozen=True)
class PathsSettings:
    ffmpeg_path: str
    ffprobe_path: str
    vmaf_ffmpeg_path: str
    vmaf_model_path: str
    log_file: str
    history_file: str
    vmaf_log_dir: str


@dataclasses.dataclass(frozen=True)
class RuntimeSettings:
    scan_interval: int
    max_workers: int
    post_process_workers: int
    codec_timeout: int
    vmaf_timeout: int
    frame_count_timeout: int
    file_stable_window: int
    temp_prefix: str
    retry_failed: bool
    video_extensions: frozenset[str]
    skip_codecs: frozenset[str]


@dataclasses.dataclass(frozen=True)
class EncodingProfile:
    video_encoder: str
    encoder_params: Mapping[str, Any]
    video_gop: int
    video_look_ahead_depth: int
    audio_codec: str
    audio_bitrate: str
    audio_passthrough_codecs: frozenset[str]
    keep_subtitles: bool
    keep_attachments: bool
    sw_decode_fallback_codecs: frozenset[str]


@dataclasses.dataclass(frozen=True)
class QualityPolicy:
    vmaf_mode: str
    vmaf_n_threads: int
    vmaf_n_subsample: int
    vmaf_hwaccel: bool
    vmaf_hw_fallback: bool
    vmaf_log_retention_days: int
    keep_source: bool
    keep_target: bool
    vmaf_accept_high: float
    vmaf_accept_low: float
    accept_ratio_max: float
    accept_ratio_low: float


@dataclasses.dataclass(frozen=True)
class LibraryProfile:
    name: str
    input: str
    output: str
    cache: str


@dataclasses.dataclass(frozen=True)
class Settings:
    paths: PathsSettings
    runtime: RuntimeSettings
    encoding: EncodingProfile
    quality: QualityPolicy
    libraries: Mapping[str, LibraryProfile]

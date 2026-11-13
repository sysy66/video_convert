"""
全局配置模块。集中管理所有运行参数（路径、阈值、VMAF 选项、库映射等），
并提供 libraries.conf 的加载/解析与模板生成。

Global configuration module. Centralizes runtime parameters (paths, thresholds,
VMAF options, library mappings, etc.) and handles loading/parsing of
libraries.conf as well as template generation.
"""

import os
from types import MappingProxyType

from src.domain.settings import (  # noqa: F401
    VMAF_MODE_ASYNC,
    VMAF_MODE_OFF,
    VMAF_MODE_SYNC,
    EncodingProfile,
    LibraryProfile,
    PathsSettings,
    QualityPolicy,
    RuntimeSettings,
    Settings,
)

# 多库支持：名称 → {input, output, cache} 映射。
# Multi-library support: name → {input, output, cache} mapping.
#
# 库列表从项目根目录的 libraries.conf 读取（每行一个库），无需改代码：
#   # 名称 = 输入[, 输出[, 缓存]]
#   movies = /mnt/movies
#   shows  = /mnt/shows, /mnt/out, /mnt/cache
# 输出省略时默认与输入相同；缓存省略时默认与输出相同；以 # 开头的行是注释。
# Libraries are read from libraries.conf in the project root (one per line),
# so users never edit source. Format: name = input[, output[, cache]].
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBRARIES_CONF_FILE = os.path.join(_PROJECT_ROOT, "libraries.conf")
LIBRARIES_CONF_TEMPLATE = """\
# 视频转码媒体库配置（每行一个库）
# Video transcode library configuration (one library per line)
#
# 格式：名称 = 输入[, 输出[, 缓存]]
# Format: name = input[, output[, cache]]
# 输出省略时默认与输入相同；缓存省略时默认与输出相同。以 # 开头的行是注释。
# output defaults to input when omitted; cache defaults to output when omitted.
# Lines starting with # are comments.
#
# 示例 / Examples:
# movies = /mnt/movies
# series = /mnt/series, /mnt/series_out
# shows  = /mnt/shows, /mnt/out, /mnt/cache
"""


def _normalize_libraries(raw):
    """把外部表示（纯字符串或 {input, output, cache} 字典）统一为完整字段。
    纯字符串表示 input=output=cache；字典中 output 省略时默认与 input 相同，
    cache 省略时默认与 output 相同。

    Normalize external representations (a plain string or an
    {input, output, cache} dict) into a complete field set. A plain string means
    input=output=cache; missing output defaults to input, missing cache defaults
    to output.
    """
    normalized = {}
    for name, value in raw.items():
        if isinstance(value, str):
            normalized[name] = {"input": value, "output": value, "cache": value}
        elif isinstance(value, dict):
            inp = value["input"]
            outp = value.get("output", inp)
            normalized[name] = {
                "input": inp,
                "output": outp,
                "cache": value.get("cache", outp),
            }
        else:
            raise TypeError(f"Invalid LIBRARIES entry for {name}: {value!r}")
    return normalized


def _parse_library_value(name, value):
    """解析单条库定义的值部分，返回 str（仅输入）或 {input, output, cache} 字典。

    Parse the value part of a single library entry, returning either a str (input
    only) or an {input, output, cache} dict.
    """
    parts = [p.strip() for p in value.split(",")]
    if len(parts) not in (1, 2, 3):
        raise ValueError(
            f"Library [{name}]: invalid value {value!r}; "
            "expected PATH, input,output, or input,output,cache"
        )
    if not all(parts):
        raise ValueError(f"Library [{name}]: empty path in {value!r}")
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return {"input": parts[0], "output": parts[1]}
    return {"input": parts[0], "output": parts[1], "cache": parts[2]}


def load_libraries_from_conf(path=None):
    """从配置文件读取库定义，返回原始（未规范化）字典；文件不存在时返回空字典。

    Load library definitions from the config file, returning the raw (unnormalized)
    dict; an empty dict is returned if the file does not exist.
    """
    path = path or LIBRARIES_CONF_FILE
    raw = {}
    if not os.path.exists(path):
        return raw
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, sep, value = line.partition("=")
            name = name.strip()
            value = value.strip()
            if not sep or not name or not value:
                raise ValueError(f"{path}:{lineno}: malformed line {line!r}")
            raw[name] = _parse_library_value(name, value)
    return raw


def ensure_libraries_conf(path=None):
    """若配置文件不存在则生成带示例注释的模板，方便用户"开箱即用"。

    Create a template with sample comments if the config file does not exist, so
    users can get started out of the box.
    """
    path = path or LIBRARIES_CONF_FILE
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(LIBRARIES_CONF_TEMPLATE)
        except OSError:
            pass
    return path


_LIBRARIES_RAW = load_libraries_from_conf()
LIBRARIES = _normalize_libraries(_LIBRARIES_RAW)


def parse_cli_libraries(defs):
    """解析 --library CLI 参数列表，返回 raw 库定义字典。
    出错时抛 ValueError，msg 与原 main.py 中 print(f"error: ...") 的文本逐字一致。
    """
    raw = {}
    for lib_def in defs:
        if "=" not in lib_def:
            raise ValueError(
                f"error: --library must be NAME=PATH, NAME=input:output, or NAME=input:output:cache, got {lib_def!r}"  # noqa: E501
            )
        name, rest = lib_def.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError(
                f"error: invalid library format {lib_def!r}; library name cannot be empty"
            )
        parts = [p.strip() for p in rest.split(":")]
        if len(parts) not in (1, 2, 3):
            raise ValueError(
                f"error: invalid library format {lib_def!r}; use NAME=PATH, NAME=input:output, or NAME=input:output:cache"  # noqa: E501
            )
        if not all(parts):
            raise ValueError(
                f"error: invalid library format {lib_def!r}; path components cannot be empty"
            )
        if len(parts) == 1:
            raw[name] = parts[0]
        elif len(parts) == 2:
            raw[name] = {"input": parts[0], "output": parts[1]}
        else:
            raw[name] = {"input": parts[0], "output": parts[1], "cache": parts[2]}
    return raw


def build_settings(args=None, libraries_raw=None):
    """从默认值 + libraries.conf + CLI 参数构造冻结的 Settings 快照。

    不写任何全局变量。args 为 argparse.Namespace 或 None。
    """
    # --- 路径 ---
    paths = PathsSettings(
        ffmpeg_path=args.ffmpeg if args and args.ffmpeg else FFMPEG_PATH,
        ffprobe_path=args.ffprobe if args and args.ffprobe else FFPROBE_PATH,
        vmaf_ffmpeg_path=args.vmaf_ffmpeg if args and args.vmaf_ffmpeg else VMAF_FFMPEG_PATH,
        vmaf_model_path=args.vmaf_model if args and args.vmaf_model else VMAF_MODEL_PATH,
        log_file=args.log if args and args.log else LOG_FILE,
        history_file=args.history if args and args.history else HISTORY_FILE,
        vmaf_log_dir=VMAF_LOG_DIR,
    )

    # --- 运行时 ---
    encoder = args.encoder if args and args.encoder else VIDEO_ENCODER
    base_skip = set(SKIP_CODECS) | {encoder}
    if args and args.skip_codecs:
        base_skip.update(args.skip_codecs)
    runtime = RuntimeSettings(
        scan_interval=args.scan_interval
        if args and args.scan_interval is not None
        else SCAN_INTERVAL,
        max_workers=args.max_workers if args and args.max_workers is not None else MAX_WORKERS,
        post_process_workers=args.post_process_workers
        if args and args.post_process_workers is not None
        else POST_PROCESS_WORKERS,
        codec_timeout=args.codec_timeout
        if args and args.codec_timeout is not None
        else CODEC_TIMEOUT,
        vmaf_timeout=args.vmaf_timeout if args and args.vmaf_timeout is not None else VMAF_TIMEOUT,
        frame_count_timeout=FRAME_COUNT_TIMEOUT,
        file_stable_window=FILE_STABLE_WINDOW,
        temp_prefix=TEMP_PREFIX,
        retry_failed=args.retry_failed if args and args.retry_failed else RETRY_FAILED,
        video_extensions=frozenset(VIDEO_EXTENSIONS),
        skip_codecs=frozenset(base_skip),
    )

    # --- 编码 ---
    # encoder_params：用 MappingProxyType 递归冻结嵌套 dict
    def _freeze_params(d):
        return MappingProxyType(
            {k: (_freeze_params(v) if isinstance(v, dict) else v) for k, v in d.items()}
        )

    encoding = EncodingProfile(
        video_encoder=encoder,
        encoder_params=_freeze_params(ENCODER_PARAMS),
        video_gop=VIDEO_GOP,
        video_look_ahead_depth=VIDEO_LOOK_AHEAD_DEPTH,
        audio_codec=AUDIO_CODEC,
        audio_bitrate=AUDIO_BITRATE,
        audio_passthrough_codecs=frozenset(AUDIO_PASSTHROUGH_CODECS),
        keep_subtitles=KEEP_SUBTITLES,
        keep_attachments=KEEP_ATTACHMENTS,
        sw_decode_fallback_codecs=frozenset(SW_DECODE_FALLBACK_CODECS),
    )

    # --- 质量策略 ---
    quality = QualityPolicy(
        vmaf_mode=args.vmaf if args and args.vmaf is not None else VMAF_MODE,
        vmaf_n_threads=args.vmaf_threads
        if args and args.vmaf_threads is not None
        else VMAF_N_THREADS,
        vmaf_n_subsample=VMAF_N_SUBSAMPLE,
        vmaf_hwaccel=not args.no_vmaf_hwaccel if args and args.no_vmaf_hwaccel else VMAF_HWACCEL,
        vmaf_hw_fallback=not args.no_vmaf_hw_fallback
        if args and args.no_vmaf_hw_fallback
        else VMAF_HW_FALLBACK,
        vmaf_log_retention_days=VMAF_LOG_RETENTION_DAYS,
        keep_source=args.keep_source if args and args.keep_source else KEEP_SOURCE,
        keep_target=args.keep_target if args and args.keep_target else KEEP_TARGET,
        vmaf_accept_high=VMAF_ACCEPT_HIGH,
        vmaf_accept_low=VMAF_ACCEPT_LOW,
        accept_ratio_max=ACCEPT_RATIO_MAX,
        accept_ratio_low=ACCEPT_RATIO_LOW,
    )

    # --- 库 ---
    if libraries_raw is not None:
        raw = libraries_raw
    elif args and args.libraries:
        raw = parse_cli_libraries(args.libraries)
    else:
        raw = _LIBRARIES_RAW
    normalized = _normalize_libraries(raw)
    lib_profiles = {
        name: LibraryProfile(name=name, input=v["input"], output=v["output"], cache=v["cache"])
        for name, v in normalized.items()
    }

    return Settings(
        paths=paths,
        runtime=runtime,
        encoding=encoding,
        quality=quality,
        libraries=MappingProxyType(lib_profiles),
    )


def apply_legacy_globals(settings):  # TODO(v1.7-settings-05): remove
    """把 Settings 快照回填到旧的全局变量，供尚未迁移的模块使用。

    Backfill legacy module globals from the Settings snapshot for modules
    not yet migrated to consume Settings directly.
    """  # TODO(v1.7-settings-05): remove
    global FFMPEG_PATH, FFPROBE_PATH, VMAF_FFMPEG_PATH, VMAF_MODEL_PATH
    global LOG_FILE, HISTORY_FILE
    global SCAN_INTERVAL, MAX_WORKERS, POST_PROCESS_WORKERS
    global CODEC_TIMEOUT, VMAF_TIMEOUT, RETRY_FAILED, SKIP_CODECS
    global VIDEO_ENCODER, VMAF_MODE, VMAF_N_THREADS
    global VMAF_HWACCEL, VMAF_HW_FALLBACK, KEEP_SOURCE, KEEP_TARGET
    global LIBRARIES, _LIBRARIES_RAW

    p = settings.paths
    FFMPEG_PATH = p.ffmpeg_path
    FFPROBE_PATH = p.ffprobe_path
    VMAF_FFMPEG_PATH = p.vmaf_ffmpeg_path
    VMAF_MODEL_PATH = p.vmaf_model_path
    LOG_FILE = p.log_file
    HISTORY_FILE = p.history_file

    r = settings.runtime
    SCAN_INTERVAL = r.scan_interval
    MAX_WORKERS = r.max_workers
    POST_PROCESS_WORKERS = r.post_process_workers
    CODEC_TIMEOUT = r.codec_timeout
    VMAF_TIMEOUT = r.vmaf_timeout
    RETRY_FAILED = r.retry_failed
    SKIP_CODECS = set(r.skip_codecs)

    e = settings.encoding
    VIDEO_ENCODER = e.video_encoder

    q = settings.quality
    VMAF_MODE = q.vmaf_mode
    VMAF_N_THREADS = q.vmaf_n_threads
    VMAF_HWACCEL = q.vmaf_hwaccel
    VMAF_HW_FALLBACK = q.vmaf_hw_fallback
    KEEP_SOURCE = q.keep_source
    KEEP_TARGET = q.keep_target

    # 库映射回填为旧格式 dict
    LIBRARIES = {
        name: {"input": lp.input, "output": lp.output, "cache": lp.cache}
        for name, lp in settings.libraries.items()
    }
    _LIBRARIES_RAW = {
        name: {"input": lp.input, "output": lp.output, "cache": lp.cache}
        for name, lp in settings.libraries.items()
    }


FFMPEG_PATH = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE_PATH = "/usr/lib/jellyfin-ffmpeg/ffprobe"
# VMAF 打分必须使用新版本 ffmpeg：fps 帧率对齐 + libvmaf 组合在旧构建会崩溃
# （Assertion best_input >= 0 at fftools/ffmpeg_filter.c），
# 自编译统一版 /opt/ffmpeg-qsv-vmaf（git-2026-07-30）无此问题。
# VMAF scoring requires a recent ffmpeg: combining the fps filter with libvmaf
# crashes on older builds (Assertion best_input >= 0 at fftools/ffmpeg_filter.c).
VMAF_FFMPEG_PATH = "/opt/ffmpeg-qsv-vmaf/bin/ffmpeg"
VMAF_MODEL_PATH = "/opt/ffmpeg-qsv-vmaf/models/vmaf_v0.6.1neg.json"

SCAN_INTERVAL = 1200
# 文件稳定窗口：mtime 距今超过该秒数才认为写入完成（防止残缺文件入队转码）。
# File stability window: only submit files whose mtime is older than this (seconds).
FILE_STABLE_WINDOW = 300
MAX_WORKERS = 2
CODEC_TIMEOUT = 18000

TEMP_PREFIX = ".work_"

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".ts",
    ".mts",
    ".m2ts",
    ".wmv",
    ".flv",
    ".webm",
    ".rmvb",
}

SKIP_CODECS = set()

# 编码目标 / Encoding target
# "hevc" → hevc_qsv (HEVC Main10, Intel Iris Xe)
# "av1"  → av1_qsv  (AV1, Intel Arc)
VIDEO_ENCODER = "av1"

# 编码器独立参数 / Per-encoder settings
ENCODER_PARAMS = {
    "hevc": {
        "codec": "hevc_qsv",
        "profile": "main10",
        "quality": 20,
        "preset": "slow",
        "b_frames": 8,
        "extbrc": True,
        "adaptive_i": True,
        "adaptive_b": True,
    },
    "av1": {
        "codec": "av1_qsv",
        "profile": "main",
        "quality": 24,
        "preset": "veryslow",
        "b_frames": 7,
        "extbrc": True,
        "adaptive_i": True,
        "adaptive_b": True,
        "b_strategy": 1,
    },
}

VIDEO_GOP = 240
VIDEO_LOOK_AHEAD_DEPTH = 70

# 音频编码参数
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "256k"
AUDIO_PASSTHROUGH_CODECS = {"aac"}

# 流处理策略
KEEP_SUBTITLES = True
KEEP_ATTACHMENTS = True
# 软解回退编解码器（QSV 硬解崩溃时回退到纯软件解码 + hwupload）。
# 加入的编解码器需与 src/media/ffmpeg_runner.py 的动态解码器名解析保持一致。
# Software decode fallback codecs (fall back to software decode + hwupload when QSV crashes).
SW_DECODE_FALLBACK_CODECS = {"vc1", "mpeg4", "vp9"}

LOG_FILE = "data/app.log"
# 转码历史数据库（SQLite）/ Transcoding history database (SQLite)
HISTORY_FILE = "data/history.db"

# VMAF 采样间隔：每 N 帧只评估 1 帧。全片解码但大幅减少计算量。
# VMAF subsample: evaluate 1 frame every N frames. Covers the whole video with reduced cost.
VMAF_N_SUBSAMPLE = 30

# libvmaf 多线程数：0 = auto（默认，留 1 核给转码），>0 = 显式指定。
# libvmaf v2+ 默认单线程（n_threads=0），多线程可数倍提速但收益随线程数递减（>4 后递减明显）。
# Threads for libvmaf: 0 = auto (default, leave 1 core for transcoding), >0 = explicit.
# libvmaf v2+ defaults to single-threaded; threading gives several-fold speedup but gains
# diminish beyond ~4 threads.
VMAF_N_THREADS = ((os.cpu_count() or 1) + 1) // 2

# VMAF 打分帧序对齐：始终按帧序号逐帧配对（setpts=N/30/TB），与容器 tbr/
# 帧率网格无关；两路帧数不一致时由 framesync 以短流为准截断配对。帧数只做
# 日志诊断（不可得或差异大都只记日志），不会阻断或改变打分结果，也没有
# fps 网格投影兜底（已移除）。转码侧不加任何帧率规整参数（-r 会致 VMAF
# 虚低，-fps_mode cfr 会使产物帧率翻倍），对齐只在打分侧进行。
# VMAF frame-index alignment: frames are always paired by index
# (setpts=N/30/TB), independent of the container tbr/frame-rate grid; framesync
# truncates at the shorter stream when the two frame counts differ. Frame
# counts are logged for diagnostics only (whether missing or divergent) and
# never gate or change the score; there is no fps-grid projection fallback
# (removed). Transcoding adds no frame-rate regularization (-r deflates VMAF,
# -fps_mode cfr doubles the output frame rate); alignment happens only on the
# scoring side.

# 打分前帧数探测（get_frame_count / -count_packets）超时秒数：读包计数不
# 解码，600 秒为最慢网络场景的余量；探测超时只影响日志诊断（ref_frame_count
# 记为 None），不影响是否打分。
# Timeout (seconds) for the pre-scoring frame count probe (get_frame_count /
# -count_packets), which counts demux packets without decoding; 600 s leaves
# headroom for the slowest network case. A timeout here only affects the log
# diagnostic (ref_frame_count becomes None); it never blocks scoring.
FRAME_COUNT_TIMEOUT = 600

# VMAF 逐帧 json 日志目录：即使转码结果被拒绝也持久保留，用于事后审计判定是否可信。
# Directory for per-run VMAF json logs; kept even when the result is rejected, for auditing.
VMAF_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "vmaf_logs")
# VMAF 日志保留天数，超出此期限的逐帧 json 日志将被自动清理。
# VMAF log retention in days; older per-frame json logs are auto-purged.
VMAF_LOG_RETENTION_DAYS = 7
VMAF_TIMEOUT = 36000
# VMAF 开启 QSV 硬件加速解码（HW VMAF 失败时自动回退到纯软件）
# Enable QSV hardware-accelerated decoding for VMAF (falls back to software on failure)
VMAF_HWACCEL = True
VMAF_HW_FALLBACK = True
# VMAF 打分模式（--vmaf off|sync|async）：
#   off   关闭（默认），仅按大小比接受
#   sync  同步：转码线程内联打分，耗时阻塞转码池
#   async 异步：产物先移入输出区保留源，post_pool 内联打分后决定去留
# 默认关闭（VMAF 需通过 --vmaf sync|async 显式启用）。
# VMAF scoring mode (--vmaf off|sync|async):
#   off   disabled (default), accept on ratio check only
#   sync  inline scoring in the transcode thread (blocks the transcode pool)
#   async move output into place keeping the source; the post pool scores it
VMAF_MODE = VMAF_MODE_OFF

# 后处理 worker 线程数（VMAF 打分 + 落盘等，默认 1 即可）。
# Post-process worker threads (VMAF scoring + landing, etc.; 1 is enough by default).
POST_PROCESS_WORKERS = 1

# 转换成功后保留源文件：True=产物复制到输出目录后不删除源文件（常用于验证
# 或临时批量转码）；False=复制后删除源文件。
# Keep the source file after a successful transcode: True=copy the output into
# the output directory without deleting the source (useful for verification or
# batches); False=delete the source after copying.
KEEP_SOURCE = False

# 拒绝时新文件仍落盘到目标库：True=reject（含 ratio 超标）时把转码产物放入输出库，
# 仅记 rejected（源保留，供人工复核被拒文件）；False=拒绝时不落盘，清掉 temp 产物。
# Land the new transcoded file into the target library even on rejection: True=place
# the output into the output library on reject (including ratio-over-max), only
# record rejected (source kept, for manual review of rejected files); False=no
# landing on reject, temp output is removed.
KEEP_TARGET = False

# 质量接受规则阈值 / Quality acceptance thresholds
VMAF_ACCEPT_HIGH = 93  # VMAF ≥ 此值时检查 ACCEPT_RATIO_MAX
VMAF_ACCEPT_LOW = 80  # VMAF < 此值时直接拒绝
ACCEPT_RATIO_MAX = 1.00  # VMAF ≥ VMAF_ACCEPT_HIGH 时允许的最大 ratio
ACCEPT_RATIO_LOW = 0.75  # VMAF 在中间区间时允许的最大 ratio

# 启动时重试历史失败的转码（--retry-failed）。
# Retry previously failed transcodes at startup (--retry-failed).
RETRY_FAILED = False

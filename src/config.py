"""
全局配置模块。集中管理所有运行参数（路径、阈值、VMAF 选项、库映射等），
并提供 libraries.conf 的加载/解析与模板生成。

Global configuration module. Centralizes runtime parameters (paths, thresholds,
VMAF options, library mappings, etc.) and handles loading/parsing of
libraries.conf as well as template generation.
"""

import os

# 多库支持：名称 → {input, output, cache} 映射。
# Multi-library support: name → {input, output, cache} mapping.
#
# 库列表从项目根目录的 libraries.conf 读取（每行一个库），无需改代码：
#   # 名称 = 输入[, 输出[, 缓存]]
#   movies = /mnt/movies
#   shows  = /mnt/shows, /mnt/out, /mnt/cache
# 输出/缓存省略时与输入相同；以 # 开头的行是注释。
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
# 输出/缓存省略时与输入相同；以 # 开头的行是注释。
# output/cache default to input when omitted; lines starting with # are comments.
#
# 示例 / Examples:
# movies = /mnt/movies
# shows  = /mnt/shows, /mnt/out, /mnt/cache
"""


def _normalize_libraries(raw):
    """把两种外部表示（纯字符串或 {input, output, cache} 字典）统一为完整字段。
    纯字符串表示 input=output=cache；字典缺失的字段默认与 input 相同。

    Normalize both external representations (a plain string or an
    {input, output, cache} dict) into a complete field set. A plain string means
    input=output=cache; missing dict fields default to the input value.
    """
    normalized = {}
    for name, value in raw.items():
        if isinstance(value, str):
            normalized[name] = {"input": value, "output": value, "cache": value}
        elif isinstance(value, dict):
            inp = value["input"]
            normalized[name] = {
                "input": inp,
                "output": value.get("output", inp),
                "cache": value.get("cache", inp),
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
    if len(parts) == 1:
        if not parts[0]:
            raise ValueError(f"Library [{name}]: empty path")
        return parts[0]
    if len(parts) == 3 and all(parts):
        return {"input": parts[0], "output": parts[1], "cache": parts[2]}
    raise ValueError(
        f"Library [{name}]: invalid value {value!r}; expected PATH or input,output,cache"
    )


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
# 加入的编解码器需与 src/ffmpeg_runner.py 的动态解码器名解析保持一致。
# Software decode fallback codecs (fall back to software decode + hwupload when QSV crashes).
SW_DECODE_FALLBACK_CODECS = {"vc1", "mpeg4", "vp9"}

LOG_FILE = "data/app.log"
# 转码历史数据库（SQLite）/ Transcoding history database (SQLite)
HISTORY_FILE = "data/history.db"

# VMAF 采样间隔：每 N 帧只评估 1 帧。全片解码但大幅减少计算量。
# VMAF subsample: evaluate 1 frame every N frames. Covers the whole video with reduced cost.
VMAF_SUBSAMPLE = 30

# libvmaf 多线程数：0 = auto（默认，留 1 核给转码），>0 = 显式指定。
# libvmaf v2+ 默认单线程（n_threads=0），多线程可数倍提速但收益随线程数递减（>4 后递减明显）。
# Threads for libvmaf: 0 = auto (default, leave 1 core for transcoding), >0 = explicit.
# libvmaf v2+ defaults to single-threaded; threading gives several-fold speedup but gains
# diminish beyond ~4 threads.
VMAF_N_THREADS = 0

# VMAF 打分帧率对齐总开关。打分侧对齐：优先"帧序对齐"（两路帧数相等则按帧序号
# 逐帧配对，与容器 tbr/帧率网格无关，VFR 源不再假低分）；仅当两路帧数不相等
# （或帧数探测失败）时才回退到 fps 网格投影，目标帧率按三级链取
# detect_vmaf_align_rate 的返回值（帧间隔投票帧率 → 规范化 avg_frame_rate →
# r_frame_rate），全缺失时用 VMAF_ALIGN_FPS_RATE 兜底。转码侧不产生任何帧率
# 规整参数（实验证明 -r/-fps_mode cfr 分别致 VMAF 虚低/产物双倍帧），对齐只在
# 打分侧进行。实测（2026-08-13 chn-127 全片）：帧序对齐 92.09 vs fps=30/1
# 投影 62.37。
# With VMAF_ALIGN_FPS=True scoring prefers frame-index alignment (when both
# inputs have equal frame counts frames are paired by index, independent of the
# container tbr/frame-rate grid, so VFR sources are no longer falsely deflated);
# fps-grid projection is used only when the counts differ or probing fails, its
# target rate comes from detect_vmaf_align_rate's three-level chain (voted
# PTS-interval rate -> normalized avg_frame_rate -> r_frame_rate), falling back
# to VMAF_ALIGN_FPS_RATE when nothing is available. Transcoding adds no
# frame-rate regularization (experiments showed -r deflates scores and
# -fps_mode cfr doubles output frames); alignment happens on the scoring side
# only. Measured (2026-08-13 chn-127 full movie): frame-index 92.09 vs
# fps=30/1 projection 62.37.
VMAF_ALIGN_FPS = True
VMAF_ALIGN_FPS_RATE = "30000/1001"

# 打分前帧数探测（get_frame_count / -count_frames）超时秒数：全量解码计数，
# 超长影片（3+ 小时）也可能需要数分钟，探测失败会回退 fps 网格投影。
# Timeout (seconds) for the pre-scoring frame count probe (get_frame_count /
# -count_frames), which decodes the whole input; very long movies (3+ hours)
# may take minutes, and a probe failure falls back to fps-grid projection.
FRAME_COUNT_TIMEOUT = 3600

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
# VMAF 是否为转码的硬性要求：True=不可用时拒绝，False=不可用时仍接受（仅按大小比判断）。
# 默认关闭（VMAF 需通过 --vmaf sync|async 显式启用）。
# Whether VMAF is required: True=reject if unavailable, False=accept anyway (ratio check only).
# Disabled by default; enable via --vmaf sync|async.
VMAF_REQUIRED = False

# 异步 VMAF 打分：True=转码成功后先保留源文件并移到输出区，后台线程打分后决定去留。
# 打分 worker 线程数（VMAF 极耗解码器资源，默认 1 即可）。
# Async VMAF scoring: True=move output into place without deleting the source; a background
# worker scores VMAF later and keeps exactly one of the two files.
VMAF_ASYNC = False
VMAF_WORKERS = 1

# 转换成功后保留源文件：True=不删除原文件，仅移入输出（常用于验证或临时批量转码）。
# Keep the source file after a successful transcode: True=do not delete the original,
# only place the output into the output directory (useful for verification or batches).
KEEP_SOURCE = False

# 质量接受规则阈值 / Quality acceptance thresholds
# 极端低分阈值：VMAF 低于此值视为输入/产物异常（如写入中残缺文件），记 failed
# 永久排除，仅 --retry-failed 启动时手动重试。
# Extreme-low threshold: VMAF below this suggests an incomplete input/output; mark
# failed (permanently excluded, retried only via --retry-failed).
VMAF_EXTREME_LOW = 60
VMAF_ACCEPT_HIGH = 93  # VMAF ≥ 此值时检查 ACCEPT_RATIO_MAX
VMAF_ACCEPT_LOW = 80  # VMAF < 此值时直接拒绝
ACCEPT_RATIO_MAX = 1.00  # VMAF ≥ VMAF_ACCEPT_HIGH 时允许的最大 ratio
ACCEPT_RATIO_LOW = 0.75  # VMAF 在中间区间时允许的最大 ratio

# 启动时重试历史失败的转码（--retry-failed）。
# Retry previously failed transcodes at startup (--retry-failed).
RETRY_FAILED = False

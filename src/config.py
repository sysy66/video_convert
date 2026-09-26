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
VMAF_MODE_OFF = "off"
VMAF_MODE_SYNC = "sync"
VMAF_MODE_ASYNC = "async"
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
# 当前未被任何代码路径读取（v1.6 删除旧异步 worker 链时一并失去了调用方），
# 保留在此仅为兼容旧配置引用；如需恢复"极端低分记 failed"的行为需要重新接入。
# Currently unread by any code path (its caller was removed along with the old
# async worker chain in v1.6); kept only for backward-compat references. Re-wiring
# is needed to restore the "extreme-low score marks failed" behavior.
VMAF_EXTREME_LOW = 60
VMAF_ACCEPT_HIGH = 93  # VMAF ≥ 此值时检查 ACCEPT_RATIO_MAX
VMAF_ACCEPT_LOW = 80  # VMAF < 此值时直接拒绝
ACCEPT_RATIO_MAX = 1.00  # VMAF ≥ VMAF_ACCEPT_HIGH 时允许的最大 ratio
ACCEPT_RATIO_LOW = 0.75  # VMAF 在中间区间时允许的最大 ratio

# 启动时重试历史失败的转码（--retry-failed）。
# Retry previously failed transcodes at startup (--retry-failed).
RETRY_FAILED = False

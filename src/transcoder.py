import os
import re
import shutil
import string
import random
import subprocess
import threading
from pathlib import Path

from src import config
from src.probe import get_streams_info

log = __import__("logging").getLogger(__name__)


def _random_suffix(length=6):
    """
    生成随机后缀，用于临时目录名称。
    Generate a random suffix for temporary directory names.

    :param length: 后缀长度 (Length of the suffix)
    :return: 随机字符串 (Random string)
    """
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=length))


def _build_ffmpeg_cmd(ffmpeg_path, ffprobe_path, input_path, output_path):
    """
    构建 FFmpeg 命令行参数列表。
    通过 ffprobe 探测输入文件流信息，动态选择视频/音频/字幕/附件流，
    并根据配置决定编码参数（如音频直通、字幕保留等）。

    Build the FFmpeg command-line argument list.
    Probe the input file with ffprobe to dynamically select video/audio/subtitle/attachment
    streams, and decide encoding parameters according to configuration
    (e.g., audio passthrough, subtitle retention).

    :param ffmpeg_path: FFmpeg 可执行文件路径 (Path to the FFmpeg executable)
    :param ffprobe_path: ffprobe 可执行文件路径 (Path to the ffprobe executable)
    :param input_path: 输入视频文件路径 (Path to the input video file)
    :param output_path: 输出视频文件路径 (Path to the output video file)
    :return: (cmd_list, total_duration) — 命令列表和视频总时长（秒）
             (cmd_list, total_duration) where duration is in seconds, or None if unknown
    :raises RuntimeError: 当流探测失败时抛出 (Raised when stream probing fails)
    """
    info = get_streams_info(input_path, ffprobe_path)
    if info is None:
        raise RuntimeError(f"Failed to probe streams for {input_path}")

    streams = info["streams"]
    duration = info["duration"]

    # 收集所有音频流的编码格式，用于判断是否需要直通
    # Collect all audio codecs to decide whether passthrough is possible
    audio_codecs = set()
    for s in streams:
        if s.get("codec_type") == "audio":
            codec = s.get("codec_name", "").lower()
            if codec:
                audio_codecs.add(codec)

    cmd = [
        # 基础参数 / Basic parameters
        ffmpeg_path, "-y",
        # Intel QSV 硬件加速 / Intel QSV hardware acceleration
        "-hwaccel", "qsv", "-hwaccel_output_format", "qsv",
        # 输入文件 / Input file
        "-i", str(input_path),
        # --- 视频流 / Video stream ---
        # 选择第一个视频流（按类型，而非按索引），避免流顺序问题
        # Select the first video stream by type (not index) to avoid stream ordering issues
        "-map", "0:v:0", "-c:v", "hevc_qsv", "-profile:v", "main10",
        "-global_quality", str(config.VIDEO_QUALITY),
        "-preset", config.VIDEO_PRESET,
        # 高级 QSV 优化 / Advanced QSV optimisation
        "-look_ahead", "1", "-look_ahead_depth", str(config.VIDEO_LOOK_AHEAD_DEPTH),
        "-g", str(config.VIDEO_GOP), "-bf", str(config.VIDEO_B_FRAMES),
        "-extbrc", "1",
        "-adaptive_i", "1", "-adaptive_b", "1",
        # 像素格式转换 + 恒定帧率 / Pixel format conversion + constant frame rate
        "-vf", "vpp_qsv=format=p010le", "-fps_mode", "cfr",
        # --- 音频流 / Audio stream ---
        # 选择所有音频流（'?' 表示无音频时不报错）
        # Select all audio streams ('?' means don't fail if none exist)
        "-map", "0:a?",
    ]

    # 音频编码策略：若源全部为 AAC 等直通格式则直接复制，否则重新编码
    # Audio encoding strategy: copy if all source streams are passthrough codecs, else re-encode
    if audio_codecs and audio_codecs.issubset(config.AUDIO_PASSTHROUGH_CODECS):
        cmd.extend(["-c:a", "copy"])
    else:
        cmd.extend(["-c:a", config.AUDIO_CODEC, "-b:a", config.AUDIO_BITRATE])

    # --- 字幕流 / Subtitle stream ---
    # 直通复制所有字幕流（受配置 KEEP_SUBTITLES 控制）
    # Passthrough all subtitle streams (controlled by config KEEP_SUBTITLES)
    if config.KEEP_SUBTITLES:
        cmd.extend(["-map", "0:s?", "-c:s", "copy"])

    # --- 附件流 / Attachment stream ---
    # 直通复制附件（如字体文件），受配置 KEEP_ATTACHMENTS 控制
    # Passthrough attachments (e.g. fonts), controlled by config KEEP_ATTACHMENTS
    if config.KEEP_ATTACHMENTS:
        cmd.extend(["-map", "0:t?", "-c:t", "copy"])

    cmd.append(str(output_path))
    return cmd, duration


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")


def _parse_progress_time(line):
    """
    从 FFmpeg 的 stderr 输出行中解析当前的编码时间（秒）。
    Parse the current encoding time (in seconds) from an FFmpeg stderr line.

    :param line: FFmpeg 输出的文本行 (A line of FFmpeg output)
    :return: 秒数 (Seconds as float), or None if parsing fails
    """
    m = _TIME_RE.search(line)
    if m:
        h, min_, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return h * 3600 + min_ * 60 + s
    return None


def _run_ffmpeg_with_progress(cmd, total_duration, file_stem):
    """
    运行 FFmpeg 命令并实时解析进度信息。
    在后台线程中读取 stderr，每当进度前进 >=10% 时记录日志。

    Run an FFmpeg command and parse progress information in real time.
    A background thread reads stderr and logs a message every time
    progress advances by >=10%.

    :param cmd: FFmpeg 命令行参数列表 (FFmpeg command-line argument list)
    :param total_duration: 视频总时长（秒），用于计算百分比；为 None 时不计算
                           Total duration in seconds for percentage calculation; skipped if None
    :param file_stem: 文件名（不含扩展名），用于日志标识
                      File name without extension, used in log messages
    :return: (returncode, stderr_text) — 退出码和完整错误输出
             (returncode, full_stderr_text)
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stderr_lines = []
    last_logged_pct = -1
    reader_done = threading.Event()

    def _reader():
        for line in iter(proc.stderr.readline, ""):
            stderr_lines.append(line)
            if total_duration:
                secs = _parse_progress_time(line)
                if secs is not None and secs > 0:
                    pct = int(secs / total_duration * 100)
                    if pct >= last_logged_pct + 10:
                        log.info("PROGRESS: %s - %d%%", file_stem, min(pct, 100))
                        last_logged_pct = pct
        reader_done.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    proc.wait()
    reader_done.wait(timeout=10)

    return proc.returncode, "".join(stderr_lines)


def transcode(filepath, history, ffmpeg_path, ffprobe_path):
    """
    对单个视频文件执行转码的完整流程：
      1. 创建临时工作目录
      2. 构建 FFmpeg 命令并执行（含实时进度日志）
      3. 成功时用输出文件替换原文件
      4. 失败/异常时清理临时目录并标记历史

    Complete transcoding workflow for a single video file:
      1. Create a temporary working directory
      2. Build and run the FFmpeg command (with real-time progress logging)
      3. On success, replace the original file with the output
      4. On failure/exception, clean up the temp directory and mark history

    :param filepath: 源文件路径 (Path to the source video file)
    :param history: 历史记录对象 (History tracking object)
    :param ffmpeg_path: FFmpeg 可执行文件路径 (Path to the FFmpeg executable)
    :param ffprobe_path: ffprobe 可执行文件路径 (Path to the ffprobe executable)
    :return: True 表示成功 (True on success), False 表示失败 (False on failure)
    """
    filepath = Path(filepath)
    parent_dir = filepath.parent
    stem = filepath.stem
    suffix = _random_suffix()
    temp_dir_name = f"{config.TEMP_PREFIX}{suffix}"
    temp_dir = parent_dir / temp_dir_name

    output_filename = f"{stem}.mkv"
    output_path = temp_dir / output_filename

    history.mark_started(str(filepath))

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        log.info("TRANSCODE: %s -> %s (temp: %s)", filepath.name, output_filename, temp_dir_name)

        cmd, total_duration = _build_ffmpeg_cmd(ffmpeg_path, ffprobe_path, filepath, output_path)
        log.debug("FFmpeg command: %s", " ".join(str(c) for c in cmd))

        returncode, stderr = _run_ffmpeg_with_progress(cmd, total_duration, filepath.name)

        if returncode != 0:
            error_msg = stderr.strip() or f"exit code {returncode}"
            log.error("TRANSCODE FAILED: %s - %s", filepath.name, error_msg)
            history.mark_failed(str(filepath), error_msg)
            _cleanup_temp(temp_dir)
            return False

        if not output_path.exists():
            log.error("TRANSCODE FAILED: output not found for %s", filepath.name)
            history.mark_failed(str(filepath), "Output file not found after transcode")
            _cleanup_temp(temp_dir)
            return False

        log.info("REPLACE: %s -> %s", filepath.name, output_filename)
        filepath.unlink()
        shutil.move(str(output_path), str(parent_dir / output_filename))
        _cleanup_temp(temp_dir)

        history.mark_done(str(filepath), str(parent_dir / output_filename))
        log.info("DONE: %s", output_filename)
        return True

    except subprocess.TimeoutExpired:
        log.error("TRANSCODE TIMEOUT: %s", filepath.name)
        history.mark_failed(str(filepath), "Transcode timed out")
        _cleanup_temp(temp_dir)
        return False
    except Exception as e:
        log.error("TRANSCODE ERROR: %s - %s", filepath.name, e)
        history.mark_failed(str(filepath), str(e))
        _cleanup_temp(temp_dir)
        return False


def _cleanup_temp(temp_dir):
    """
    安全地删除临时工作目录（忽略不存在的情况）。
    Safely remove a temporary working directory (no-op if it does not exist).

    :param temp_dir: 临时目录的 Path 对象 (Path object of the temp directory)
    """
    try:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
            log.debug("Cleaned up temp dir: %s", temp_dir)
    except OSError as e:
        log.warning("Failed to clean up temp dir %s: %s", temp_dir, e)


def cleanup_stale_temp_dirs(watch_dir, temp_prefix):
    """
    递归扫描监视目录，清理所有残留的临时工作目录（通常因程序崩溃未清理）。
    Recursively scan the watch directory and remove any stale temporary
    working directories (usually left behind by crashes).

    :param watch_dir: 监视目录的根路径 (Root path of the watched directory)
    :param temp_prefix: 临时目录的前缀 (Prefix of temporary directory names)
    """
    count = 0
    for root, dirs, _ in os.walk(watch_dir):
        for d in list(dirs):
            if d.startswith(temp_prefix):
                full_path = Path(root) / d
                try:
                    shutil.rmtree(full_path)
                    count += 1
                    log.info("Removed stale temp dir: %s", full_path)
                except OSError as e:
                    log.warning("Failed to remove stale temp dir %s: %s", full_path, e)
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith(temp_prefix)]
    if count:
        log.info("Cleaned up %d stale temp director%s", count, "ies" if count != 1 else "y")




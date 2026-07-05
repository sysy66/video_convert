import os
import re
import shutil
import signal
import string
import random
import subprocess
import threading
import time as _time
from pathlib import Path

from src import config
from src.probe import get_streams_info
from src.quality import compute_vmaf, should_accept

log = __import__("logging").getLogger(__name__)

# 追踪所有活跃的 ffmpeg 子进程 PID，用于优雅关闭时清理孤儿进程
# Track all active ffmpeg child process PIDs for orphan cleanup on graceful shutdown
_active_ffmpeg_pids = set()
_active_ffmpeg_lock = threading.Lock()

# 追踪当前正在转码的文件路径，用于避免将本进程的 started 条目误判为残留
# Track filepaths currently being transcoded to avoid falsely flagging them as stale
_active_transcodes = set()
_active_transcodes_lock = threading.Lock()


def register_ffmpeg_pid(pid):
    """注册 ffmpeg 子进程 PID。Register an ffmpeg child process PID."""
    with _active_ffmpeg_lock:
        _active_ffmpeg_pids.add(pid)


def unregister_ffmpeg_pid(pid):
    """注销 ffmpeg 子进程 PID。Unregister an ffmpeg child process PID."""
    with _active_ffmpeg_lock:
        _active_ffmpeg_pids.discard(pid)


def kill_active_ffmpeg():
    """
    终止所有活跃的 ffmpeg 子进程（关闭流程调用，防止孤儿进程残留）。
    先 SIGTERM，2 秒后仍未退出则 SIGKILL，应对 NFS D-state 阻塞场景。

    Terminate all active ffmpeg child processes (called during shutdown to
    prevent orphan processes from lingering). Sends SIGTERM first, then
    SIGKILL after 2 seconds for stuck (D-state) processes.
    """
    with _active_ffmpeg_lock:
        pids = list(_active_ffmpeg_pids)
    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            log.info("Sent SIGTERM to ffmpeg PID %d", pid)
        except ProcessLookupError:
            pass
        except OSError as e:
            log.warning("Failed to kill ffmpeg PID %d: %s", pid, e)
    _time.sleep(2)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            log.info("Sent SIGKILL to ffmpeg PID %d", pid)
        except ProcessLookupError:
            pass
        except OSError as e:
            log.warning("Failed to SIGKILL ffmpeg PID %d: %s", pid, e)


def mark_stale_started_as_failed(history):
    """
    将 history 中所有 status == "started" 但本进程未在转码的条目标记为 failed。
    用于启动 / 每个扫描周期前清理停电残留，避免 stuck started 被 FULL SCAN 跳过。

    Mark all history entries with status "started" that are NOT currently being
    transcoded by this process as "failed". Handles stale entries left by power
    outages so they get retried on the next FULL SCAN.

    :param history: 历史记录对象 (History object)
    """
    count = 0
    for fp in history.iter_started():
        with _active_transcodes_lock:
            if fp in _active_transcodes:
                continue
        history.mark_failed(fp, "Stale: process interrupted, will retry")
        count += 1
    if count:
        log.info("Marked %d stale 'started' entr%s as failed", count, "y" if count == 1 else "ies")


def _unique_path(path):
    """
    如果路径已存在，自动追加编号（如 foo.mkv → foo.1.mkv），防止覆盖。
    If the path already exists, append a counter (e.g. foo.mkv → foo.1.mkv)
    to prevent overwriting.

    :param path: 期望的路径 (Desired path)
    :return: 不重复的路径 (A non-conflicting path)
    """
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        new = parent / f"{stem}.{counter}{suffix}"
        if not new.exists():
            return new
        counter += 1


def _format_size(bytes_val):
    """
    将字节数格式化为人类可读的字符串（KB/MB/GB）。
    Format bytes into a human-readable string (KB/MB/GB).

    :param bytes_val: 字节数 (Number of bytes)
    :return: 格式化后的字符串，如 "1.23 GB" (Formatted string, e.g. "1.23 GB")
    """
    for unit in ("B", "KB", "MB", "GB"):
        if abs(bytes_val) < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def _random_suffix(length=6):
    """
    生成随机后缀，用于临时目录名称。
    Generate a random suffix for temporary directory names.

    :param length: 后缀长度 (Length of the suffix)
    :return: 随机字符串 (Random string)
    """
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=length))


def _build_ffmpeg_cmd(ffmpeg_path, ffprobe_path, input_path, output_path, sw_fallback=False):
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
    :param sw_fallback: 是否使用软件缩放回退（绕开 vpp_qsv 滤镜在 hwaccel 切换时的兼容性问题）
                        Whether to use software-scale fallback (bypass vpp_qsv filter
                        compatibility issues during hwaccel reconfiguration)
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
        "-hwaccel", "qsv",
    ]

    if sw_fallback:
        # 软件缩放回退：帧保持在系统内存，用软件 format 转换后 hwupload 上传
        # Software-scale fallback: keep frames in system memory, convert with
        # software format filter, then upload to QSV via hwupload
        cmd.extend(["-hwaccel_output_format", "none"])
    else:
        cmd.extend(["-hwaccel_output_format", "qsv"])

    cmd.extend([
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
        "-fps_mode", "cfr",
        # --- 音频流 / Audio stream ---
        # 选择所有音频流（'?' 表示无音频时不报错）
        # Select all audio streams ('?' means don't fail if none exist)
        "-map", "0:a?",
    ])

    if sw_fallback:
        cmd.extend(["-vf", "format=p010le,hwupload=extra_hw_frames=64"])
    else:
        cmd.extend(["-vf", "vpp_qsv=format=p010le"])

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

    register_ffmpeg_pid(proc.pid)

    stderr_lines = []
    last_logged_pct = -1
    reader_done = threading.Event()

    def _reader():
        nonlocal last_logged_pct
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
    unregister_ffmpeg_pid(proc.pid)
    reader_done.wait(timeout=10)

    return proc.returncode, "".join(stderr_lines)


def transcode(filepath, history, ffmpeg_path, ffprobe_path, output_dir=None, cache_dir=None):
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
    :param output_dir: 输出目录，不设置则同源目录 (Output directory, defaults to source parent)
    :param cache_dir: 缓存（临时）目录，不设置则同源目录 (Cache/temp directory, defaults to source parent)
    :return: True 表示成功 (True on success), False 表示失败 (False on failure)
    """
    filepath = Path(filepath)
    stem = filepath.stem
    suffix = _random_suffix()
    if cache_dir:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = cache_dir / f"{config.TEMP_PREFIX}{suffix}"
    else:
        temp_dir = filepath.parent / f"{config.TEMP_PREFIX}{suffix}"
    start_time = _time.monotonic()
    input_size = filepath.stat().st_size

    output_filename = f"{stem}.mkv"
    output_path = temp_dir / output_filename

    history.mark_started(str(filepath))
    with _active_transcodes_lock:
        _active_transcodes.add(str(filepath))

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "TRANSCODE: %s (%s) -> %s (temp: %s)",
            filepath.name, _format_size(input_size), output_filename, temp_dir.name,
        )

        cmd, total_duration = _build_ffmpeg_cmd(ffmpeg_path, ffprobe_path, filepath, output_path, sw_fallback=False)
        log.debug("FFMPEG CMD: %s %s", filepath.name, " ".join(str(c) for c in cmd))

        returncode, stderr = _run_ffmpeg_with_progress(cmd, total_duration, filepath.name)

        # 第一次运行（QSV）stderr 存入 .ffmpeg.log.qsv
        try:
            (temp_dir / ".ffmpeg.log.qsv").write_text(stderr, encoding="utf-8")
        except OSError:
            pass

        # QSV filter reinit 失败则立即用软件缩放重试（新建临时目录避免冲突）
        if returncode != 0 and "Error reinitializing filters" in stderr:
            log.warning("QSV SW FALLBACK: %s - re-trying with software scale", filepath.name)
            _cleanup_temp(temp_dir)
            suffix = _random_suffix()
            if cache_dir:
                temp_dir = Path(cache_dir) / f"{config.TEMP_PREFIX}{suffix}"
            else:
                temp_dir = filepath.parent / f"{config.TEMP_PREFIX}{suffix}"
            temp_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_dir / output_filename
            cmd, _ = _build_ffmpeg_cmd(ffmpeg_path, ffprobe_path, filepath, output_path, sw_fallback=True)
            log.debug("FFMPEG CMD (sw fallback): %s ...", filepath.name)
            returncode, stderr = _run_ffmpeg_with_progress(cmd, total_duration, filepath.name)

        # 最终 stderr 写入 .ffmpeg.log
        try:
            (temp_dir / ".ffmpeg.log").write_text(stderr, encoding="utf-8")
        except OSError:
            pass

        if returncode != 0:
            error_msg = stderr.strip() or f"exit code {returncode}"
            elapsed = _time.monotonic() - start_time
            log.error("TRANSCODE FAILED: %s (%.1fs) - %s", filepath.name, elapsed, error_msg)
            history.mark_failed(str(filepath), error_msg)
            _cleanup_temp(temp_dir)
            return False

        if not output_path.exists():
            elapsed = _time.monotonic() - start_time
            log.error("TRANSCODE FAILED: output not found for %s (%.1fs)", filepath.name, elapsed)
            history.mark_failed(str(filepath), "Output file not found after transcode")
            _cleanup_temp(temp_dir)
            return False

        # 在移动文件前计算 VMAF（输出仍在临时目录，原文件尚存在）
        # Compute VMAF before moving the file (output still in temp, original still exists)
        vmaf_score = None
        if config.VMAF_ENABLED:
            vmaf_score = compute_vmaf(filepath, output_path)

        output_size = output_path.stat().st_size
        ratio = output_size / input_size if input_size else 0
        elapsed = _time.monotonic() - start_time

        if should_accept(vmaf_score, ratio):
            if output_dir:
                output_dir = Path(output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)
                final_path = _unique_path(output_dir / output_filename)
            else:
                final_path = _unique_path(filepath.parent / output_filename)
            if final_path.name != output_filename:
                log.info("DEST EXISTS: using %s instead of %s", final_path.name, output_filename)
            shutil.move(str(output_path), str(final_path))
            _cleanup_temp(temp_dir)
            filepath.unlink()
            history.mark_done(str(filepath), str(final_path), vmaf_score=vmaf_score, rejected=False)
            if vmaf_score is not None:
                log.info(
                    "ACCEPT: %s (%s, %s, %.1f%%, VMAF %.2f, %.1fs)",
                    final_path.name, _format_size(output_size),
                    "smaller" if ratio < 1 else "larger",
                    ratio * 100, vmaf_score, elapsed,
                )
            else:
                log.info(
                    "ACCEPT: %s (%s, %s, %.1f%%, %.1fs)",
                    final_path.name, _format_size(output_size),
                    "smaller" if ratio < 1 else "larger",
                    ratio * 100, elapsed,
                )
        else:
            _cleanup_temp(temp_dir)
            history.mark_done(str(filepath), None, vmaf_score=vmaf_score, rejected=True)
            log.warning(
                "REJECT: %s (VMAF %s, ratio %.1f%%, keeping original, %.1fs)",
                filepath.name,
                "{:.2f}".format(vmaf_score) if vmaf_score is not None else "N/A",
                ratio * 100, elapsed,
            )
        return True

    except subprocess.TimeoutExpired:
        elapsed = _time.monotonic() - start_time
        log.error("TRANSCODE TIMEOUT: %s (%.1fs)", filepath.name, elapsed)
        history.mark_failed(str(filepath), "Transcode timed out")
        _cleanup_temp(temp_dir)
        return False
    except Exception as e:
        elapsed = _time.monotonic() - start_time
        log.error("TRANSCODE ERROR: %s (%.1fs) - %s", filepath.name, elapsed, e)
        history.mark_failed(str(filepath), str(e))
        _cleanup_temp(temp_dir)
        return False
    finally:
        with _active_transcodes_lock:
            _active_transcodes.discard(str(filepath))


def _cleanup_temp(temp_dir):
    """
    安全地删除临时工作目录（忽略不存在的情况）。
    Safely remove a temporary working directory (no-op if it does not exist).

    :param temp_dir: 临时目录的 Path 对象 (Path object of the temp directory)
    """
    try:
        if temp_dir.exists() and not _rmtree_robust(temp_dir):
            log.warning("Failed to clean up temp dir %s completely", temp_dir)
        else:
            log.debug("Cleaned up temp dir: %s", temp_dir)
    except OSError as e:
        log.warning("Failed to clean up temp dir %s: %s", temp_dir, e)


def _rmtree_robust(path):
    """
    尝试多种方式删除目录树，比 shutil.rmtree 更健壮（处理 NFS .nfs* 残留等情形）。
    Try multiple strategies to remove a directory tree, more robust than plain
    shutil.rmtree (handles NFS .nfs* leftovers, etc.).

    :param path: 要删除的目录路径 (Path to the directory to remove)
    :return: True 表示最终删除成功 (True if eventually successful)
    """
    # 策略 1：shutil.rmtree
    # Strategy 1: shutil.rmtree
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        pass

    # 策略 2：先逐个删除目录内文件，再重试 rmtree
    # Strategy 2: remove individual files first, then retry rmtree
    try:
        for entry in os.listdir(path):
            entry_path = path / entry
            try:
                if entry_path.is_file() or entry_path.is_symlink():
                    entry_path.unlink()
                elif entry_path.is_dir():
                    shutil.rmtree(entry_path, ignore_errors=True)
            except OSError:
                pass
        shutil.rmtree(path)
        return True
    except OSError:
        pass

    # 策略 3：subprocess rm -rf（处理某些内核级锁的情况）
    # Strategy 3: subprocess rm -rf (handles some kernel-level locks)
    try:
        subprocess.run(["rm", "-rf", str(path)], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        pass

    return False


def cleanup_stale_temp_dirs(watch_dir, temp_prefix):
    """
    递归扫描监视目录，清理所有残留的临时工作目录（通常因程序崩溃未清理）。
    使用 _rmtree_robust 增强对 NFS .nfs 残留、权限问题等容错。

    Recursively scan the watch directory and remove any stale temporary
    working directories (usually left behind by crashes).
    Uses _rmtree_robust for better tolerance of NFS .nfs* leftovers,
    permission issues, etc.

    :param watch_dir: 监视目录的根路径 (Root path of the watched directory)
    :param temp_prefix: 临时目录的前缀 (Prefix of temporary directory names)
    """
    count = 0
    for root, dirs, _ in os.walk(watch_dir):
        for d in list(dirs):
            if d.startswith(temp_prefix):
                full_path = Path(root) / d
                if _rmtree_robust(full_path):
                    count += 1
                    log.info("Removed stale temp dir: %s", full_path)
                else:
                    log.warning(
                        "Failed to remove stale temp dir %s — "
                        "directory may contain NFS .nfs* locked files. "
                        "Try: lsof +D %s  to find the holding process.",
                        full_path, full_path,
                    )
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith(temp_prefix)]
    if count:
        log.info("Cleaned up %d stale temp director%s", count, "ies" if count != 1 else "y")




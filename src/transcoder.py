import os
import queue
import random
import re
import shutil
import signal
import string
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
# 活跃临时工作目录（转码进行中），cleanup 必须跳过，防止删除正在使用的目录。
# Active temp work dirs; the stale-cleanup pass must skip these.
_active_temp_dirs = set()
_active_temp_dirs_lock = threading.Lock()


def active_temp_dirs_snapshot():
    """返回当前活跃临时目录的副本（供 cleanup 传入，避免持锁遍历）。"""
    with _active_temp_dirs_lock:
        return set(_active_temp_dirs)


# 异步 VMAF 打分任务队列：任务元组 (source, output, ratio, safe_stem, history)。
# 无界队列，任务仅含路径元组不占内存；打分并发由 worker 数控制。
# Async VMAF scoring task queue; tasks are (source, output, ratio, safe_stem, history)
# tuples. Unbounded: entries are lightweight path tuples, concurrency is bounded by workers.
vmaf_tasks = queue.Queue()
_VMAF_STOP = object()
_vmaf_worker_threads = []
_vmaf_workers_lock = threading.Lock()
_vmaf_workers_started = False


def _final_dest_path(filepath, output_filename, input_dir, output_dir):
    """计算输出区目标路径（保持相对目录结构，重名自动编号）。"""
    if output_dir:
        output_dir = Path(output_dir)
        if input_dir:
            input_dir = Path(input_dir)
            try:
                rel = filepath.parent.relative_to(input_dir)
            except ValueError:
                rel = Path()
            dest_dir = output_dir / rel
        else:
            dest_dir = output_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        final_path = _unique_path(dest_dir / output_filename)
    else:
        final_path = _unique_path(filepath.parent / output_filename)
    return final_path


def _handle_vmaf_task(task):
    """处理单个异步 VMAF 打分任务：打分后按 should_accept 决策文件去留并标记历史。"""
    source, output, ratio, safe_stem, history = task
    source = Path(source)
    output = Path(output)

    vmaf_log = Path(config.VMAF_LOG_DIR) / f"{safe_stem}.{_time.strftime('%Y%m%d-%H%M%S')}.json"
    vmaf_start = _time.monotonic()
    vmaf_score = compute_vmaf(source, output, log_path=vmaf_log)
    vmaf_elapsed = _time.monotonic() - vmaf_start

    if should_accept(vmaf_score, ratio):
        if not output.exists():
            log.error("VMAF ACCEPT FAILED: output missing for %s, marking failed", source.name)
            history.mark_failed(str(source), "VMAF accept failed: output missing")
            return
        try:
            source.unlink()
        except OSError as e:
            log.error("VMAF ACCEPT CLEANUP FAILED: %s — %s", source.name, e)
            history.mark_failed(str(source), f"VMAF accept cleanup failed: {e}")
            return
        history.mark_done(str(source), str(output), vmaf_score=vmaf_score, rejected=False)
        log.info(
            "ACCEPT (async): %s (VMAF %.2f, ratio %.1f%%, %.1fs)",
            output.name,
            vmaf_score or 0,
            ratio * 100,
            vmaf_elapsed,
        )
    else:
        try:
            output.unlink()
        except OSError as e:
            log.error("VMAF REJECT CLEANUP FAILED: %s — %s", output.name, e)
            history.mark_failed(str(source), f"VMAF reject cleanup failed: {e}")
            return
        # 极端低分视为输入/产物异常（如写入中的残缺文件被误转码），
        # 记 failed 触发退避重试，而不是永久 rejected 封锁该文件。
        if vmaf_score is not None and vmaf_score < config.VMAF_EXTREME_LOW:
            history.mark_failed(
                str(source),
                f"VMAF {vmaf_score:.2f} below {config.VMAF_EXTREME_LOW}, input may be incomplete",
            )
            log.warning(
                "VMAF EXTREME LOW (async): %s (VMAF %.2f, ratio %.1f%%, marking failed for retry)",
                output.name,
                vmaf_score,
                ratio * 100,
            )
            return
        history.mark_done(str(source), None, vmaf_score=vmaf_score, rejected=True)
        log.warning(
            "REJECT (async): %s (VMAF %s, ratio %.1f%%, keeping original, %.1fs)",
            output.name,
            f"{vmaf_score:.2f}" if vmaf_score is not None else "N/A",
            ratio * 100,
            vmaf_elapsed,
        )


def _vmaf_worker():
    """后台 VMAF 打分 worker 循环：消费队列直到收到停止哨兵。"""
    while True:
        task = vmaf_tasks.get()
        if task is _VMAF_STOP:
            return
        try:
            _handle_vmaf_task(task)
        except Exception as e:
            log.error("VMAF WORKER ERROR: %s — %s", task[0], e, exc_info=True)


def start_vmaf_workers():
    """启动后台 VMAF 打分 worker（幂等，重复调用不产生新线程）。"""
    global _vmaf_workers_started
    with _vmaf_workers_lock:
        if _vmaf_workers_started:
            return
        for _ in range(max(1, config.VMAF_WORKERS)):
            thread = threading.Thread(target=_vmaf_worker, name="vmaf-worker", daemon=True)
            thread.start()
            _vmaf_worker_threads.append(thread)
        _vmaf_workers_started = True
        log.info("Started %d async VMAF worker thread(s)", len(_vmaf_worker_threads))


def drain_vmaf_tasks():
    """优雅关闭：等待队列中所有打分任务完成并停止所有 worker 线程。"""
    global _vmaf_workers_started
    with _vmaf_workers_lock:
        if not _vmaf_workers_started:
            return
        workers = list(_vmaf_worker_threads)
    for _ in workers:
        vmaf_tasks.put(_VMAF_STOP)
    for thread in workers:
        thread.join()
    with _vmaf_workers_lock:
        _vmaf_worker_threads.clear()
        _vmaf_workers_started = False
    log.info("Drained %d async VMAF worker thread(s)", len(workers))


def recover_pending_vmaf(history):
    """启动恢复：将残留的 pending_vmaf 记录重新入队打分（kill -9 后的双份文件）。"""
    count = 0
    for filepath, output_path in history.iter_pending_vmaf():
        if not Path(output_path).exists():
            log.error("VMAF RECOVER: output missing for %s, marking failed", filepath)
            history.mark_failed(filepath, "Pending VMAF output missing on recovery")
            continue
        if not Path(filepath).exists():
            log.warning("VMAF RECOVER: source missing for %s, removing orphan output", filepath)
            try:
                Path(output_path).unlink()
            except OSError:
                pass
            history.mark_done(filepath, None, rejected=True)
            continue
        stem = Path(filepath).stem
        safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80]
        try:
            ratio = Path(output_path).stat().st_size / Path(filepath).stat().st_size
        except OSError:
            ratio = 0
        vmaf_tasks.put((filepath, output_path, ratio, safe_stem, history))
        count += 1
    if count:
        log.info("Recovered %d pending VMAF task(s)", count)


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

    sw_decode_needed = False
    for s in streams:
        if s.get("codec_type") == "video":
            if s.get("codec_name", "").lower() in config.SW_DECODE_FALLBACK_CODECS:
                sw_decode_needed = True
            break

    cmd = [
        # 基础参数 / Basic parameters
        ffmpeg_path,
        "-y",
        # Intel QSV 硬件加速 / Intel QSV hardware acceleration
        "-hwaccel",
        "qsv",
    ]

    if sw_fallback or sw_decode_needed:
        # 软件缩放回退：帧保持在系统内存，用软件 format 转换后 hwupload 上传
        # Software-scale fallback: keep frames in system memory, convert with
        # software format filter, then upload to QSV via hwupload
        cmd.extend(["-hwaccel_output_format", "none"])
    else:
        cmd.extend(["-hwaccel_output_format", "qsv"])

    if sw_decode_needed:
        cmd.extend(["-c:v", "vc1"])

    enc = config.ENCODER_PARAMS[config.VIDEO_ENCODER]

    cmd.extend(
        [
            # 输入文件 / Input file
            "-i",
            str(input_path),
            # --- 视频流 / Video stream ---
            # 选择第一个视频流（按类型，而非按索引），避免流顺序问题
            # Select the first video stream by type (not index) to avoid stream ordering issues
            "-map",
            "0:v:0",
            "-c:v",
            enc["codec"],
            "-profile:v",
            enc["profile"],
            "-global_quality",
            str(enc["quality"]),
            "-preset",
            enc["preset"],
            # 高级 QSV 优化 / Advanced QSV optimisation
            "-look_ahead",
            "1",
            "-look_ahead_depth",
            str(config.VIDEO_LOOK_AHEAD_DEPTH),
            "-g",
            str(config.VIDEO_GOP),
            "-fps_mode",
            "cfr",
            # 编码器专属参数（-bf / -extbrc / -adaptive_i / -adaptive_b / -b_strategy 等）
            # Encoder-specific arguments
        ]
    )

    if enc.get("b_frames"):
        cmd.extend(["-bf", str(enc["b_frames"])])
    for flag in ("extbrc", "adaptive_i", "adaptive_b", "b_strategy"):
        val = enc.get(flag)
        if val is not None and val is not False:
            cmd.extend([f"-{flag}", str(int(val) if isinstance(val, bool) else val)])

    cmd.extend(
        [
            # --- 音频流 / Audio stream ---
            # 选择所有音频流（'?' 表示无音频时不报错）
            # Select all audio streams ('?' means don't fail if none exist)
            "-map",
            "0:a?",
        ]
    )

    if sw_fallback or sw_decode_needed:
        cmd.extend(["-vf", "format=p010le,hwupload=extra_hw_frames=64"])
    else:
        cmd.extend(["-vf", "vpp_qsv=format=p010le"])

    # 逐流音频编码策略：直通格式复制，其余重新编码
    # Per-stream audio encoding: passthrough for compatible codecs, re-encode for others
    audio_idx = 0
    for s in streams:
        if s.get("codec_type") == "audio":
            codec = s.get("codec_name", "").lower()
            if codec in config.AUDIO_PASSTHROUGH_CODECS:
                cmd.append(f"-c:a:{audio_idx}")
                cmd.append("copy")
            else:
                cmd.append(f"-c:a:{audio_idx}")
                cmd.append(config.AUDIO_CODEC)
                cmd.append(f"-b:a:{audio_idx}")
                cmd.append(config.AUDIO_BITRATE)
            audio_idx += 1

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
_FPS_RE = re.compile(r"fps=\s*([\d.]+)")


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


def _parse_progress_fps(line):
    m = _FPS_RE.search(line)
    if m:
        return float(m.group(1))
    return None


def _run_ffmpeg_with_progress(cmd, total_duration, file_stem, timeout=None):
    """
    运行 FFmpeg 命令并实时解析进度信息。
    在后台线程中读取 stderr，每当进度前进 >=1% 时记录日志（含 fps）。

    Run an FFmpeg command and parse progress information in real time.
    A background thread reads stderr and logs fps every time
    progress advances by >=1%.

    :param cmd: FFmpeg 命令行参数列表 (FFmpeg command-line argument list)
    :param total_duration: 视频总时长（秒），用于计算百分比；为 None 时不计算
                           Total duration in seconds for percentage calculation; skipped if None
    :param file_stem: 文件名（不含扩展名），用于日志标识
                      File name without extension, used in log messages
    :param timeout: 超时秒数，超时后 kill 子进程并抛出 TimeoutExpired
                    Timeout in seconds; kills the subprocess and raises TimeoutExpired on expiry
    :return: (returncode, stderr_text) — 退出码和完整错误输出
             (returncode, full_stderr_text)
    :raises subprocess.TimeoutExpired: 当子进程在指定时间内未完成
                                       When the subprocess does not finish within timeout
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    register_ffmpeg_pid(proc.pid)

    stderr_lines = []
    last_logged_pct = -1
    reader_done = threading.Event()

    def _reader():
        nonlocal last_logged_pct
        for raw_line in iter(proc.stderr.readline, b""):
            line = raw_line.decode("utf-8", errors="replace")
            stderr_lines.append(line)
            if total_duration:
                secs = _parse_progress_time(line)
                if secs is not None and secs > 0:
                    pct = int(secs / total_duration * 100)
                    if pct >= last_logged_pct + 1:
                        fps = _parse_progress_fps(line)
                        if fps is not None:
                            log.info(
                                "PROGRESS: %s - %d%% (fps %.1f)", file_stem, min(pct, 100), fps
                            )
                        else:
                            log.info("PROGRESS: %s - %d%%", file_stem, min(pct, 100))
                        last_logged_pct = pct
        reader_done.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    try:
        if timeout is not None:
            proc.wait(timeout=timeout)
        else:
            proc.wait()
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        reader_done.wait(timeout=10)
        unregister_ffmpeg_pid(proc.pid)
        raise

    unregister_ffmpeg_pid(proc.pid)
    reader_done.wait(timeout=10)

    return proc.returncode, "".join(stderr_lines)


def transcode(
    filepath, history, ffmpeg_path, ffprobe_path, input_dir=None, output_dir=None, cache_dir=None
):
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
    :param cache_dir: 缓存（临时）目录，不设置则同源目录
                      (Cache/temp directory, defaults to source parent)
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

    with _active_temp_dirs_lock:
        _active_temp_dirs.add(str(temp_dir))
    history.mark_started(str(filepath))
    with _active_transcodes_lock:
        _active_transcodes.add(str(filepath))

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "TRANSCODE: %s (%s) -> %s (temp: %s)",
            filepath.name,
            _format_size(input_size),
            output_filename,
            temp_dir.name,
        )

        cmd, total_duration = _build_ffmpeg_cmd(
            ffmpeg_path, ffprobe_path, filepath, output_path, sw_fallback=False
        )
        log.info("FFMPEG CMD: %s %s", filepath.name, " ".join(str(c) for c in cmd))

        returncode, stderr = _run_ffmpeg_with_progress(
            cmd, total_duration, filepath.name, timeout=config.CODEC_TIMEOUT
        )

        # 第一次运行（QSV）stderr 存入 .ffmpeg.log.qsv
        try:
            (temp_dir / ".ffmpeg.log.qsv").write_text(stderr, encoding="utf-8")
        except OSError:
            pass

        # QSV filter reinit 失败则立即用软件缩放重试（新建临时目录避免冲突）
        if returncode != 0 and "Error reinitializing filters" in stderr:
            log.warning("QSV SW FALLBACK: %s - re-trying with software scale", filepath.name)
            old_temp_dir = output_path.parent
            _cleanup_temp(temp_dir)
            suffix = _random_suffix()
            if cache_dir:
                temp_dir = Path(cache_dir) / f"{config.TEMP_PREFIX}{suffix}"
            else:
                temp_dir = filepath.parent / f"{config.TEMP_PREFIX}{suffix}"
            with _active_temp_dirs_lock:
                _active_temp_dirs.discard(str(old_temp_dir))
                _active_temp_dirs.add(str(temp_dir))
            temp_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_dir / output_filename
            cmd, _ = _build_ffmpeg_cmd(
                ffmpeg_path, ffprobe_path, filepath, output_path, sw_fallback=True
            )
            log.info(
                "FFMPEG CMD (sw fallback): %s %s", filepath.name, " ".join(str(c) for c in cmd)
            )
            returncode, stderr = _run_ffmpeg_with_progress(
                cmd, total_duration, filepath.name, timeout=config.CODEC_TIMEOUT
            )

        # 最终 stderr 写入 .ffmpeg.log
        try:
            (temp_dir / ".ffmpeg.log").write_text(stderr, encoding="utf-8")
        except OSError:
            pass

        if returncode != 0:
            error_msg = stderr.strip() or f"exit code {returncode}"
            elapsed = _time.monotonic() - start_time
            log.error("TRANSCODE FAILED: %s (%.1fs) - %s", filepath.name, elapsed, error_msg)
            _cleanup_temp(temp_dir)
            history.mark_failed(str(filepath), error_msg)
            return False

        if not output_path.exists():
            elapsed = _time.monotonic() - start_time
            log.error("TRANSCODE FAILED: output not found for %s (%.1fs)", filepath.name, elapsed)
            _cleanup_temp(temp_dir)
            history.mark_failed(str(filepath), "Output file not found after transcode")
            return False

        output_size = output_path.stat().st_size
        ratio = output_size / input_size if input_size else 0
        elapsed = _time.monotonic() - start_time

        # 异步 VMAF：文件先移入输出区但不删源，后台 worker 打分后决定去留。
        # 与内联打分完全互斥（内联 VMAF 耗时数分钟到数小时，阻塞转码线程）。
        # Async VMAF: move the output into place but keep the source; a background
        # worker scores it later and keeps exactly one of the two files.
        if config.VMAF_ASYNC:
            if ratio > config.ACCEPT_RATIO_MAX and config.VMAF_REQUIRED:
                # ratio 超标必拒（VMAF ≥ 93 也要求 ratio ≤ ACCEPT_RATIO_MAX），
                # 直接拒绝不入队，省去昂贵的采样打分。
                _cleanup_temp(temp_dir)
                history.mark_done(str(filepath), None, vmaf_score=None, rejected=True)
                log.warning(
                    "REJECT (async): %s (ratio %.1f%% exceeds %.0f%%, keeping original)",
                    filepath.name,
                    ratio * 100,
                    config.ACCEPT_RATIO_MAX * 100,
                )
                return True
            final_path = _final_dest_path(filepath, output_filename, input_dir, output_dir)
            if final_path.name != output_filename:
                log.info("DEST EXISTS: using %s instead of %s", final_path.name, output_filename)
            try:
                shutil.copyfile(str(output_path), str(final_path))
                output_path.unlink()
                _cleanup_temp(temp_dir)
            except OSError as e:
                log.error(
                    "FILE MOVE FAILED (async): %s — %s",
                    final_path.name,
                    e,
                )
                _cleanup_temp(temp_dir)
                history.mark_failed(str(filepath), f"File move failed: {e}")
                return False
            history.mark_pending_vmaf(str(filepath), str(final_path))
            safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80]
            vmaf_tasks.put((str(filepath), str(final_path), ratio, safe_stem, history))
            log.info(
                "VMAF PENDING (async): %s (%s, ratio %.1f%%, queued for scoring)",
                final_path.name,
                _format_size(output_size),
                ratio * 100,
            )
            return True

        # ratio 超标则直接跳过 VMAF（必拒，省去几十分钟到2小时的采样打分）
        vmaf_score = None
        vmaf_elapsed = None
        if config.VMAF_REQUIRED and ratio <= config.ACCEPT_RATIO_MAX:
            safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80]
            vmaf_log = (
                Path(config.VMAF_LOG_DIR) / f"{safe_stem}.{_time.strftime('%Y%m%d-%H%M%S')}.json"
            )
            vmaf_start = _time.monotonic()
            vmaf_score = compute_vmaf(filepath, output_path, log_path=vmaf_log)
            vmaf_elapsed = _time.monotonic() - vmaf_start

        if should_accept(vmaf_score, ratio):
            final_path = _final_dest_path(filepath, output_filename, input_dir, output_dir)
            if final_path.name != output_filename:
                log.info("DEST EXISTS: using %s instead of %s", final_path.name, output_filename)
            # 先移动文件，成功后再记录 history；失败不影响 VMAF 日志
            # Copy output before logging acceptance — file I/O may fail on NFS
            try:
                # 跨文件系统时 os.rename 不可用，且 copy2/chmod 在 NFS 上 EPERM
                # os.rename fails across filesystems; copy2/copymode raises EPERM on NFS
                shutil.copyfile(str(output_path), str(final_path))
                output_path.unlink()
                _cleanup_temp(temp_dir)
                filepath.unlink()
            except OSError as e:
                log.error(
                    "FILE MOVE FAILED: %s (VMAF %.2f, ratio %.1f%%) — %s",
                    final_path.name,
                    vmaf_score or 0,
                    ratio * 100,
                    e,
                )
                _cleanup_temp(temp_dir)
                history.mark_failed(str(filepath), f"File move failed: {e}")
                return False
            history.mark_done(str(filepath), str(final_path), vmaf_score=vmaf_score, rejected=False)
            if vmaf_elapsed is not None:
                log.info(
                    "ACCEPT: %s (%s, %s, %.1f%%, VMAF %.2f, codec %.1fs + vmaf %.1fs)",
                    final_path.name,
                    _format_size(output_size),
                    "smaller" if ratio < 1 else "larger",
                    ratio * 100,
                    vmaf_score,
                    elapsed,
                    vmaf_elapsed,
                )
            else:
                log.info(
                    "ACCEPT: %s (%s, %s, %.1f%%, %.1fs)",
                    final_path.name,
                    _format_size(output_size),
                    "smaller" if ratio < 1 else "larger",
                    ratio * 100,
                    elapsed,
                )
        else:
            _cleanup_temp(temp_dir)
            history.mark_done(str(filepath), None, vmaf_score=vmaf_score, rejected=True)
            if vmaf_elapsed is not None:
                log.warning(
                    "REJECT: %s (VMAF %s, ratio %.1f%%, codec %.1fs + vmaf %.1fs, keeping original)",  # noqa: E501  # 日志格式串含多个 %s，换行破坏占位符对齐
                    filepath.name,
                    f"{vmaf_score:.2f}" if vmaf_score is not None else "N/A",
                    ratio * 100,
                    elapsed,
                    vmaf_elapsed,
                )
            else:
                log.warning(
                    "REJECT: %s (VMAF %s, ratio %.1f%%, keeping original, %.1fs)",
                    filepath.name,
                    f"{vmaf_score:.2f}" if vmaf_score is not None else "N/A",
                    ratio * 100,
                    elapsed,
                )
        return True

    except subprocess.TimeoutExpired:
        elapsed = _time.monotonic() - start_time
        log.error("TRANSCODE TIMEOUT: %s (%.1fs)", filepath.name, elapsed)
        _cleanup_temp(temp_dir)
        history.mark_failed(str(filepath), "Transcode timed out")
        return False
    except Exception as e:
        elapsed = _time.monotonic() - start_time
        log.error("TRANSCODE ERROR: %s (%.1fs) - %s", filepath.name, elapsed, e)
        _cleanup_temp(temp_dir)
        history.mark_failed(str(filepath), str(e))
        return False
    finally:
        with _active_transcodes_lock:
            _active_transcodes.discard(str(filepath))
        with _active_temp_dirs_lock:
            _active_temp_dirs.discard(str(temp_dir))


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


def cleanup_stale_temp_dirs(watch_dir, temp_prefix, active_dirs=None):
    """
    递归扫描监视目录，清理所有残留的临时工作目录（通常因程序崩溃未清理）。
    使用 _rmtree_robust 增强对 NFS .nfs 残留、权限问题等容错。
    active_dirs 中的目录（当前正在转码）会被跳过。

    Recursively scan the watch directory and remove any stale temporary
    working directories (usually left behind by crashes). Directories listed
    in active_dirs (currently being transcoded) are skipped.
    Uses _rmtree_robust for better tolerance of NFS .nfs* leftovers,
    permission issues, etc.

    :param watch_dir: 监视目录的根路径 (Root path of the watched directory)
    :param temp_prefix: 临时目录的前缀 (Prefix of temporary directory names)
    :param active_dirs: 活跃临时目录集合（跳过不删）(Set of active temp dirs to skip)
    """
    count = 0
    active = set(active_dirs or ())
    for root, dirs, _ in os.walk(watch_dir):
        for d in list(dirs):
            if d.startswith(temp_prefix):
                full_path = Path(root) / d
                if str(full_path) in active:
                    continue
                if _rmtree_robust(full_path):
                    count += 1
                    log.info("Removed stale temp dir: %s", full_path)
                else:
                    log.warning(
                        "Failed to remove stale temp dir %s — "
                        "directory may contain NFS .nfs* locked files. "
                        "Try: lsof +D %s  to find the holding process.",
                        full_path,
                        full_path,
                    )
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith(temp_prefix)]
    if count:
        log.info("Cleaned up %d stale temp director%s", count, "ies" if count != 1 else "y")

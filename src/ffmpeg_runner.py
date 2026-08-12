"""
FFmpeg 执行模块。构建 FFmpeg 命令行、执行转码并实时解析进度（时长、fps），
以及把失败的 stderr 归纳为简洁摘要。

FFmpeg runner module. Builds the FFmpeg command line, runs the transcode while
parsing live progress (time, fps), and summarizes failed stderr output.
"""

import re
import subprocess
import threading

from src import config
from src.probe import get_streams_info, get_video_codec
from src.process_guard import register_ffmpeg_pid, unregister_ffmpeg_pid

log = __import__("logging").getLogger(__name__)


def _build_ffmpeg_cmd(
    ffmpeg_path,
    ffprobe_path,
    input_path,
    output_path,
    sw_scale_fallback=False,
    sw_decode_fallback=False,
    cfr_rate=None,
):
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
    :param sw_scale_fallback: 是否使用软件缩放回退（绕开 vpp_qsv 滤镜在 hwaccel 切换时的兼容性问题）
                        Whether to use software-scale fallback (bypass vpp_qsv filter
                        compatibility issues during hwaccel reconfiguration)
    :param cfr_rate: 伪 VFR 源（classify_fps 档2）的 CFR 化目标帧率分数；
                     作为输出选项加 -r 把产物规整为真 CFR，供后续 VMAF 逐帧对齐打分。
                     (Tier-2 pseudo-VFR target fraction; emitted as the -r output option
                     so the output is true CFR for aligned VMAF scoring)
    :return: (cmd_list, total_duration) — 命令列表和视频总时长（秒）
             (cmd_list, total_duration) where duration is in seconds, or None if unknown
    :raises RuntimeError: 当流探测失败时抛出 (Raised when stream probing fails)
    """
    info = get_streams_info(input_path, ffprobe_path)
    if info is None:
        raise RuntimeError(f"Failed to probe streams for {input_path}")

    streams = info["streams"]
    duration = info["duration"]

    sw_decode_codec = None
    if sw_decode_fallback:
        sw_decode_codec = get_video_codec(input_path, ffprobe_path)

    cmd = [
        # 基础参数 / Basic parameters
        ffmpeg_path,
        "-y",
        # Intel QSV 硬件加速 / Intel QSV hardware acceleration
        "-hwaccel",
        "qsv",
    ]

    if sw_scale_fallback or sw_decode_fallback:
        # 软件缩放回退：帧保持在系统内存，用软件 format 转换后 hwupload 上传
        # Software-scale fallback: keep frames in system memory, convert with
        # software format filter, then upload to QSV via hwupload
        cmd.extend(["-hwaccel_output_format", "none"])
        # 显式初始化 QSV 设备，为 hwupload 滤镜提供
        # 设备上下文；否则 "Error initializing filters" 并非真实编码失败
        cmd.extend(["-init_hw_device", "qsv=hw"])
    else:
        cmd.extend(["-hwaccel_output_format", "qsv"])

    if sw_decode_fallback and sw_decode_codec:
        # 软件解码回退：置于 -i 之前，作为输入选项强制输入解码器。
        # 解码器名取自探测到的 codec_name（与 SW_DECODE_FALLBACK_CODECS 对应）。
        # Software-decode fallback: placed before -i, forces the input decoder
        # as an input option. The decoder name comes from the probed codec_name.
        cmd.extend(["-c:v", sw_decode_codec])

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

    if sw_scale_fallback or sw_decode_fallback:
        cmd.extend(["-vf", "format=p010le,hwupload=extra_hw_frames=64"])
    else:
        cmd.extend(["-vf", "vpp_qsv=format=p010le"])

    if cfr_rate:
        # 伪 VFR → 真 CFR：输出选项 -r 让编码器按目标帧率规整帧序。
        # Project the pseudo-VFR source onto a true CFR grid via the -r output option.
        cmd.extend(["-r", str(cfr_rate)])

    cmd.extend(
        [
            # --- 音频流 / Audio stream ---
            # 选择所有音频流（'?' 表示无音频时不报错）
            # Select all audio streams ('?' means don't fail if none exist)
            "-map",
            "0:a?",
        ]
    )

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
    """从 FFmpeg 进度行解析实时 fps。

    Parse the real-time fps value from an FFmpeg progress line.

    :param line: FFmpeg 的一行进度输出 (A single FFmpeg progress output line)
    :return: fps 数值，未匹配时返回 None (The fps value, or None if not matched)
    """
    m = _FPS_RE.search(line)
    if m:
        return float(m.group(1))
    return None


def _run_ffmpeg_with_progress(cmd, total_duration, file_stem, timeout=None):
    """
    运行 FFmpeg 命令并实时解析进度信息。
    在后台线程中读取 stderr，每当进度前进 >=1% 时记录日志（若该行可解析出 fps 则一并记录）。

    Run an FFmpeg command and parse progress information in real time.
    A background thread reads stderr and logs a message every time
    progress advances by >=1%, including the fps when it is parseable.

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


def _summarize_error_stderr(stderr, max_body=100, head_keep=20):
    """
    精简 FFmpeg 失败输出用于日志与历史记录：过滤逐帧进度行并截断过长输出。
    FFmpeg 真正的错误集中在输出末尾，几千行 "frame=..." 进度行没有诊断价值。

    Summarize FFmpeg error output for logging/history: drop per-frame progress
    lines and truncate overly long output. The real error is at the tail; the
    thousands of "frame=..." progress lines carry no diagnostic value.

    :param stderr: 完整 FFmpeg stderr 文本 (Full FFmpeg stderr text)
    :param max_body: 保留的非进度行数上限 (Max non-progress lines to keep)
    :param head_keep: 超限时保留的头部行数 (Head lines kept when truncating)
    :return: 精简后的错误摘要 (Summarized error text)
    """
    lines = [line.rstrip() for line in stderr.splitlines() if line.strip()]
    body = [line for line in lines if not line.strip().startswith("frame=")]
    if len(body) > max_body:
        tail_keep = max(0, max_body - head_keep - 1)
        body = (
            body[:head_keep]
            + [f"... (truncated {len(body) - max_body} lines) ..."]
            + body[-tail_keep:]
        )
    return "\n".join(body) or stderr.strip()

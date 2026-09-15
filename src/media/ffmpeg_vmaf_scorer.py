import json
import os
import re
import subprocess
import time
from pathlib import Path

from src.media.probe import VideoProbe

log = __import__("logging").getLogger(__name__)


class FfmpegVMAFScorer:
    """
    封装 ffmpeg libvmaf 打分。`compute_vmaf` 是唯一入口：若启用 HW 解码，
    先尝试硬件路径，失败时按 `vmaf_hw_fallback` 决定是否回退到软件解码路径。

    Wraps ffmpeg libvmaf scoring. `compute_vmaf` is the sole entry point: if
    HW decode is enabled, it tries the hardware path first and, on failure,
    falls back to the software path according to `vmaf_hw_fallback`.
    """

    def __init__(
        self,
        ffprobe_path: str,
        vmaf_ffmpeg_path: str,
        vmaf_model_path: str,
        vmaf_log_dir: str,
        n_threads: int,
        n_subsample: int,
        vmaf_timeout: int,
        vmaf_hwaccel: bool,
        vmaf_hw_fallback: bool,
    ):
        self.ffprobe_path = ffprobe_path
        # 无状态封装，单个实例可安全服务所有 VMAF 任务。
        # Stateless wrapper; a single instance can safely serve all VMAF jobs.
        self.probe = VideoProbe(ffprobe_path)
        self.vmaf_ffmpeg_path = vmaf_ffmpeg_path
        self.vmaf_model_path = vmaf_model_path
        self.vmaf_log_dir = vmaf_log_dir
        self.n_threads = n_threads
        self.n_subsample = n_subsample
        self.timeout = vmaf_timeout
        self.vmaf_hwaccel = vmaf_hwaccel
        self.vmaf_hw_fallback = vmaf_hw_fallback

    def _hw_download_format(self, filepath):
        """
        探测视频真实 pix_fmt，决定 hwdownload 应按 8bit(nv12) 还是 10bit(p010le) 取帧
        （不能假定输入位深，8bit/10bit 源都常见）。探测失败时默认 8bit：猜错的后果只是
        hwdownload 报错、安全回退到软件路径，不会产生错误分数。

        Probe the real pix_fmt to decide whether hwdownload should request 8bit(nv12)
        or 10bit(p010le) — bit depth can't be assumed from the source. Defaults to
        8bit on probe failure: a wrong guess only makes hwdownload fail and fall back
        to software, never a corrupted score.
        """
        pix_fmt = self.probe.get_pix_fmt(filepath) or ""
        if "10le" in pix_fmt or "10be" in pix_fmt or pix_fmt == "p010le":
            return "p010le"
        return "nv12"

    def _read_vmaf_from_json(self, log_path):
        """
        从 libvmaf 输出的 json 日志中读取聚合平均分（pooled_metrics.vmaf.mean）。
        这比正则抓取 stderr 更稳定。
        Read the aggregate mean score from a libvmaf json log; more reliable than
        scraping stderr.

        :param log_path: json 日志路径 (Path to the json log)
        :return: (mean, minimum) 元组，读取失败时返回 (None, None)
                 (mean, minimum) tuple, or (None, None) on failure
        """
        try:
            with open(log_path, encoding="utf-8") as f:
                data = json.load(f)
            vmaf = data.get("pooled_metrics", {}).get("vmaf", {})
            mean = vmaf.get("mean")
            minimum = vmaf.get("min")
            return (
                float(mean) if mean is not None else None,
                float(minimum) if minimum is not None else None,
            )
        except Exception:
            return (None, None)

    def _frame_index_align_string(self):
        """
        构建帧序对齐 setpts 字符串：把每路的帧时间戳按帧序号线性化
        （setpts=N/30/TB；这里的 30 只是归一化基准，取任何值效果相同），使得
        两路第 n 帧时间戳相等、逐帧配对对比，与源/产物的容器 tbr 声明、真实帧率无关。

        Build the frame-index alignment setpts string: linearizes each stream's
        frame timestamps by frame index (setpts=N/30/TB — the 30 is just a
        normalization base and any value would work), so frame n of both inputs
        gets identical timestamps and is compared pairwise, independent of the
        container tbr declaration and true frame rates.
        """
        return "setpts=N/30/TB"

    def _frame_counts_check(self, ref_frame_count, dist_frame_count):
        """
        仅供日志诊断：记录两路帧数是否可得、是否一致。不返回值、不影响打分——
        打分始终走帧序对齐，帧数差异只体现在日志里，不会阻断或改变分数。

        Diagnostic only: logs whether the two frame counts are available and equal.
        Returns nothing and never gates scoring — frame-count differences show up
        only in the log, never in the score.
        """
        if ref_frame_count is None or dist_frame_count is None:
            log.info(
                f"ref, dist frame count differ: {ref_frame_count}, {dist_frame_count}."
                "This may result in inaccurate quality metrics."
            )
            return
        log.info(f"VMAF frame counts ref={ref_frame_count} dist={dist_frame_count}")
        return

    def _run_vmaf(
        self,
        ref_path,
        dist_path,
        log_path,
        ref_frame_count=None,
        dist_frame_count=None,
        hwaccel=False,
    ):
        """
        内部：构建并执行单次 ffmpeg VMAF 命令。
        对齐策略：始终用 setpts=N/30/TB 把两路帧时间戳按帧序号线性化，第 n 帧与
        第 n 帧逐帧配对，不依赖 tbr 声明或帧率网格。尾部帧数不一致时，由 framesync
        的 shortest=1:repeatlast=0 以短流为准截断配对（帧数差异只体现在日志里，
        不会阻断打分）。
        HW 模式下 hwdownload 按每路各自探测到的真实原生位深取帧（8bit → nv12，
        10bit → p010le），不经过 vpp_qsv 等有损转换。若某路源编码 QSV 无法硬解，
        hwdownload 会直接失败（不会静默降级成软解再上传），从而安全回退到软件路径。

        :param hwaccel: True 时启用 QSV 硬件解码（hwdownload 后接软件 libvmaf）
        :param ref_frame_count: 参考帧数（仅日志诊断，不影响打分）
        :param dist_frame_count: 失真帧数（仅日志诊断，不影响打分）
        """
        _align = self._frame_index_align_string()
        self._frame_counts_check(ref_frame_count, dist_frame_count)

        if hwaccel:
            ref_fmt = self._hw_download_format(ref_path)
            dist_fmt = self._hw_download_format(dist_path)
            hwaccel_flags = ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
            filter_complex = (
                "[0:v]hwdownload,format=" + ref_fmt + "[ref_dl];"
                "[ref_dl]" + _align + "[r0];"
                "[1:v]hwdownload,format=" + dist_fmt + "[dist_dl];"
                "[dist_dl]" + _align + "[d0];"
                "[d0][r0]scale2ref=flags=bicubic[d1][r1];"
                "[d1]format=yuv420p[dist];"
                "[r1]format=yuv420p[ref];"
                "[dist][ref]libvmaf={opts}"
            )
            input_parts = (
                hwaccel_flags + ["-i", str(ref_path)] + hwaccel_flags + ["-i", str(dist_path)]
            )
        else:
            filter_complex = (
                "[1:v]" + _align + "[d0];"
                "[0:v]" + _align + "[r0];"
                "[d0][r0]scale2ref=flags=bicubic[d1][r1];"
                "[d1]format=yuv420p[dist];"
                "[r1]format=yuv420p[ref];"
                "[dist][ref]libvmaf={opts}"
            )
            input_parts = ["-i", str(ref_path)] + ["-i", str(dist_path)]

        opts = []
        if self.vmaf_model_path and os.path.exists(self.vmaf_model_path):
            opts.append(f"model='path={self.vmaf_model_path}'")
        opts.append(f"n_subsample={self.n_subsample}")
        opts.append(f"n_threads={self.n_threads}")
        # shortest=1 以短流为准截断尾部，repeatlast=0 表示尾部帧不足时不重复
        # 最后一帧填充——两路帧数不一致时靠这对参数决定如何截断配对。
        # shortest=1 truncates at the shorter stream; repeatlast=0 means it does
        # not pad the tail by repeating the last frame when counts differ.
        opts.append("shortest=1:repeatlast=0")

        if log_path is not None:
            try:
                os.makedirs(os.path.dirname(str(log_path)), exist_ok=True)
                opts.append("log_fmt=json")
                opts.append(f"log_path={log_path}")
            except OSError as e:
                log.warning("VMAF log dir not writable, skipping json log: %s", e)
                log_path = None

        filter_complex_str = filter_complex.format(opts=":".join(opts))
        cmd = (
            [self.vmaf_ffmpeg_path]
            + list(input_parts)
            + ["-filter_complex", filter_complex_str, "-an", "-f", "null", "-"]
        )

        tag = "HW" if hwaccel else "SW"

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=self.timeout)
            if result.returncode != 0:
                log.warning(
                    "VMAF [%s] failed (rc=%d) for %s",
                    tag,
                    result.returncode,
                    os.path.basename(str(dist_path)),
                )
                return None

            # 分数唯一来源是 libvmaf json 日志的 pooled_metrics.vmaf.mean，
            # stderr 不参与取分。
            # The only score source is pooled_metrics.vmaf.mean from the libvmaf
            # json log; stderr is never parsed for scoring.
            if log_path is not None:
                mean, _minimum = self._read_vmaf_from_json(log_path)
                if mean is not None:
                    return mean

            log.warning(
                "VMAF [%s] score not found in json log for %s",
                tag,
                os.path.basename(str(dist_path)),
            )
            return None
        except subprocess.TimeoutExpired:
            log.warning("VMAF [%s] timed out for %s", tag, os.path.basename(str(dist_path)))
        except Exception as e:
            log.warning("VMAF [%s] failed for %s: %s", tag, os.path.basename(str(dist_path)), e)

        return None

    def compute_vmaf(
        self, ref_path, dist_path, log_path=None, ref_frame_count=None, dist_frame_count=None
    ) -> float | None:

        if not self.vmaf_ffmpeg_path:
            log.warning("VMAF not configured, skipping quality check")
            return None

        if log_path is None:
            safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(ref_path).stem)[:80]
            log_path = (
                Path(self.vmaf_log_dir) / f"{safe_stem}.{time.strftime('%Y%m%d-%H%M%S')}.json"
            )

        if self.vmaf_hwaccel:
            score = self._run_vmaf(
                ref_path,
                dist_path,
                log_path,
                ref_frame_count=ref_frame_count,
                dist_frame_count=dist_frame_count,
                hwaccel=True,
            )
            if score is not None:
                return score
            if self.vmaf_hw_fallback:
                log.info("VMAF HW attempt failed, falling back to software decode")
            else:
                return None

        return self._run_vmaf(
            ref_path,
            dist_path,
            log_path,
            ref_frame_count=ref_frame_count,
            dist_frame_count=dist_frame_count,
            hwaccel=False,
        )

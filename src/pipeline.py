from pathlib import Path

from src.job import TranscodeJob
from src.post_transcode import post_transcode
from src.transcoder import transcode


class TranscodePipeline:
    """进程级共享上下文：编码器路径、历史库、打分回调。不可变，线程安全。"""

    def __init__(self, history, ffmpeg_path, ffprobe_path, compute_vmaf=None, keep_source=False):
        self.history = history
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.compute_vmaf = compute_vmaf
        self.keep_source = keep_source

    def make_job(self, filepath, lib_info) -> "TranscodeJob":
        """工厂：扫描/requeue 处唯一需要构造 Job 的入口。"""
        return TranscodeJob(
            pipeline=self,
            filepath=Path(filepath),
            lib_input=Path(lib_info["input"]),
            lib_output=Path(lib_info["output"]),
            lib_cache=Path(lib_info["cache"]),
        )

    def transcode(self, job):
        ok, frames, elapsed = transcode(
            job.filepath,
            self.history,
            self.ffmpeg_path,
            self.ffprobe_path,
            job.lib_input,
            job.lib_output,
            job.lib_cache,
            self.keep_source,
            self.compute_vmaf,
        )
        job.dist_frame_count, job.elapsed = frames, elapsed
        return ok, frames, elapsed

    def post_process(self, job):
        return post_transcode(
            {"dist_frame_count": job.dist_frame_count, "elapsed": job.elapsed},
            job.filepath,
            self.history,
            self.ffmpeg_path,
            self.ffprobe_path,
            job.lib_input,
            job.lib_output,
            job.lib_cache,
            self.keep_source,
            self.compute_vmaf,
        )

from pathlib import Path

from src import config
from src.job import TranscodeJob
from src.post_process import post_process_stage
from src.probe import VideoProbe
from src.transcoder import transcode_stage


class TranscodePipeline:
    """进程级共享上下文：编码器路径、历史库、打分回调。不可变，线程安全。"""

    def __init__(
        self,
        history,
        ffmpeg_path,
        ffprobe_path,
        compute_vmaf=None,
        keep_source=False,
        vmaf_mode=None,
    ):
        self.history = history
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        # ffprobe wrapper is stateless and safe to share across worker threads.
        # ffprobe 封装没有可变状态，可在线程间复用同一个实例。
        self.probe = VideoProbe(ffprobe_path)
        self.compute_vmaf = compute_vmaf
        self.keep_source = keep_source
        self.vmaf_mode = vmaf_mode if vmaf_mode is not None else config.VMAF_MODE

    def make_job(self, filepath, lib_info) -> "TranscodeJob":
        """工厂：扫描/requeue 处唯一需要构造 Job 的入口。"""
        return TranscodeJob(
            pipeline=self,
            filepath=Path(filepath).resolve(),
            lib_input=Path(lib_info["input"]).resolve(),
            lib_output=Path(lib_info["output"]).resolve(),
            lib_cache=Path(lib_info["cache"]).resolve(),
        )

    def transcode(self, job):
        return transcode_stage(self, job)

    def post_process(self, job):
        return post_process_stage(self, job)

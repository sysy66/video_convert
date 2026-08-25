from __future__ import annotations  # 所有标注变字符串，不求值

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只在 mypy/IDE 静态分析时为 True
    from src.pipeline import TranscodePipeline


@dataclass
class TranscodeJob:
    pipeline: TranscodePipeline  # 回指共享依赖（组合，不是复制）,运行时这只是个字符串标注

    # 身份
    filepath: Path
    lib_input: Path
    lib_output: Path
    lib_cache: Path

    # 阶段产物：transcode() 写入，post_process() 读取
    output_path: Path | None = None
    dist_frame_count: int | None = None
    elapsed: float | None = None

    @property
    def key(self) -> str:
        return str(self.filepath)  # 去重注册表的键

    def transcode(self):
        return self.pipeline.transcode(self)

    def post_process(self):
        return self.pipeline.post_process(self)

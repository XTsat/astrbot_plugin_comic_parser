"""NSFW 检测与打码：权重管理、YOLO 检测器、封面马赛克。

四个子模块各自独立：
- :mod:`weights` 只负责「权重在哪、要不要下载」；
- :mod:`detector` 只负责「这张图里有没有露骨部位、有多露骨」；
- :mod:`censor` 只负责「把给定区域糊掉」；
- :mod:`coverage` 负责「多个框合起来盖了多少」的并集计算；
- :mod:`onnx_detector` 是同一套检测逻辑的 ONNX 实现（不需要 torch）。

上层（:mod:`core.cover`）把它们串成「下载完成后发一张（可打码的）封面」。
"""

from .censor import censor_cover, coverage_ratio
from .coverage import union_coverage
from .detector import Detection, Detector, NsfwScore, create_detector, is_nsfw_label
from .deps import ensure_onnxruntime, onnxruntime_installed
from .weights import ModelWeights, pick_model, variant_filename

__all__ = [
    "Detection",
    "Detector",
    "ModelWeights",
    "NsfwScore",
    "censor_cover",
    "coverage_ratio",
    "create_detector",
    "ensure_onnxruntime",
    "onnxruntime_installed",
    "pick_model",
    "is_nsfw_label",
    "union_coverage",
    "variant_filename",
]

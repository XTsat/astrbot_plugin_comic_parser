"""NSFW 检测与打码：权重管理、YOLO 检测器、封面马赛克。

三个子模块各自独立：
- :mod:`weights` 只负责「权重在哪、要不要下载」；
- :mod:`detector` 只负责「这张图里有没有露骨部位、有多露骨」；
- :mod:`censor` 只负责「把给定区域糊掉」。

上层（:mod:`core.cover`）把它们串成「下载完成后发一张（可打码的）封面」。
"""

from .censor import censor_cover, coverage_ratio
from .detector import Detection, NsfwDetector, NsfwScore
from .weights import ModelWeights, pick_model, variant_filename

__all__ = [
    "Detection",
    "ModelWeights",
    "NsfwDetector",
    "NsfwScore",
    "censor_cover",
    "coverage_ratio",
    "pick_model",
    "variant_filename",
]

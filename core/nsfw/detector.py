"""NSFW 检测的公共类型与后端工厂。

检测只有一条实现路径：**ONNX Runtime**（权重 `.onnx`，依赖约 200M）。
PyTorch 那条路已经移除——为了跑一个 39MB 的权重去装 1.8G 的 torch 不划算，
而同一份权重导成 ONNX 后 CPU 推理还更快（实测 960 输入 816ms vs 1086ms）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ...domain.constants import (
    DEFAULT_NSFW_IMGSZ,
    DEFAULT_NSFW_THRESHOLD,
    NSFW_BOX_KEYWORDS,
)
from .weights import ModelWeights


@dataclass(frozen=True, slots=True)
class Detection:
    """一个被判定为露骨的部位。"""

    label: str
    confidence: float
    box: tuple[float, float, float, float]

    @property
    def area(self) -> float:
        return max(0.0, self.box[2] - self.box[0]) * max(0.0, self.box[3] - self.box[1])


@dataclass(frozen=True, slots=True)
class NsfwScore:
    """一次评分的结论：0-100 的露骨指数（按检出的露骨面积占比算）。"""

    value: float = 0.0
    coverage: float = 0.0
    detections: tuple[Detection, ...] = ()

    @property
    def boxes(self) -> list[tuple[float, float, float, float]]:
        """给打码用的框列表。"""
        return [item.box for item in self.detections]

    @property
    def labels(self) -> list[str]:
        return sorted({item.label for item in self.detections})

    @classmethod
    def empty(cls) -> "NsfwScore":
        return cls()


class Detector(Protocol):
    """检测器对外只需要这三件事，打码与评分都只依赖它。"""

    @property
    def model_path(self) -> Path | None: ...

    def analyze(self, image_path: Path, *, threshold: float | None = None) -> NsfwScore: ...

    def reset(self) -> None: ...


def create_detector(
    weights: ModelWeights,
    *,
    threshold: float = DEFAULT_NSFW_THRESHOLD,
    imgsz: int = DEFAULT_NSFW_IMGSZ,
) -> Detector:
    """构造检测器（当前只有 ONNX 实现，保留工厂是为了上层不必知道具体类）。"""
    from .onnx_detector import OnnxDetector  # 局部 import：避免与 onnx_detector 循环依赖

    return OnnxDetector(weights=weights, threshold=threshold, imgsz=imgsz)


def is_nsfw_label(label: str) -> bool:
    """模型类别是否属于需要处理的露骨部位。"""
    lowered = label.lower()
    return any(keyword in lowered for keyword in NSFW_BOX_KEYWORDS)


__all__ = [
    "Detection",
    "Detector",
    "NsfwScore",
    "create_detector",
    "is_nsfw_label",
]

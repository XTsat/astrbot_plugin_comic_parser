"""NSFW 检测的公共类型与后端工厂。

检测走 **ONNX Runtime**（权重 `.onnx`，依赖约 100MB）：装得少、CPU 推理也够快
（实测 960 输入单张约 0.9 秒）。依赖不随插件安装，开启打码后由插件自行补上；
缺失时退化成一个「不可用」检测器，不影响下载与打包。
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
    NSFW_IGNORED_KEYWORDS,
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
    # 检测压根没跑起来（没模型 / 推理报错）。区别于 failed=False 且 detections 为空
    # 的「跑了但图上没检出」——后者是安全的，前者不能当「干净」处理。
    failed: bool = False

    @property
    def boxes(self) -> list[tuple[float, float, float, float]]:
        """给打码用的框列表。"""
        return [item.box for item in self.detections]

    @property
    def labels(self) -> list[str]:
        return sorted({item.label for item in self.detections})

    @classmethod
    def empty(cls) -> "NsfwScore":
        """跑过检测，图上没有露骨部位。"""
        return cls()

    @classmethod
    def unavailable(cls) -> "NsfwScore":
        """检测没能执行（无模型或推理失败），结论未知。"""
        return cls(failed=True)


class Detector(Protocol):
    """检测器对外只需要这三件事，打码与评分都只依赖它。"""

    @property
    def model_path(self) -> Path | None: ...

    @property
    def input_size(self) -> int | None: ...

    def analyze(self, image_path: Path, *, threshold: float | None = None) -> NsfwScore: ...

    def reset(self) -> None: ...


class NullDetector:
    """运行时依赖缺失时的占位检测器。

    `onnxruntime`（连同 numpy）是按需安装的，插件刚装上、依赖还没装好时不能因此
    加载失败：这里一律按「没有模型」回答，上层照旧走「不发封面」的分支。
    """

    @property
    def model_path(self) -> Path | None:
        return None

    @property
    def input_size(self) -> int | None:
        return None

    def analyze(self, image_path: Path, *, threshold: float | None = None) -> NsfwScore:
        return NsfwScore.unavailable()

    def reset(self) -> None:
        return None


def create_detector(
    weights: ModelWeights,
    *,
    threshold: float = DEFAULT_NSFW_THRESHOLD,
    imgsz: int = DEFAULT_NSFW_IMGSZ,
    labels: tuple[str, ...] = (),
    task: str = "",
) -> Detector:
    """构造检测器（当前只有 ONNX 实现，保留工厂是为了上层不必知道具体类）。

    Args:
        weights: 权重管理对象，负责「用哪份权重」与「从哪下」。
        threshold: 置信度下限。
        imgsz: 推理输入尺寸。
        labels: 类别顺序覆盖；留空则交给检测器按权重文件名与权重自带 metadata 解析
            （不同上游的类别集合不同，不能在上层写死一份）。
        task: 任务类型覆盖（detect / segment）；留空时取 weight 预设里的值，再由
            权重 metadata 校正——分割导出必须知道这件事，否则会多解出 mask 系数列。
    """
    try:
        from .onnx_detector import OnnxDetector  # 局部 import：避免循环依赖
    except ImportError as exc:  # numpy / onnxruntime 还没装好
        from astrbot.api import logger

        from ...domain.constants import PLUGIN_NAME

        logger.warning(
            f"[{PLUGIN_NAME}] ⚠️ 检测运行时未就绪（{exc}），本次加载不做封面打码；"
            "开启打码时插件会自动安装依赖，装好后重载即可"
        )
        return NullDetector()

    return OnnxDetector(
        weights=weights,
        threshold=threshold,
        imgsz=imgsz,
        labels=labels or tuple(getattr(weights, "labels", ())),
        task=task or str(getattr(weights, "task", "")),
    )


def is_nsfw_label(label: str) -> bool:
    """模型类别是否属于需要处理的露骨部位。

    用子串匹配，所以 `nipple`、`nipple_f`、`female_nipple` 这类带前后缀的写法都认；
    先过一遍 `NSFW_IGNORED_KEYWORDS` 否定表：二次元分割档会给出分数高达 0.9 的
    `female face` / `male face`，把脸糊掉既没必要，也会在类无关 NMS 里挤掉重叠的部位框。
    """
    lowered = label.lower()
    if any(keyword in lowered for keyword in NSFW_IGNORED_KEYWORDS):
        return False
    return any(keyword in lowered for keyword in NSFW_BOX_KEYWORDS)


__all__ = [
    "Detection",
    "Detector",
    "NsfwScore",
    "NullDetector",
    "create_detector",
    "is_nsfw_label",
]

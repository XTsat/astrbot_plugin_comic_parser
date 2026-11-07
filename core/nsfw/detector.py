"""NSFW 检测：把 YOLO 目标检测模型包成「给封面打分 + 给出可打码的框」的接口。

模型是**目标检测**而不是整图分类——它输出的就是 `anus / make_love / nipple / penis /
vagina` 这些部位的坐标，因此同一个结果既能算「露骨程度」，也能直接拿来打码。
没装 `ultralytics`、或 `models/` 里没有权重时，检测器保持不可用，调用方据此跳过评分与
局部打码，绝不会因此让下载失败。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from PIL import Image
from astrbot.api import logger

from ...domain.constants import (
    DEFAULT_NSFW_IMGSZ,
    DEFAULT_NSFW_THRESHOLD,
    NSFW_BOX_KEYWORDS,
    PLUGIN_NAME,
)
from .coverage import union_coverage
from .weights import ModelWeights


class _YoloModel(Protocol):
    """ultralytics 的 YOLO 实例只需要「能当函数调」。"""

    def __call__(self, source: str, **kwargs: object) -> Sequence[object]: ...


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


@dataclass(slots=True)
class NsfwDetector:
    """惰性加载的 YOLO 检测器；加载失败只记一次日志，之后保持不可用。"""

    weights: ModelWeights
    device: str = ""
    threshold: float = DEFAULT_NSFW_THRESHOLD
    # 推理输入尺寸：640 会在露骨页上吐出覆盖整幅的粗框，实测 960 才能框到具体部位
    imgsz: int = DEFAULT_NSFW_IMGSZ
    _model: _YoloModel | None = field(default=None, init=False, repr=False)
    _load_failed: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ---- 可用性 ----

    @property
    def model_path(self) -> Path | None:
        """当前能找到的权重文件（不触发下载、不加载模型）。"""
        return self.weights.local_model()

    @property
    def available(self) -> bool:
        """权重就位且推理依赖可用时为 True。

        用于在提示里区分「没配模型」和「模型算出来就是 0 分」这两种情况。
        """
        if self._model is not None:
            return True
        if self.model_path is None:
            return False
        try:
            import importlib.util

            return importlib.util.find_spec("ultralytics") is not None
        except (ImportError, ValueError):
            return False

    def reset(self) -> None:
        """丢掉已加载的模型，让下次调用重新读 `models/`（权重刚下载完时用）。"""
        with self._lock:
            self._model = None
            self._load_failed = False

    # ---- 推理 ----

    def analyze(self, image_path: Path, *, threshold: float | None = None) -> NsfwScore:
        """给单张图打分（封面用这个）。

        Args:
            image_path: 图片路径。
            threshold: 检测置信度阈值，None 表示用配置值。

        Returns:
            NsfwScore: 失败或不可用时返回全零结果，调用方可据 ``detections`` 判断。
        """
        model = self._ensure_model()
        if model is None:
            return NsfwScore.empty()

        detections = self._detect(model, image_path, threshold)
        if not detections:
            return NsfwScore.empty()

        width, height = self._image_size(image_path)
        coverage = self._coverage(detections, width, height)
        return NsfwScore(
            value=round(coverage * 100, 1),
            coverage=coverage,
            detections=tuple(detections),
        )

    # ---- 内部实现 ----

    def _ensure_model(self) -> _YoloModel | None:
        """加载模型（线程安全，只尝试一次）。"""
        if self._model is not None:
            return self._model
        if self._load_failed:
            return None

        with self._lock:
            if self._model is not None:
                return self._model
            if self._load_failed:
                return None

            model_path = self.weights.local_model()
            if model_path is None:
                logger.info(f"[{PLUGIN_NAME}] 没有可用的 NSFW 权重，跳过评分与局部打码")
                self._load_failed = True
                return None

            try:
                from ultralytics import YOLO  # 惰性 import：不用 YOLO 的部署不必装它
            except ImportError as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}] 未安装 ultralytics，无法做 NSFW 检测（{exc}）；"
                    "封面将按整张兜底打码"
                )
                self._load_failed = True
                return None

            try:
                self._model = YOLO(str(model_path))
            except Exception as exc:
                logger.error(f"[{PLUGIN_NAME}] 加载 NSFW 权重失败 {model_path}: {exc}")
                self._load_failed = True
                return None

            logger.info(f"[{PLUGIN_NAME}] NSFW 检测模型已加载: {model_path.name}")
            return self._model

    def _detect(
        self, model: _YoloModel, image_path: Path, threshold: float | None
    ) -> list[Detection]:
        """跑一次推理，只留下露骨部位。"""
        conf = self.threshold if threshold is None else threshold
        kwargs: dict[str, object] = {"verbose": False, "conf": conf, "imgsz": self.imgsz}
        if self.device:
            kwargs["device"] = self.device

        try:
            results = model(str(image_path), **kwargs)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] NSFW 检测失败 {Path(image_path).name}: {exc}")
            return []

        detections: list[Detection] = []
        for result in results or []:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            names = getattr(result, "names", {}) or {}
            for raw in boxes:
                try:
                    cls_id = int(raw.cls[0])
                    label = str(names.get(cls_id, "")).lower()
                    confidence = float(raw.conf[0])
                    coords = raw.xyxy[0]
                    box = (
                        float(coords[0]),
                        float(coords[1]),
                        float(coords[2]),
                        float(coords[3]),
                    )
                except (AttributeError, IndexError, TypeError, ValueError):
                    continue

                if not any(keyword in label for keyword in NSFW_BOX_KEYWORDS):
                    continue
                detections.append(
                    Detection(label=label, confidence=confidence, box=box)
                )
        return detections

    @staticmethod
    def _image_size(image_path: Path) -> tuple[int, int]:
        try:
            with Image.open(image_path) as image:
                return image.size
        except Exception:
            return (0, 0)

    @staticmethod
    def _coverage(detections: Sequence[Detection], width: int, height: int) -> float:
        """检出区域占整图的比例（并集，重叠只算一次）。

        用并集而不是面积相加：漫画页上同一部位常被连续检出多个重叠框，
        相加会把「几处小框」算成「整张都露骨」。
        """
        return union_coverage([item.box for item in detections], width, height)

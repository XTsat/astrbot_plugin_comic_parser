"""ONNX 后端：用 onnxruntime 跑 YOLO 检测，不引入深度学习框架。

只依赖 `onnxruntime`（约 200M）：实测 960 输入下 CPU 单张约 0.9 秒，够用且装得少。

预处理与后处理必须和 ultralytics 对齐，否则框会整体偏移。两个坑记在这里：
- 缩放要用「半像素中心 + 落回 uint8」的最近邻双线性（等价 cv2.INTER_LINEAR）；
  PIL 的 BILINEAR 会在缩小时抗锯齿，置信度能差 27%（实测 0.385 vs 0.304）。
- 输出是 ``(4 + 类别数, 候选数)``，前四列是 ``cx, cy, w, h``（输入尺寸下的像素坐标）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image
from astrbot.api import logger

from ...domain.constants import (
    DEFAULT_NSFW_IMGSZ,
    DEFAULT_NSFW_THRESHOLD,
    PLUGIN_NAME,
)
from .coverage import union_coverage
from .detector import Detection, NsfwScore, is_nsfw_label
from .weights import ModelWeights

# YOLO11 检测头的固定类别顺序（权重自带，导出前后一致）
DEFAULT_LABELS = ("anus", "make_love", "nipple", "penis", "vagina")

# NMS 的 IoU 门限与单图保留上限，与模型训练时的默认后处理一致
NMS_IOU = 0.45
MAX_DETECTIONS = 300
PAD_VALUE = 114


class _OnnxSession(Protocol):
    """onnxruntime 会话只需要这两件事。"""

    def run(self, output_names: object, input_feed: dict[str, np.ndarray]) -> list[np.ndarray]: ...

    def get_inputs(self) -> list[object]: ...


@dataclass(slots=True)
class OnnxDetector:
    """与 :class:`~core.nsfw.detector.NsfwDetector` 同接口的 ONNX 实现。"""

    weights: ModelWeights
    threshold: float = DEFAULT_NSFW_THRESHOLD
    imgsz: int = DEFAULT_NSFW_IMGSZ
    _session: _OnnxSession | None = field(default=None, init=False, repr=False)
    _load_failed: bool = field(default=False, init=False, repr=False)
    _imgsz_notice: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ---- 可用性 ----

    @property
    def model_path(self) -> Path | None:
        """当前能找到的 ONNX 权重（不加载会话）。"""
        path = self.weights.local_model()
        if path is None or path.suffix.lower() != ".onnx":
            return None
        return path

    @property
    def input_size(self) -> int | None:
        """权重声明的输入边长。

        本插件导出的 ONNX 是**固定尺寸**（如 ``[1, 3, 960, 960]``），此时除了这个边长之外
        的任何 ``imgsz`` 都会在推理时报 shape 不匹配，所以上层要么按它给，要么干脆不暴露调节；
        动态尺寸的导出件返回 ``None``。
        """
        session = self._ensure_session()
        if session is None:
            return None
        try:
            shape = session.get_inputs()[0].shape
        except (IndexError, AttributeError):
            return None
        height, width = (shape[2], shape[3]) if len(shape) >= 4 else (None, None)
        if isinstance(height, int) and isinstance(width, int) and height == width:
            return height
        return None

    @property
    def available(self) -> bool:
        if self._session is not None:
            return True
        if self.model_path is None:
            return False
        try:
            import importlib.util

            return importlib.util.find_spec("onnxruntime") is not None
        except (ImportError, ValueError):
            return False

    def reset(self) -> None:
        with self._lock:
            self._session = None
            self._load_failed = False

    # ---- 推理 ----

    def analyze(self, image_path: Path, *, threshold: float | None = None) -> NsfwScore:
        """给单张图打分，返回 :class:`NsfwScore`。"""
        session = self._ensure_session()
        if session is None:
            return NsfwScore.unavailable()

        conf = self.threshold if threshold is None else threshold
        try:
            blob, size, scale, pad = self._preprocess(image_path)
            raw = session.run(None, {self._input_name(session): blob})[0]
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] ONNX 推理失败 {Path(image_path).name}: {exc}")
            return NsfwScore.unavailable()

        detections = self.decode(raw, size, scale, pad, conf)
        if not detections:
            return NsfwScore.empty()

        coverage = union_coverage([item.box for item in detections], *size)
        return NsfwScore(
            value=round(coverage * 100, 1),
            coverage=coverage,
            detections=tuple(detections),
        )

    # ---- 预处理 / 后处理（可单测） ----

    def _effective_imgsz(self) -> int:
        """真正要喂给网络的边长。

        本插件导出的权重是固定尺寸（``[1, 3, 960, 960]``），把配置里的尺寸填成别的值会让
        session 直接报 shape 不匹配、整张封面按「检测失败」跳过——所以固定尺寸的权重一律
        按它自己声明的边长来，配置值只在动态尺寸的导出件上才作数。
        """
        fixed = self.input_size
        if fixed is not None and fixed != self.imgsz:
            if not self._imgsz_notice:
                self._imgsz_notice = True
                logger.warning(
                    f"[{PLUGIN_NAME}] 权重固定 {fixed} 输入，已忽略配置的推理尺寸 {self.imgsz}；"
                    "这项配置只对动态尺寸的权重有效"
                )
            return fixed
        return self.imgsz

    def _preprocess(self, image_path: Path) -> tuple[np.ndarray, tuple[int, int], float, tuple[int, int]]:
        """letterbox 到 imgsz×imgsz，返回 (NCHW blob, 原图尺寸, 缩放比, 左上填充)。"""
        with Image.open(image_path) as image:
            rgb = np.asarray(image.convert("RGB"))

        height, width = rgb.shape[:2]
        if width < 2 or height < 2:
            raise ValueError(f"图片尺寸异常: {width}x{height}")

        imgsz = self._effective_imgsz()
        scale = min(imgsz / width, imgsz / height)
        new_w, new_h = int(round(width * scale)), int(round(height * scale))
        canvas = np.full((imgsz, imgsz, 3), PAD_VALUE, dtype=np.uint8)
        pad_x, pad_y = (imgsz - new_w) // 2, (imgsz - new_h) // 2
        canvas[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = self._resize_bilinear(
            rgb, new_w, new_h
        )

        blob = canvas.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        return blob, (width, height), scale, (pad_x, pad_y)

    @staticmethod
    def _resize_bilinear(image: np.ndarray, new_w: int, new_h: int) -> np.ndarray:
        """复刻 cv2.INTER_LINEAR：半像素中心、无抗锯齿、结果落回 uint8。"""
        src_h, src_w = image.shape[:2]
        ys = np.clip(
            (np.arange(new_h, dtype=np.float32) + 0.5) * (src_h / new_h) - 0.5,
            0,
            src_h - 1,
        )
        xs = np.clip(
            (np.arange(new_w, dtype=np.float32) + 0.5) * (src_w / new_w) - 0.5,
            0,
            src_w - 1,
        )
        y0 = np.floor(ys).astype(np.int32)
        x0 = np.floor(xs).astype(np.int32)
        y1 = np.minimum(y0 + 1, src_h - 1)
        x1 = np.minimum(x0 + 1, src_w - 1)
        wy = (ys - y0)[:, None, None]
        wx = (xs - x0)[None, :, None]

        top = image[y0][:, x0] * (1 - wx) + image[y0][:, x1] * wx
        bottom = image[y1][:, x0] * (1 - wx) + image[y1][:, x1] * wx
        merged = top * (1 - wy) + bottom * wy
        return np.clip(np.rint(merged), 0, 255).astype(np.uint8)

    def decode(
        self,
        raw: np.ndarray,
        size: tuple[int, int],
        scale: float,
        pad: tuple[int, int],
        conf: float,
        labels: tuple[str, ...] = DEFAULT_LABELS,
    ) -> list[Detection]:
        """把网络输出解成原图坐标下的露骨部位框（含置信度过滤与 NMS）。"""
        width, height = size
        pad_x, pad_y = pad

        pred = np.asarray(raw)[0] if np.asarray(raw).ndim == 3 else np.asarray(raw)
        # 统一成 (候选数, 4+类别数)：通道维永远比候选数小得多，候选数极少的极端情况单独兜住
        if pred.shape[0] < pred.shape[1] or pred.shape[1] < 5:
            pred = pred.T

        if pred.shape[1] < 5:
            return []

        class_scores = pred[:, 4:]
        class_ids = class_scores.argmax(axis=1)
        confidences = class_scores[np.arange(len(class_ids)), class_ids]
        mask = confidences > conf
        if not mask.any():
            return []

        rows = pred[mask]
        confs = confidences[mask]
        ids = class_ids[mask]

        cx, cy, bw, bh = rows[:, 0], rows[:, 1], rows[:, 2], rows[:, 3]
        x1 = (cx - bw / 2 - pad_x) / scale
        y1 = (cy - bh / 2 - pad_y) / scale
        x2 = (cx + bw / 2 - pad_x) / scale
        y2 = (cy + bh / 2 - pad_y) / scale
        boxes = np.stack(
            [
                np.clip(x1, 0, width),
                np.clip(y1, 0, height),
                np.clip(x2, 0, width),
                np.clip(y2, 0, height),
            ],
            axis=1,
        )

        keep = self._nms(boxes, confs)
        detections: list[Detection] = []
        for index in keep[:MAX_DETECTIONS]:
            label = labels[int(ids[index])] if int(ids[index]) < len(labels) else ""
            if not is_nsfw_label(label):
                continue
            detections.append(
                Detection(
                    label=label,
                    confidence=float(confs[index]),
                    box=tuple(float(v) for v in boxes[index]),
                )
            )
        return detections

    @staticmethod
    def _nms(
        boxes: np.ndarray, scores: np.ndarray, iou_thr: float = NMS_IOU
    ) -> list[int]:
        """标准贪心 NMS（类别无关）：同一处被重复检出的框只留最高分的那个。"""
        order = scores.argsort()[::-1]
        keep: list[int] = []
        while order.size:
            current = int(order[0])
            keep.append(current)
            if order.size == 1:
                break

            rest = order[1:]
            xx1 = np.maximum(boxes[current, 0], boxes[rest, 0])
            yy1 = np.maximum(boxes[current, 1], boxes[rest, 1])
            xx2 = np.minimum(boxes[current, 2], boxes[rest, 2])
            yy2 = np.minimum(boxes[current, 3], boxes[rest, 3])
            inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)

            area_current = (boxes[current, 2] - boxes[current, 0]) * (
                boxes[current, 3] - boxes[current, 1]
            )
            area_rest = (boxes[rest, 2] - boxes[rest, 0]) * (
                boxes[rest, 3] - boxes[rest, 1]
            )
            union = np.maximum(area_current + area_rest - inter, 1e-9)
            order = rest[inter / union <= iou_thr]
        return keep

    # ---- 会话 ----

    def _ensure_session(self) -> _OnnxSession | None:
        if self._session is not None:
            return self._session
        if self._load_failed:
            return None

        with self._lock:
            if self._session is not None:
                return self._session
            if self._load_failed:
                return None

            model_path = self.model_path
            if model_path is None:
                logger.info(f"[{PLUGIN_NAME}] 没有可用的 ONNX 权重，跳过评分与局部打码")
                self._load_failed = True
                return None

            try:
                import onnxruntime

                session = onnxruntime.InferenceSession(
                    str(model_path), providers=["CPUExecutionProvider"]
                )
            except ImportError as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}] 未安装 onnxruntime，无法做 NSFW 检测（{exc}）；"
                    "请先 pip install onnxruntime"
                )
                self._load_failed = True
                return None
            except Exception as exc:
                logger.error(f"[{PLUGIN_NAME}] 加载 ONNX 权重失败 {model_path}: {exc}")
                self._load_failed = True
                return None

            self._session = session
            logger.info(f"[{PLUGIN_NAME}] ONNX 检测模型已加载: {model_path.name}")
            return session

    @staticmethod
    def _input_name(session: _OnnxSession) -> str:
        inputs = session.get_inputs()
        return str(getattr(inputs[0], "name", "images"))


def is_onnx(path: Path) -> bool:
    """权重是否为 ONNX 格式。"""
    return path.suffix.lower() == ".onnx"


__all__ = ["DEFAULT_LABELS", "OnnxDetector", "is_onnx"]

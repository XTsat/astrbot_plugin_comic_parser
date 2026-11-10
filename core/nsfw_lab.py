"""打码实验台的业务逻辑：暂存上传的图，按页面传来的参数跑评分与打码。

页面只关心两件事——「这张图现在几分」和「按这组参数糊完长什么样」，所以这里把链路做薄：
图片进一个有上限的内存暂存表，评分与打码**复用 :mod:`core.nsfw` 的真实实现**（与封面交付
完全同一套检测器与打码函数），推理丢线程里跑，不阻塞事件循环。

页面侧的参数（模型档位、阈值、输入尺寸、样式、块大小、外扩、整张阈值）一律由请求携带：
实验台存在的意义就是拿不同参数去试，所以不读也不写插件配置。
"""

from __future__ import annotations

import asyncio
import base64
import io
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image
from astrbot.api import logger

from ..domain.constants import (
    COVER_CENSOR_STYLE_LABELS,
    COVER_CENSOR_STYLES,
    COVER_MAX_BOXES,
    DEFAULT_COVER_CENSOR_BLOCK,
    DEFAULT_COVER_CENSOR_PADDING,
    DEFAULT_COVER_CENSOR_STYLE,
    DEFAULT_COVER_FULL_CENSOR_AREA,
    DEFAULT_NSFW_IMGSZ,
    DEFAULT_NSFW_MODEL_PRESET,
    DEFAULT_NSFW_THRESHOLD,
    IMAGE_SUFFIXES,
    NSFW_IMGSZ_RANGE,
    NSFW_LAB_IMGSZ_CHOICES,
    NSFW_LAB_MAX_IMAGES,
    NSFW_LAB_MAX_UPLOAD_BYTES,
    NSFW_MODEL_PRESETS,
    PLUGIN_NAME,
)
from ..domain.errors import LabError
from .nsfw import Detector, ModelWeights, create_detector, censor_cover


@dataclass(slots=True)
class LabImage:
    """一张上传到实验台的图：落成的临时文件 + 原始尺寸。"""

    id: str
    path: Path
    width: int
    height: int
    size: int
    # 最近一次打码结果：页面点「下载」时直接发它，不用再把框重传一遍
    censored_path: Path | None = None
    censored_full: bool = False


class NsfwLabService:
    """把插件的检测器与打码函数包成页面能用的四个动作。

    Args:
        models_dir: 权重目录（与封面打码共用，插件目录下的 ``models/``）。
        temp_root: 插件的临时目录根，实验台自己在里面开一个子目录。
        preset: 默认档位，页面首次打开时的初始值。
        threshold: 默认置信度阈值，同上。
        imgsz: 默认推理输入尺寸，同上。
        style / block / padding / full_area: 默认打码参数，同上。
    """

    def __init__(
        self,
        models_dir: Path,
        temp_root: Path,
        *,
        preset: str = DEFAULT_NSFW_MODEL_PRESET,
        threshold: float = DEFAULT_NSFW_THRESHOLD,
        imgsz: int = DEFAULT_NSFW_IMGSZ,
        style: str = DEFAULT_COVER_CENSOR_STYLE,
        block: int = DEFAULT_COVER_CENSOR_BLOCK,
        padding: int = DEFAULT_COVER_CENSOR_PADDING,
        full_area: float = DEFAULT_COVER_FULL_CENSOR_AREA,
        auto_download: bool = True,
        download_url: str = "",
        proxy: str = "",
    ) -> None:
        self._models_dir = Path(models_dir)
        self._temp_dir = Path(temp_root) / "nsfw_lab"
        self._ensure_temp_dir()
        self._defaults = {
            "preset": preset,
            "threshold": threshold,
            "imgsz": imgsz,
            "style": style,
            "block": block,
            "padding": padding,
            "full_area": full_area,
        }
        # 页面上的「下载缺失档位」复用插件配置里的下载地址 / 代理 / 自定源
        self._download_url = download_url or ""
        self._proxy = proxy or ""
        self._auto_download = auto_download
        # 一次只允许一个下载：几十 MB 的文件并行下只会互相抢带宽
        self._prepare_task: asyncio.Task[None] | None = None
        self._prepare_state: dict[str, Any] = {
            "state": "idle",  # idle / downloading / ready / error
            "preset": "",
            "percent": 0.0,
            "error": "",
        }
        # 同一个检测器会话不能并发跑：整段「改输入尺寸 + 推理」都在这把锁里
        self._infer_lock = threading.Lock()
        self._detectors: dict[str, Detector] = {}
        self._images: OrderedDict[str, LabImage] = OrderedDict()
        self._images_lock = threading.Lock()

    # ---- 元信息 ----

    def presets(self) -> list[dict[str, Any]]:
        """全部档位 + 是否已下载。

        页面要把「有 nano/small/int8 可以换」这件事摆出来，所以未下载的档位也要列，
        只是标成 ``available: False``，由页面给一个下载入口。
        """
        items: list[dict[str, Any]] = []
        for preset, spec in NSFW_MODEL_PRESETS.items():
            path = self._models_dir / spec.filename
            items.append(
                {
                    "value": preset,
                    "label": spec.summary,
                    "file": spec.filename,
                    "available": path.is_file(),
                    "bytes": path.stat().st_size if path.is_file() else 0,
                }
            )
        return items

    def describe(self) -> dict[str, Any]:
        """页面首屏需要的全部元信息：可用档位、默认值、样式清单、输入尺寸是否可调。"""
        return {
            "models_dir": str(self._models_dir),
            "presets": self.presets(),
            "runtime_ready": _onnxruntime_ready(),
            "imgsz_fixed": self._fixed_input_size(),
            "auto_download": self._auto_download,
            "download": dict(self._prepare_state),
            "defaults": dict(self._defaults),
            "styles": [
                {"value": style, "label": COVER_CENSOR_STYLE_LABELS.get(style, style)}
                for style in COVER_CENSOR_STYLES
            ],
            "imgsz_choices": list(NSFW_LAB_IMGSZ_CHOICES),
            "imgsz_range": list(NSFW_IMGSZ_RANGE),
            "max_upload_bytes": NSFW_LAB_MAX_UPLOAD_BYTES,
            "max_boxes": COVER_MAX_BOXES,
        }

    # ---- 图片暂存 ----

    def add_image(self, raw: bytes, suffix: str) -> LabImage:
        """把上传的字节落成临时文件并登记；超出上限时挤掉最早的一张。"""
        if not raw:
            raise LabError("没有收到图片内容")
        if len(raw) > NSFW_LAB_MAX_UPLOAD_BYTES:
            raise LabError(
                f"图片超过 {NSFW_LAB_MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限，换个小的试"
            )

        try:
            with Image.open(_BytesReader(raw)) as probe:
                width, height = probe.size
                probe.verify()
        except Exception as exc:  # PIL 对损坏文件抛的异常类型很杂
            raise LabError(f"不是能识别的图片：{exc}") from exc

        suffix = suffix if suffix in IMAGE_SUFFIXES else ".jpg"
        image_id = uuid.uuid4().hex
        path = self._ensure_temp_dir() / f"{image_id}{suffix}"
        path.write_bytes(raw)

        item = LabImage(
            id=image_id, path=path, width=width, height=height, size=len(raw)
        )
        with self._images_lock:
            self._images[image_id] = item
            while len(self._images) > NSFW_LAB_MAX_IMAGES:
                _, dropped = self._images.popitem(last=False)
                self._remove_files(dropped)
        return item

    def get_image(self, image_id: str) -> LabImage:
        with self._images_lock:
            item = self._images.get(image_id)
        if item is None:
            raise LabError("这张图已经过期（插件重载或缓存被挤掉了），请重新上传")
        return item

    def download_path(self, image_id: str) -> tuple[Path, str]:
        """要下载的那张图：有打码结果就给打码结果，否则给原图。"""
        item = self.get_image(image_id)
        path = item.censored_path if item.censored_path is not None else item.path
        stem = "censored" if item.censored_path is not None else "original"
        return path, f"cover_{stem}{path.suffix.lower()}"

    def clear(self) -> int:
        """清空暂存（插件卸载/重载时调用）。"""
        with self._images_lock:
            dropped = list(self._images.values())
            self._images.clear()
        for item in dropped:
            self._remove_files(item)
        return len(dropped)

    # ---- 权重下载 ----

    async def prepare_model(self, preset: Any) -> dict[str, Any]:
        """把某个档位的权重下下来（后台跑），页面用 :meth:`model_status` 轮询进度。

        下载源、代理、自定义地址全部沿用插件配置：页面上换档位不该绕过用户的网络设置。

        Args:
            preset: 档位名；未知值退回默认档。

        Returns:
            dict: 当前下载状态快照。
        """
        preset = preset if preset in NSFW_MODEL_PRESETS else DEFAULT_NSFW_MODEL_PRESET
        filename = NSFW_MODEL_PRESETS[preset].filename
        if (self._models_dir / filename).is_file():
            self._prepare_state = _prepare_snapshot("ready", preset, 1.0, "")
            return dict(self._prepare_state)

        if not self._auto_download:
            raise LabError(
                "插件设置里关掉了「缺失时自动下载权重」；去「封面与模型 → 模型与检测」打开它，"
                "或手动把 .onnx 放进插件目录的 models/"
            )

        if self._prepare_task is not None and not self._prepare_task.done():
            busy = self._prepare_state.get("preset") or "其它档位"
            raise LabError(f"正在下载 {busy}，等它下完再换档位")

        self._prepare_state = _prepare_snapshot("downloading", preset, 0.0, "")
        self._prepare_task = asyncio.create_task(self._download_model(preset))
        return dict(self._prepare_state)

    def model_status(self) -> dict[str, Any]:
        """当前下载状态 + 各档位是否就位（页面按秒轮询）。"""
        return {**self._prepare_state, "presets": self.presets()}

    async def _download_model(self, preset: str) -> None:
        """后台任务：真正下载权重，结果写回 _prepare_state。"""
        weights = ModelWeights(
            self._models_dir,
            preset=preset,
            auto_download=True,
            download_url=self._download_url,
            proxy=self._proxy,
            on_progress=self._note_progress,
        )
        try:
            path, _ = await weights.ensure(require_preset=True)
        except Exception as exc:  # aiohttp 的异常类型很杂，这里统一收口
            logger.warning(f"[{PLUGIN_NAME}] 打码实验台下载权重失败 {preset}: {exc}")
            self._prepare_state = _prepare_snapshot("error", preset, 0.0, str(exc))
            return

        if path is None:
            self._prepare_state = _prepare_snapshot(
                "error",
                preset,
                0.0,
                "所有下载源都失败了：可在「封面与模型 → 模型与检测 → 自定义下载地址」填镜像，"
                "或手动把 .onnx 放进插件目录的 models/",
            )
            return

        # 之前可能缓存过「没有权重」时的检测器，换掉它才能真正用上新档位
        self._detectors.pop(preset, None)
        self._prepare_state = _prepare_snapshot("ready", preset, 1.0, "")
        logger.info(f"[{PLUGIN_NAME}] 打码实验台权重就绪：{path.name}")

    def _note_progress(self, ratio: float) -> None:
        if self._prepare_state.get("state") == "downloading":
            self._prepare_state["percent"] = round(max(0.0, min(1.0, ratio)), 3)

    # ---- 评分 ----

    async def score(
        self,
        image_id: str,
        *,
        preset: str,
        threshold: float,
        imgsz: int,
    ) -> dict[str, Any]:
        """给一张图打分：返回 0-100 的露骨指数与逐处检出。"""
        item = self.get_image(image_id)
        preset = preset if preset in NSFW_MODEL_PRESETS else DEFAULT_NSFW_MODEL_PRESET
        threshold = _clamp_float(threshold, 0.0, 0.95, DEFAULT_NSFW_THRESHOLD)
        fixed = self._fixed_input_size(preset)
        requested_imgsz = imgsz
        if fixed is not None:
            # 权重是固定尺寸导出件：别的边长会直接推理失败，页面传什么都得按它来
            imgsz = fixed
        else:
            low, high = NSFW_IMGSZ_RANGE
            imgsz = int(_clamp_float(imgsz, low, high, DEFAULT_NSFW_IMGSZ))

        started = time.perf_counter()
        score = await asyncio.to_thread(self._analyze, item.path, preset, threshold, imgsz)
        elapsed_ms = (time.perf_counter() - started) * 1000

        if score.failed:
            raise LabError(
                "检测没跑起来：缺 onnxruntime 或权重读不出来，看插件日志；"
                "页面仍可以先试打码样式"
            )

        return {
            "image_id": image_id,
            "width": item.width,
            "height": item.height,
            "preset": preset,
            "threshold": threshold,
            "imgsz": imgsz,
            # 权重写死输入尺寸时，页面要能说清「你选的值没被采用」
            "imgsz_fixed": fixed,
            "imgsz_ignored": fixed is not None and int(_clamp_float(requested_imgsz, 0, 65535, 0)) != fixed,
            "score": score.value,
            "coverage": round(score.coverage, 4),
            "labels": list(score.labels),
            "cost_ms": round(elapsed_ms, 1),
            "detections": [
                {
                    "label": detection.label,
                    "confidence": round(detection.confidence, 4),
                    "box": [round(float(value), 1) for value in detection.box],
                }
                for detection in score.detections
            ],
        }

    def _analyze(self, path: Path, preset: str, threshold: float, imgsz: int):
        """线程里跑：取检测器 → 改输入尺寸 → 推理，整段持锁。"""
        with self._infer_lock:
            detector = self._detector(preset)
            # 输入尺寸是检测器上的可变字段，必须在锁内改，否则两张图并发时会互相串
            detector.imgsz = imgsz
            return detector.analyze(path, threshold=threshold)

    def _fixed_input_size(self, preset: str | None = None) -> int | None:
        """权重写死的输入边长；取不到（没权重/没依赖）时返回 None，页面就退回可调下拉。"""
        try:
            detector = self._detector(preset or self._defaults["preset"])
        except LabError:
            return None
        with self._infer_lock:
            return detector.input_size

    def _detector(self, preset: str) -> Detector:
        detector = self._detectors.get(preset)
        if detector is not None:
            return detector

        weights = ModelWeights(self._models_dir, preset=preset, auto_download=False)
        # 必须严格按档位取：local_model() 会拿别的档位顶替，页面上的「档位」就成了摆设
        model_path = weights.model_for_preset()
        if model_path is None:
            raise LabError(
                f"模型目录里没有 {preset} 的权重；在插件设置里打开「给封面打码」让它自动下载，"
                "或把 .onnx 放进插件的 models/ 目录后重载插件"
            )

        detector = create_detector(
            weights, threshold=DEFAULT_NSFW_THRESHOLD, imgsz=DEFAULT_NSFW_IMGSZ
        )
        self._detectors[preset] = detector
        logger.info(f"[{PLUGIN_NAME}] 打码实验台加载权重：{model_path.name}")
        return detector

    # ---- 打码 ----

    async def censor(
        self,
        image_id: str,
        *,
        boxes: Any = None,
        style: Any = None,
        block: Any = None,
        padding: Any = None,
        full_area: Any = None,
    ) -> dict[str, Any]:
        """按给定框与样式糊图，结果另存一份并记在该图上供下载。

        Args:
            image_id: 上传时拿到的图 id。
            boxes: 检测框列表，元素是 ``(x1, y1, x2, y2)``；脏值逐项丢掉。
            style: 打码样式；未知值退回马赛克。
            block: 马赛克块大小（模糊半径按它换算）。
            padding: 每个框向外扩的像素。
            full_area: 检出并集达到该比例就整张打码，``<= 0`` 关闭。

        Returns:
            dict: 打码后图片的 data URL 与覆盖率等结论。
        """
        item = self.get_image(image_id)
        style = style if style in COVER_CENSOR_STYLES else DEFAULT_COVER_CENSOR_STYLE
        block = int(_clamp_float(block, 2, 128, DEFAULT_COVER_CENSOR_BLOCK))
        padding = int(_clamp_float(padding, 0, 256, DEFAULT_COVER_CENSOR_PADDING))
        full_area = _clamp_float(full_area, 0.0, 1.0, DEFAULT_COVER_FULL_CENSOR_AREA)
        clean_boxes = [
            [float(value) for value in candidate[:4]]
            for candidate in (boxes if isinstance(boxes, list) else [])
            if isinstance(candidate, (list, tuple)) and len(candidate) >= 4
        ][:COVER_MAX_BOXES]

        target = self._ensure_temp_dir() / f"{item.id}_censored{item.path.suffix.lower()}"
        changed, coverage = await asyncio.to_thread(
            self._apply_censor, item.path, target, clean_boxes, style, block, padding, full_area
        )

        item.censored_path = target if changed else None
        item.censored_full = bool(changed and full_area > 0 and coverage >= full_area)
        self._drop_other_censored(item, keep=item.censored_path)

        payload: dict[str, Any] = {
            "image_id": image_id,
            "changed": changed,
            "coverage": round(coverage, 4),
            "full": item.censored_full,
            "highlight_label": "整张打码" if item.censored_full else ("局部打码" if changed else "未改动"),
        }
        if changed:
            payload["image"] = _data_url(target)
            payload["mime"] = "image/jpeg" if target.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        return payload

    def _apply_censor(
        self,
        source: Path,
        target: Path,
        boxes: list[list[float]],
        style: str,
        block: int,
        padding: int,
        full_area: float,
    ) -> tuple[bool, float]:
        """线程里跑：打码函数就地覆盖文件，所以先复制一份到 target 再糊。"""
        target.write_bytes(source.read_bytes())
        try:
            return censor_cover(
                target,
                boxes,
                style=style,
                block=block,
                padding=padding,
                full_area=full_area,
            )
        except Exception:
            target.unlink(missing_ok=True)
            raise

    def close(self) -> None:
        """插件卸载：停掉在跑的下载，清干净暂存文件。"""
        if self._prepare_task is not None and not self._prepare_task.done():
            self._prepare_task.cancel()
            self._prepare_task = None
        self.clear()
        self._detectors.clear()

    # ---- 内部 ----

    def _ensure_temp_dir(self) -> Path:
        """每次写之前确认目录在。

        插件的 ``terminate()`` 会清空整个 temp/ 目录，而重载时新实例可能先于旧实例收尾：
        只在 __init__ 里建一次的话，新实例手里的目录会被旧实例的清理顺手删掉，
        第一张图就会写失败（FileNotFoundError）。
        """
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        return self._temp_dir

    def _drop_other_censored(self, item: LabImage, *, keep: Path | None) -> None:
        """同一张图只留最新一份打码结果，旧的删掉。"""
        for stale in self._temp_dir.glob(f"{item.id}_censored.*"):
            if keep is not None and stale == keep:
                continue
            stale.unlink(missing_ok=True)

    @staticmethod
    def _remove_files(item: LabImage) -> None:
        for path in (item.path, item.censored_path):
            if path is not None:
                path.unlink(missing_ok=True)


class _BytesReader:
    """PIL 只认文件对象，这里把 bytes 包成最小可读对象。"""

    def __init__(self, raw: bytes) -> None:
        self._buffer = io.BytesIO(raw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._buffer, name)


def _prepare_snapshot(state: str, preset: str, percent: float, error: str) -> dict[str, Any]:
    return {"state": state, "preset": preset, "percent": percent, "error": error}


def _clamp_float(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(high, max(low, number))


def _data_url(path: Path) -> str:
    mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _onnxruntime_ready() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("onnxruntime") is not None
    except (ImportError, ValueError):
        return False


__all__ = ["LabImage", "NsfwLabService"]

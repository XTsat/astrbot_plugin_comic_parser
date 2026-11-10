"""插件面板（漫画下载器面板）的业务逻辑：暂存上传的图，按页面传来的参数跑评分与打码。

页面只关心两件事——「这张图现在几分」和「按这组参数糊完长什么样」，所以这里把链路做薄：
图片进一个有上限的内存暂存表，评分与打码**复用 :mod:`core.nsfw` 的真实实现**（与封面交付
完全同一套检测器与打码函数），推理丢线程里跑，不阻塞事件循环。

页面侧的参数（模型档位、阈值、输入尺寸、样式、块大小、外扩、整张阈值）一律由请求携带：
「打码测试」页存在的意义就是拿不同参数去试，所以不读也不写插件配置。
"""

from __future__ import annotations

import asyncio
import base64
import io
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
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
    PANEL_IMGSZ_CHOICES,
    PANEL_MAX_IMAGES,
    PANEL_MAX_UPLOAD_BYTES,
    NSFW_MODEL_PRESETS,
    PLUGIN_NAME,
)
from ..domain.errors import PanelError
from .nsfw import Detector, ModelWeights, create_detector, censor_cover
from .registry import resolve_link


@dataclass(slots=True)
class PanelImage:
    """一张上传到面板的图：落成的临时文件 + 原始尺寸。"""

    id: str
    path: Path
    width: int
    height: int
    size: int
    # 最近一次打码结果：页面点「下载」时直接发它，不用再把框重传一遍
    censored_path: Path | None = None
    censored_full: bool = False


class PanelService:
    """把插件的检测器与打码函数包成页面能用的四个动作。

    Args:
        models_dir: 权重目录（与封面打码共用，插件目录下的 ``models/``）。
        temp_root: 插件的临时目录根，面板自己在里面开一个子目录。
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
        plugin_info: Mapping[str, str] | None = None,
        channels_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._models_dir = Path(models_dir)
        self._temp_dir = Path(temp_root) / "panel"
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
        # 面板顶栏与「状态」页要显示的插件信息（名称、版本、数据目录）
        self._plugin_info = dict(plugin_info or {})
        # 渠道对象由上层注入：面板只管展示与「拉一张封面」，不自己搭渠道
        self._channels_provider = channels_provider
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
        self._images: OrderedDict[str, PanelImage] = OrderedDict()
        self._images_lock = threading.Lock()

    # ---- 元信息 ----

    @property
    def models_dir(self) -> Path:
        """权重目录（面板展示用，只读）。"""
        return self._models_dir

    def presets(self) -> list[dict[str, Any]]:
        """全部档位 + 是否已安装。

        「模型下载」页要把没装的档位也列出来（否则用户不知道有哪些可选），
        而「打码测试」页的模型下拉只用其中的 ``installed == True`` 那些。
        """
        items: list[dict[str, Any]] = []
        for preset, spec in NSFW_MODEL_PRESETS.items():
            path = self._models_dir / spec.filename
            installed = path.is_file()
            items.append(
                {
                    "value": preset,
                    "label": spec.summary,
                    "file": spec.filename,
                    "task": spec.task,
                    "labels": list(spec.labels),
                    "installed": installed,
                    "available": installed,
                    "bytes": path.stat().st_size if installed else 0,
                }
            )
        return items

    def models(self) -> list[dict[str, Any]]:
        """「模型下载」页要的详细清单：类别、大小、来源、许可、装没装。"""
        downloading = self._prepare_state.get("preset") if self._prepare_state.get("state") == "downloading" else ""
        items: list[dict[str, Any]] = []
        for preset, spec in NSFW_MODEL_PRESETS.items():
            path = self._models_dir / spec.filename
            installed = path.is_file()
            size = 0
            if installed:
                try:
                    size = path.stat().st_size
                except OSError:
                    installed = False
            items.append(
                {
                    "value": preset,
                    "label": spec.summary,
                    "file": spec.filename,
                    "family": spec.family,
                    "task": spec.task,
                    "labels": list(spec.labels),
                    "license": spec.license,
                    "upstream": spec.upstream,
                    "note": spec.note,
                    "installed": installed,
                    "bytes": size,
                    "approx_bytes": spec.approx_bytes,
                    "downloading": downloading == preset,
                    "current": preset == self._defaults.get("preset"),
                    "sources": list(spec.sources) or ["插件 Release（tag 见 README）"],
                }
            )
        return items

    async def fetch_from_channel(self, channel_name: Any, text: Any) -> dict[str, Any]:
        """按渠道取一本作品的元信息与封面，封面直接进面板的图片暂存表。

        Args:
            channel_name: 渠道全称或简写（``nhentai`` / ``nh``）。
            text: 作品 ID 或作品链接；链接指向别的站点时按链接那家走。

        Returns:
            dict: ``comic`` 是元信息，``image`` 是封面（取不到时为 None，``cover_error``
            说明原因）。
        """
        raw = str(text or "").strip()
        if not raw:
            raise PanelError("先填作品 ID 或作品链接")

        channels = self._channel_objects()
        if not channels:
            raise PanelError("当前没有可用渠道")

        channel = _pick_channel(channels, str(channel_name or ""))
        if channel is None:
            raise PanelError(f"没有这个渠道：{channel_name}")

        # 贴的是作品链接就按链接走：命令/渠道只是入口，不能拿 A 站的 ID 去问 B 站
        if raw.lower().startswith(("http://", "https://")):
            resolved = resolve_link(channels, raw)
            if resolved is None:
                raise PanelError("认不出这条链接属于哪个渠道，检查一下链接或改用作品 ID")
            channel, comic_id = resolved
        else:
            try:
                comic_id = channel.normalize_id(raw)
            except Exception as exc:
                raise PanelError(f"{channel.display_command} 的作品 ID 看着不对：{exc}") from exc

        if not channel.enabled:
            raise PanelError(
                f"渠道 {channel.display_command} 没有启用；先在插件设置里打开它"
            )

        work_dir = self._ensure_temp_dir() / "channel" / uuid.uuid4().hex
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            meta = await channel.fetch_meta(comic_id, work_dir)
        except PanelError:
            raise
        except Exception as exc:  # 各渠道抛的异常类型不统一，这里统一变成页面文案
            logger.warning(f"[{PLUGIN_NAME}] 面板取元信息失败 {channel.command}/{comic_id}: {exc}")
            raise PanelError(f"{channel.display_command} 取元信息失败：{exc}") from exc

        payload: dict[str, Any] = {
            "channel": channel.command,
            "display_command": channel.display_command,
            "comic": {
                "channel": meta.channel,
                "comic_id": meta.comic_id,
                "title": meta.title,
                "author": meta.author,
                "page_count": meta.page_count,
                "tags": list(meta.tags),
            },
            "image": None,
            "cover_error": "",
        }

        if not channel.supports_cover:
            payload["cover_error"] = (
                f"渠道 {channel.display_command} 只支持查元信息，取不到封面预览；可以直接上传图片试"
            )
            return payload

        try:
            raw_cover = await channel.fetch_cover(comic_id)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 面板取封面失败 {channel.command}/{comic_id}: {exc}")
            raw_cover = None

        if not raw_cover:
            payload["cover_error"] = (
                f"没取到 {channel.display_command} 的封面：可能被站点风控、站点结构有变或网络不通，"
                "详情看插件日志；可以直接上传图片试"
            )
            return payload

        item = self.add_image(raw_cover, _image_suffix(raw_cover))
        payload["image"] = {
            "image_id": item.id,
            "width": item.width,
            "height": item.height,
            "bytes": item.size,
            "data_url": _data_url(item.path),
        }
        return payload

    def _channel_objects(self) -> dict[str, Any]:
        """渠道对象表（键是渠道名）；没有注入时返回空表。"""
        if self._channels_provider is None:
            return {}
        try:
            return dict(self._channels_provider())
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 面板取渠道对象失败: {exc}")
            return {}

    def channels(self) -> list[dict[str, Any]]:
        """可用渠道清单：面板的「渠道测试」与测试页的「拉封面」都靠它。"""
        if self._channels_provider is None:
            return []
        try:
            channels = self._channels_provider()
        except Exception as exc:  # 渠道构建失败不该把整个面板拖垮
            logger.warning(f"[{PLUGIN_NAME}] 面板读渠道清单失败: {exc}")
            return []

        items: list[dict[str, Any]] = []
        for name, channel in channels.items():
            items.append(
                {
                    "name": name,
                    "command": getattr(channel, "command", name),
                    "display_command": getattr(channel, "display_command", name),
                    "enabled": bool(getattr(channel, "enabled", False)),
                    "supports_search": bool(getattr(channel, "supports_search", False)),
                    "supports_cover": bool(getattr(channel, "supports_cover", False)),
                }
            )
        items.sort(key=lambda item: (not item["enabled"], item["command"]))
        return items

    def describe(self) -> dict[str, Any]:
        """页面首屏需要的全部元信息：档位、默认值、样式、运行时与渠道清单。"""
        presets = self.presets()
        return {
            "plugin": self._plugin_info,
            "runtime": _runtime_info(),
            "channels": self.channels(),
            "installed": [item for item in presets if item["installed"]],
            "models_dir": str(self._models_dir),
            "presets": presets,
            "runtime_ready": _onnxruntime_ready(),
            "imgsz_fixed": self._fixed_input_size(),
            "auto_download": self._auto_download,
            "download": dict(self._prepare_state),
            "defaults": dict(self._defaults),
            "styles": [
                {"value": style, "label": COVER_CENSOR_STYLE_LABELS.get(style, style)}
                for style in COVER_CENSOR_STYLES
            ],
            "imgsz_choices": list(PANEL_IMGSZ_CHOICES),
            "imgsz_range": list(NSFW_IMGSZ_RANGE),
            "max_upload_bytes": PANEL_MAX_UPLOAD_BYTES,
            "max_boxes": COVER_MAX_BOXES,
        }

    # ---- 图片暂存 ----

    def add_image(self, raw: bytes, suffix: str) -> PanelImage:
        """把上传的字节落成临时文件并登记；超出上限时挤掉最早的一张。"""
        if not raw:
            raise PanelError("没有收到图片内容")
        if len(raw) > PANEL_MAX_UPLOAD_BYTES:
            raise PanelError(
                f"图片超过 {PANEL_MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限，换个小的试"
            )

        try:
            with Image.open(_BytesReader(raw)) as probe:
                width, height = probe.size
                probe.verify()
        except Exception as exc:  # PIL 对损坏文件抛的异常类型很杂
            raise PanelError(f"不是能识别的图片：{exc}") from exc

        suffix = suffix if suffix in IMAGE_SUFFIXES else ".jpg"
        image_id = uuid.uuid4().hex
        path = self._ensure_temp_dir() / f"{image_id}{suffix}"
        path.write_bytes(raw)

        item = PanelImage(
            id=image_id, path=path, width=width, height=height, size=len(raw)
        )
        with self._images_lock:
            self._images[image_id] = item
            while len(self._images) > PANEL_MAX_IMAGES:
                _, dropped = self._images.popitem(last=False)
                self._remove_files(dropped)
        return item

    def get_image(self, image_id: str) -> PanelImage:
        with self._images_lock:
            item = self._images.get(image_id)
        if item is None:
            raise PanelError("这张图已经过期（插件重载或缓存被挤掉了），请重新上传")
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

    async def download_model(self, preset: Any) -> dict[str, Any]:
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
            raise PanelError(
                "插件设置里关掉了「缺失时自动下载权重」；去「封面与模型 → 模型与检测」打开它，"
                "或手动把 .onnx 放进插件目录的 models/"
            )

        if self._prepare_task is not None and not self._prepare_task.done():
            busy = self._prepare_state.get("preset") or "其它档位"
            raise PanelError(f"正在下载 {busy}，等它下完再换档位")

        self._prepare_state = _prepare_snapshot("downloading", preset, 0.0, "")
        self._prepare_task = asyncio.create_task(self._download_model(preset))
        return dict(self._prepare_state)

    def model_status(self) -> dict[str, Any]:
        """当前下载进度 + 各档位是否已装（「模型下载」页按秒轮询）。"""
        return {**self._prepare_state, "models": self.models()}

    def delete_model(self, preset: Any) -> dict[str, Any]:
        """删掉某个档位已下载的权重。

        只删预设表里认识的文件名：面板传进来的值一律按档位名收敛，不做路径拼接，
        免得把 ``models/`` 以外的东西删掉。
        """
        key = str(preset or "").strip().lower()
        if key not in NSFW_MODEL_PRESETS:
            raise PanelError(f"没有这个模型档位：{preset}")
        spec = NSFW_MODEL_PRESETS[key]
        target = self._models_dir / spec.filename
        part = target.with_name(spec.filename + ".part")

        removed = False
        for path in (target, part):
            try:
                if path.is_file():
                    path.unlink()
                    removed = True
            except OSError as exc:
                raise PanelError(f"删不掉 {path.name}：{exc}") from exc

        # 删掉的档位如果正被缓存着，检测器得一起丢掉，否则还会拿它跑
        self._detectors.pop(key, None)
        if key == self._prepare_state.get("preset"):
            self._prepare_state = _prepare_snapshot("idle", "", 0.0, "")
        logger.info(f"[{PLUGIN_NAME}] 面板删除权重 {spec.filename}（{'已删' if removed else '本来就没有'}）")
        return {"removed": removed, "models": self.models()}

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
            logger.warning(f"[{PLUGIN_NAME}] 面板下载权重失败 {preset}: {exc}")
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
        logger.info(f"[{PLUGIN_NAME}] 面板权重就绪：{path.name}")

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
            raise PanelError(
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
        except PanelError:
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
            raise PanelError(
                f"模型目录里没有 {preset} 的权重；在插件设置里打开「给封面打码」让它自动下载，"
                "或把 .onnx 放进插件的 models/ 目录后重载插件"
            )

        detector = create_detector(
            weights, threshold=DEFAULT_NSFW_THRESHOLD, imgsz=DEFAULT_NSFW_IMGSZ
        )
        self._detectors[preset] = detector
        logger.info(f"[{PLUGIN_NAME}] 面板测试页加载权重：{model_path.name}")
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

    def _drop_other_censored(self, item: PanelImage, *, keep: Path | None) -> None:
        """同一张图只留最新一份打码结果，旧的删掉。"""
        for stale in self._temp_dir.glob(f"{item.id}_censored.*"):
            if keep is not None and stale == keep:
                continue
            stale.unlink(missing_ok=True)

    @staticmethod
    def _remove_files(item: PanelImage) -> None:
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


def _pick_channel(channels: Mapping[str, Any], name: str) -> Any | None:
    """按全称、简写或渠道名找到渠道对象。"""
    wanted = name.strip().lower()
    if not wanted:
        return None
    for key, channel in channels.items():
        if wanted in {str(key).lower(), str(getattr(channel, "command", "")).lower()}:
            return channel
        names = {str(item).lower() for item in getattr(channel, "command_names", ())}
        aliases = {str(item).lower() for item in getattr(channel, "command_aliases", ())}
        if wanted in names or wanted in aliases:
            return channel
    return None


def _image_suffix(raw: bytes) -> str:
    """按文件头猜图片后缀：渠道给的字节没有文件名，只能看魔数。"""
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".jpg"


def _runtime_info() -> dict[str, Any]:
    """检测运行时的可用性与版本：顶栏徽章与「状态」页共用。"""
    ok = _onnxruntime_ready()
    version = ""
    error = ""
    if ok:
        try:
            import onnxruntime

            version = str(getattr(onnxruntime, "__version__", ""))
        except Exception as exc:  # 装上了但导入失败（缺系统库等）
            ok = False
            error = f"onnxruntime 导入失败：{exc}"
    else:
        error = "未安装 onnxruntime（开启打码后插件会自动补装）"
    return {"ready": ok, "version": version, "error": error}


def _onnxruntime_ready() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("onnxruntime") is not None
    except (ImportError, ValueError):
        return False


__all__ = ["PanelImage", "PanelService"]

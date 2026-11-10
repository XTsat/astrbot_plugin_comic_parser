"""NSFW 模型权重的定位与自动下载。

插件不内置权重：几十 MB 的二进制不适合进 git，用不到的人也不该被迫下载，
因此默认关闭、由配置开关按需拉取。运行时只认 `.onnx`。

现有两家的权重，各自的上游与下载源都写在预设里（见 `domain/constants.py`）：

- `EraX-NSFW-V1.0`（Apache-2.0）的 ONNX 导出件：本插件从上游 `.pt` 导出
  （固定 960 输入、opset 17），int8 档额外量化，因此与上游文件不是同一份；
- `deepghs/anime_censor_detection`（仓库标签 MIT）：作者直接提供 ONNX，
  纯二次元 / 同人图训练，镜像源在国内的可达性比 HuggingFace 源站好得多。

「从哪下载」因此按预设走，而不是全球一个地址：换模型时上游可能整个换掉。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import aiohttp
from astrbot.api import logger

from ...domain.constants import (
    DEFAULT_NSFW_MODEL_PRESET,
    NSFW_DOWNLOAD_CONNECT_TIMEOUT,
    NSFW_DOWNLOAD_READ_TIMEOUT,
    NSFW_ERAX_SOURCES,
    NSFW_MIN_MODEL_BYTES,
    NSFW_MODEL_DOWNLOAD_TIMEOUT,
    NSFW_MODEL_PRESETS,
    PLUGIN_NAME,
    NsfwModelPreset,
)

# 下载进度只在跨过这些比例时打一条日志，避免每个分片都刷屏
_PROGRESS_STEPS: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)

# 进度回调：参数是已下载比例（0-1）
ProgressCallback = Callable[[float], None]

# 权重文件名 → 类别顺序：`models/` 里堆着好几家的权重时，靠文件名认出用的是哪份
_PRESET_LABELS_BY_FILE: dict[str, tuple[str, ...]] = {
    preset.filename.lower(): preset.labels for preset in NSFW_MODEL_PRESETS.values()
}


def preset_key(preset: str) -> str:
    """把配置里的预设名收敛到合法值（未知值退回默认）。"""
    key = str(preset or "").strip().lower()
    return key if key in NSFW_MODEL_PRESETS else DEFAULT_NSFW_MODEL_PRESET


def preset_filename(preset: str) -> str:
    """预设 → ONNX 权重文件名。"""
    return NSFW_MODEL_PRESETS[preset_key(preset)].filename


def preset_spec(preset: str) -> NsfwModelPreset:
    """预设 → 整份描述（文件名、文案、类别、下载源）。"""
    return NSFW_MODEL_PRESETS[preset_key(preset)]


def labels_for_filename(name: str) -> tuple[str, ...]:
    """按权重文件名反查类别顺序；对不上任何预设时返回空（交给权重 metadata）。"""
    return _PRESET_LABELS_BY_FILE.get(name.strip().lower(), ())


def _format_source(template: str, filename: str) -> str:
    """把下载地址模板里的 ``{filename}`` 换掉；写不出占位的地址原样使用。"""
    try:
        return template.format(filename=filename)
    except (KeyError, IndexError):
        return template


# 兼容旧调用名（external 只是本仓内部用，保留别名免得逐处改）
variant_filename = preset_filename


def find_model_files(models_dir: Path) -> list[Path]:
    """列出目录下可用的权重文件，过滤掉空文件与占位文件。

    `.gitkeep` 之类的空文件、下载中断留下的半截文件都在这里被挡掉：只有当体积超过
    下限才承认它是一份权重。
    """
    if not models_dir.is_dir():
        return []

    found: list[Path] = []
    try:
        entries = sorted(models_dir.iterdir())
    except OSError as exc:
        logger.debug(f"[{PLUGIN_NAME}] 读取模型目录失败 {models_dir}: {exc}")
        return []

    for path in entries:
        if path.suffix.lower() != ".onnx":
            continue
        try:
            if path.is_file() and path.stat().st_size >= NSFW_MIN_MODEL_BYTES:
                found.append(path)
        except OSError:
            continue
    return found


def pick_model(models_dir: Path, preferred_preset: str) -> Path | None:
    """挑一份权重：先认预设对应的文件，再认目录里其它已知预设，最后才认第一个 .onnx。

    目录里可能同时躺着自动下载的和用户手动放进去的权重，随便取一个就会把 A 模型的
    类别套到 B 权重上（类别错位 = 打错码），所以按文件名认比按名字排序取第一个稳。
    """
    candidates = find_model_files(models_dir)
    if not candidates:
        return None

    wanted = preset_filename(preferred_preset).lower()
    for path in candidates:
        if path.name.lower() == wanted:
            return path
    for path in candidates:
        if path.name.lower() in _PRESET_LABELS_BY_FILE:
            return path
    return candidates[0]


class ModelWeights:
    """模型权重的落点与按需下载；只负责把文件放好，加载交给检测器。"""

    def __init__(
        self,
        models_dir: Path,
        *,
        preset: str = DEFAULT_NSFW_MODEL_PRESET,
        auto_download: bool = False,
        download_url: str = "",
        proxy: str = "",
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self.models_dir = models_dir
        self.preset = preset_key(preset)
        self.auto_download = auto_download
        self.download_url = (download_url or "").strip()
        self.proxy = proxy or ""
        # 下载进度回调（0-1 的比例）：只有需要显示进度的调用方（插件面板的模型下载页）才传
        self.on_progress = on_progress
        self._lock = asyncio.Lock()

    def local_model(self) -> Path | None:
        """当前可用的权重（不做任何网络请求）。

        找不到指定档位时会退回目录里任意一份能用的权重——封面链路要的就是「有模型就行」，
        但「下载并切换到某个档位」不能这样兜，那种场景请用 :meth:`model_for_preset`。
        """
        return pick_model(self.models_dir, self.preset)

    @property
    def task(self) -> str:
        """当前档位的任务类型（detect / segment）；目录里换过同名文件时以权重 metadata 为准。"""
        return NSFW_MODEL_PRESETS[self.preset].task

    @property
    def labels(self) -> tuple[str, ...]:
        """当前权重的类别顺序：按实际选中的文件名查预设，查不到交给检测器读 metadata。"""
        path = self.local_model()
        if path is None:
            return NSFW_MODEL_PRESETS[self.preset].labels
        return labels_for_filename(path.name)

    def model_for_preset(self) -> Path | None:
        """严格按档位取权重：文件名对不上就返回 None，绝不拿别的档位顶替。"""
        path = self.models_dir / preset_filename(self.preset)
        try:
            if path.is_file() and path.stat().st_size >= NSFW_MIN_MODEL_BYTES:
                return path
        except OSError:
            return None
        return None

    async def ensure(self, *, require_preset: bool = False) -> tuple[Path | None, bool]:
        """确保本地有权重。

        Args:
            require_preset: 必须是配置里这个档位；为 False 时目录里有任意一份可用权重就返回它。

        Returns:
            tuple[Path | None, bool]: (权重路径, 是否本次新下载)。没权重且未开启自动
            下载时返回 ``(None, False)``，调用方据此跳过评分/局部打码。
        """
        async with self._lock:
            local = self.model_for_preset() if require_preset else self.local_model()
            if local is not None:
                return local, False

            if not self.auto_download:
                logger.info(
                    f"[{PLUGIN_NAME}] models/ 下没有 NSFW 权重，且未开启自动下载；"
                    "本次跳过评分与局部打码"
                )
                return None, False

            return await self._download()

    def _candidate_urls(self, filename: str) -> list[str]:
        """自定义地址：支持多行 / 空格分隔，逐条按顺序尝试。

        允许 ``{filename}`` 占位，方便填镜像或自建地址；写不了占位的地址原样使用。
        """
        return [
            _format_source(raw, filename)
            for raw in self.download_url.replace(",", " ").split()
        ]

    def _download_urls(self, filename: str) -> list[str]:
        """按顺序给出下载源：配置了自定义地址就只用它，否则用预设里的那几条。"""
        custom = self._candidate_urls(filename)
        if custom:
            return custom

        sources = NSFW_MODEL_PRESETS[self.preset].sources or NSFW_ERAX_SOURCES
        return [_format_source(source, filename) for source in sources]

    async def _download(self) -> tuple[Path | None, bool]:
        try:
            self.models_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error(f"[{PLUGIN_NAME}] 创建模型目录失败 {self.models_dir}: {exc}")
            return None, False

        filename = preset_filename(self.preset)
        target = self.models_dir / filename
        part_path = target.with_name(f"{filename}.part")

        urls = self._download_urls(filename)
        for index, url in enumerate(urls, 1):
            label = f"源 {index}/{len(urls)}"
            try:
                logger.info(
                    f"[{PLUGIN_NAME}] 开始下载 NSFW 权重 {filename}（{label}）: {url}"
                )
                size = await self._fetch(url, part_path)
                if size < NSFW_MIN_MODEL_BYTES:
                    logger.warning(
                        f"[{PLUGIN_NAME}] 下载内容只有 {size} 字节，不像权重文件，换下一个源"
                    )
                    part_path.unlink(missing_ok=True)
                    continue
                if not self._looks_like_weight(part_path):
                    logger.warning(f"[{PLUGIN_NAME}] 下载内容不是有效权重格式，换下一个源")
                    part_path.unlink(missing_ok=True)
                    continue

                os.replace(part_path, target)
                logger.info(
                    f"[{PLUGIN_NAME}] NSFW 权重下载完成: {target}（{size / 1048576:.1f} MB）"
                )
                return target, True
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 权重下载失败（{label}）{url}: {exc}")
                part_path.unlink(missing_ok=True)

        logger.error(
            f"[{PLUGIN_NAME}] 全部 {len(urls)} 个下载源都失败：可在配置"
            f"「封面与模型 → 模型与检测 → 自定义下载地址」里填镜像或自建直链"
            f"（支持 {{filename}} 占位），"
            f"或手动把 {filename} 放进插件目录的 models/"
        )
        return None, False

    async def _fetch(self, url: str, part_path: Path) -> int:
        """流式下载到 .part，返回写入的字节数。"""
        total = 0
        reported: set[float] = set()
        last_percent = -1
        timeout = aiohttp.ClientTimeout(
            total=NSFW_MODEL_DOWNLOAD_TIMEOUT,
            connect=NSFW_DOWNLOAD_CONNECT_TIMEOUT,
            sock_read=NSFW_DOWNLOAD_READ_TIMEOUT,
        )

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, proxy=self.proxy or None) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")

                expected = response.content_length or 0
                written = 0
                with part_path.open("wb") as fp:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        fp.write(chunk)
                        written += len(chunk)
                        total = expected or written
                        if not expected:
                            continue
                        ratio = written / expected
                        percent = int(ratio * 100)
                        if self.on_progress is not None and percent != last_percent:
                            last_percent = percent
                            self.on_progress(min(1.0, ratio))
                        for step in _PROGRESS_STEPS:
                            if ratio >= step and step not in reported:
                                reported.add(step)
                                logger.info(
                                    f"[{PLUGIN_NAME}] 权重下载进度 {int(step * 100)}%"
                                    f"（{written / 1048576:.1f}/{expected / 1048576:.1f} MB）"
                                )
        return written

    @staticmethod
    def _looks_like_weight(path: Path) -> bool:
        """挡掉 HTTP 200 返回的错误页。

        ``.onnx`` 是 protobuf，没有通用魔数，只能靠体积下限（调用处已检查）
        加「不是 HTML 开头」来判定。
        """
        try:
            with path.open("rb") as fp:
                head = fp.read(2)
        except OSError:
            return False
        return head not in {b"<!", b"<h", b"<?"}

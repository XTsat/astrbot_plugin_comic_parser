"""NSFW 模型权重的定位与自动下载。

权重来自 HuggingFace 的 `EraX-NSFW-V1.0`（Apache-2.0，YOLO11 的 n/s/m 三档微调版，
类别为 anus / make_love / nipple / penis / vagina）。插件不内置权重：几十 MB 的二进制
不适合进 git，用不到的人也不该被迫下载，因此默认关闭、由配置开关按需拉取。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp
from astrbot.api import logger

from ...domain.constants import (
    DEFAULT_NSFW_MODEL_VARIANT,
    NSFW_MIN_MODEL_BYTES,
    NSFW_MODEL_DOWNLOAD_TIMEOUT,
    NSFW_MODEL_REPO,
    NSFW_MODEL_SOURCES,
    NSFW_MODEL_VARIANTS,
    PLUGIN_NAME,
)

# 下载进度只在跨过这些比例时打一条日志，避免每个分片都刷屏
_PROGRESS_STEPS: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)


def variant_filename(variant: str) -> str:
    """把配置档位换算成权重文件名，未知档位退回默认档。"""
    key = str(variant or "").strip().lower()
    if key not in NSFW_MODEL_VARIANTS:
        key = DEFAULT_NSFW_MODEL_VARIANT
    return NSFW_MODEL_VARIANTS[key]


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
        if path.suffix.lower() not in {".pt", ".pth"}:
            continue
        try:
            if path.is_file() and path.stat().st_size >= NSFW_MIN_MODEL_BYTES:
                found.append(path)
        except OSError:
            continue
    return found


def pick_model(models_dir: Path, preferred_variant: str) -> Path | None:
    """优先取配置档位对应的权重，其次取目录里第一个可用权重（用户自己放的也算）。"""
    candidates = find_model_files(models_dir)
    if not candidates:
        return None

    wanted = variant_filename(preferred_variant).lower()
    for path in candidates:
        if path.name.lower() == wanted:
            return path
    return candidates[0]


class ModelWeights:
    """模型权重的落点与按需下载；只负责把文件放好，加载交给检测器。"""

    def __init__(
        self,
        models_dir: Path,
        *,
        variant: str = DEFAULT_NSFW_MODEL_VARIANT,
        auto_download: bool = False,
        download_url: str = "",
        proxy: str = "",
    ) -> None:
        self.models_dir = models_dir
        self.variant = str(variant or "").strip().lower() or DEFAULT_NSFW_MODEL_VARIANT
        self.auto_download = auto_download
        self.download_url = (download_url or "").strip()
        self.proxy = proxy or ""
        self._lock = asyncio.Lock()

    def local_model(self) -> Path | None:
        """当前可用的权重（不做任何网络请求）。"""
        return pick_model(self.models_dir, self.variant)

    async def ensure(self) -> tuple[Path | None, bool]:
        """确保本地有权重。

        Returns:
            tuple[Path | None, bool]: (权重路径, 是否本次新下载)。没权重且未开启自动
            下载时返回 ``(None, False)``，调用方据此跳过评分/局部打码。
        """
        async with self._lock:
            local = self.local_model()
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
        if self.download_url:
            # 允许 {filename}/{repo} 占位，方便换镜像或自建地址
            try:
                return [self.download_url.format(filename=filename, repo=NSFW_MODEL_REPO)]
            except (KeyError, IndexError):
                return [self.download_url]

        return [
            template.format(repo=NSFW_MODEL_REPO, filename=filename)
            for template in NSFW_MODEL_SOURCES
        ]

    async def _download(self) -> tuple[Path | None, bool]:
        try:
            self.models_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error(f"[{PLUGIN_NAME}] 创建模型目录失败 {self.models_dir}: {exc}")
            return None, False

        filename = variant_filename(self.variant)
        target = self.models_dir / filename
        part_path = target.with_name(f"{filename}.part")

        for url in self._candidate_urls(filename):
            try:
                logger.info(f"[{PLUGIN_NAME}] 开始下载 NSFW 权重 {filename}: {url}")
                size = await self._fetch(url, part_path)
                if size < NSFW_MIN_MODEL_BYTES:
                    logger.warning(
                        f"[{PLUGIN_NAME}] 下载内容只有 {size} 字节，不像权重文件，换下一个源"
                    )
                    part_path.unlink(missing_ok=True)
                    continue
                if not self._looks_like_weight(part_path):
                    logger.warning(f"[{PLUGIN_NAME}] 下载内容不是 PyTorch 权重格式，换下一个源")
                    part_path.unlink(missing_ok=True)
                    continue

                os.replace(part_path, target)
                logger.info(
                    f"[{PLUGIN_NAME}] NSFW 权重下载完成: {target}（{size / 1048576:.1f} MB）"
                )
                return target, True
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 权重下载失败 {url}: {exc}")
                part_path.unlink(missing_ok=True)

        logger.error(
            f"[{PLUGIN_NAME}] 所有权重下载源都失败：可在配置「封面与 NSFW」里填入可用的"
            "下载地址，或手动把 .pt 放进插件目录的 models/"
        )
        return None, False

    async def _fetch(self, url: str, part_path: Path) -> int:
        """流式下载到 .part，返回写入的字节数。"""
        total = 0
        reported: set[float] = set()
        timeout = aiohttp.ClientTimeout(total=NSFW_MODEL_DOWNLOAD_TIMEOUT)

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
        """torch 的 .pt 是 zip 归档，magic 为 `PK`；用它挡掉 HTTP 200 的错误页。"""
        try:
            with path.open("rb") as fp:
                return fp.read(2) == b"PK"
        except OSError:
            return False

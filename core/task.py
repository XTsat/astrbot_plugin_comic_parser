"""单次下载任务的编排。

固定顺序：解析 ID → 取元信息 → 下载图片 → **统一打包加密**（上层，不归渠道）。
渠道只填「下载」这一格，产物形态、密码、清理都在这里统一。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from ..domain import (
    ChannelError,
    ComicMeta,
    DownloadError,
    DownloadedComic,
    PackError,
    PackResult,
    PluginSettings,
)
from ..domain.constants import PLUGIN_NAME
from ..utils import build_package_name
from .channel import BaseChannel, ProgressCallback
from .packer import Packer


@dataclass(slots=True)
class DownloadOutcome:
    """一次成功任务的完整结果，供命令层拼消息与决定清理。"""

    meta: ComicMeta
    pack: PackResult
    source_dir: Path
    image_count: int
    failed_images: int
    elapsed: float

    @property
    def sendable_path(self) -> Path | None:
        """可以发给用户的文件路径；仅本地保存（fmt=none）时为 None。"""
        if not self.pack.success or self.pack.fmt == "none":
            return None
        return self.pack.output_path


class DownloadTask:
    """把一次用户请求跑完，失败时抛 PluginError 子类（消息可直接给用户）。"""

    def __init__(
        self,
        channel: BaseChannel,
        settings: PluginSettings,
        temp_root: Path,
        save_root: Path,
    ) -> None:
        self.channel = channel
        self.settings = settings
        self.temp_root = temp_root
        self.save_root = save_root

    async def run(
        self, raw_id: str, on_progress: ProgressCallback | None = None
    ) -> DownloadOutcome:
        started = time.monotonic()
        comic_id = self.channel.normalize_id(raw_id)
        work_dir = self.temp_root / f"{self.channel.name}-{comic_id}-{int(started)}"
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            meta = await self._fetch_meta(comic_id, work_dir)
            downloaded = await self._download(comic_id, work_dir, on_progress)
            pack = await asyncio.to_thread(
                self._pack, downloaded, meta
            )
        except Exception:
            Packer.cleanup(work_dir)
            raise

        if not pack.success:
            Packer.cleanup(work_dir)
            raise PackError(pack.error or "打包失败")

        return DownloadOutcome(
            meta=meta,
            pack=pack,
            source_dir=downloaded.source_dir,
            image_count=downloaded.image_count,
            failed_images=downloaded.failed_images,
            elapsed=time.monotonic() - started,
        )

    async def _fetch_meta(self, comic_id: str, work_dir: Path) -> ComicMeta:
        try:
            return await asyncio.wait_for(
                self.channel.fetch_meta(comic_id, work_dir),
                timeout=min(self.settings.download_timeout, 120),
            )
        except asyncio.TimeoutError as exc:
            raise DownloadError("获取漫画信息超时，请稍后重试") from exc
        except ChannelError:
            raise
        except Exception as exc:  # 渠道内部异常统一翻译
            logger.error(f"[{PLUGIN_NAME}] 获取元信息失败: {exc}")
            raise self.channel.wrap_failure(exc) from exc

    async def _download(
        self, comic_id: str, work_dir: Path, on_progress: ProgressCallback | None
    ) -> DownloadedComic:
        """按配置重试。整本超时是硬上限，重试不会突破它。"""
        attempts = max(0, self.settings.download_retry_times) + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.wait_for(
                    self.channel.download(comic_id, work_dir, on_progress),
                    timeout=self.settings.download_timeout,
                )
            except asyncio.TimeoutError as exc:
                last_error = DownloadError("下载超时，请稍后重试")
            except DownloadError as exc:
                last_error = exc
            except Exception as exc:
                logger.error(f"[{PLUGIN_NAME}] 下载异常(第 {attempt} 次): {exc}")
                last_error = self.channel.wrap_failure(exc)

            if attempt < attempts:
                logger.warning(
                    f"[{PLUGIN_NAME}] 下载失败，{attempt}/{attempts} 次，准备重试"
                )
                await asyncio.sleep(min(3 * attempt, 10))

        assert last_error is not None
        raise last_error

    def _pack(self, downloaded: DownloadedComic, meta: ComicMeta) -> PackResult:
        base_name = build_package_name(
            title=meta.display_title,
            comic_id=meta.comic_id,
            template=self.settings.filename_template,
            custom_template=self.settings.filename_custom,
            password=self.settings.pack_password,
            author=meta.author,
        )
        if self.settings.pack_format == "none":
            return self._keep_local(downloaded, base_name)

        packer = Packer(self.settings.pack_format, self.settings.pack_password)
        return packer.pack(downloaded.source_dir, base_name, self.temp_root)

    def _keep_local(self, downloaded: DownloadedComic, base_name: str) -> PackResult:
        """仅本地保存：把图片目录搬到长期保存目录，而不是留在临时目录里被清掉。"""
        target = self.save_root / base_name
        suffix = 1
        while target.exists():
            suffix += 1
            target = self.save_root / f"{base_name}_{suffix}"
        try:
            shutil.move(str(downloaded.source_dir), str(target))
        except OSError as exc:
            logger.error(f"[{PLUGIN_NAME}] 移动到保存目录失败: {exc}")
            raise PackError(f"保存到本地失败：{exc}") from exc
        return PackResult(success=True, output_path=target, fmt="none", encrypted=False)

    def cleanup(self, outcome: DownloadOutcome) -> None:
        """任务收尾：临时目录一律清掉；仅本地保存的目录属于用户资产，保留。"""
        if outcome.pack.fmt != "none":
            Packer.cleanup(outcome.pack.output_path)
        Packer.cleanup(outcome.source_dir)

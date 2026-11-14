"""图片下载：并发抓图、失败重试，必要时退回抓页器。

图床（`i.nhentai.net`）是独立域名，多数时候直连即可，用 aiohttp 并发最快；但图床偶尔也会
开始返回 Cloudflare 挑战（403/429/503），这时把**这一张**改走 cloudscraper 再取一次，
避免整本因为图床加了风控就直接失败。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger

from ....domain import ChannelError
from ....domain.constants import PLUGIN_NAME
from ....utils import find_existing_image, write_image_atomically
from ...progress import emit
from ...channel import ProgressCallback
from .progress import DownloadSession
from .site import PageScraper

if TYPE_CHECKING:  # 仅为类型标注；运行时按下面的惰性分支处理依赖缺失
    import aiohttp
else:
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - AstrBot 运行时自带 aiohttp
        aiohttp = None

# 图床返回这几种状态说明请求被风控挡下，而不是「这张图不存在」
_CHALLENGE_STATUS = frozenset({403, 429, 503})

# 已知图片文件头：图床偶尔以 200 回挑战页/HTML，原样交给面板只会显示一张裂图
_IMAGE_SIGNATURES: tuple[bytes, ...] = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8")


def _looks_like_image(payload: bytes) -> bool:
    return payload.startswith(_IMAGE_SIGNATURES) or (
        payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
    )


ImageProgressCallback = ProgressCallback


@dataclass(slots=True)
class DownloadStats:
    """一次整本下载的结果统计。"""

    total: int = 0
    success: int = 0
    failed: int = 0


async def fetch_image_bytes(
    url: str,
    *,
    scraper: PageScraper,
    headers: dict[str, str],
    proxy: str = "",
    timeout: int = 30,
) -> bytes | None:
    """只取一张图的原始字节（不落盘、不建目录、不重试）。

    面板的「从渠道拉封面」只要一张图：走 `ImageDownloader` 会建目录、按页序命名、查断点、
    再摊一张失败重试预算到整本上，代价远大于一次 GET；这里连会话都只开一次用完即关。
    图床回风控状态时按下载器的同一套兜底改走抓页器（浏览器指纹）再取一次。
    """
    if aiohttp is None:
        return None

    status = 0
    try:
        async with aiohttp.ClientSession(headers=headers, trust_env=False) as client:
            async with client.get(
                url, timeout=max(1, timeout), proxy=proxy or None
            ) as response:
                status = response.status
                if status == 200:
                    return _verified(await response.read())
    except (asyncio.TimeoutError, OSError, aiohttp.ClientError):
        return None

    if status not in _CHALLENGE_STATUS:
        return None
    try:
        return _verified(await asyncio.to_thread(scraper.fetch_bytes, url))
    except ChannelError:
        return None


def _verified(payload: bytes | None) -> bytes | None:
    """只把真正的图片交出去：取不到或不是图片都当失败，由调用方记一条 warning。"""
    if not payload or not _looks_like_image(payload):
        return None
    return payload


class ImageDownloader:
    """把一组图片地址下载到目录里，文件名按页序补零（001.jpg、002.jpg…）。"""

    def __init__(
        self,
        *,
        scraper: PageScraper,
        headers: dict[str, str],
        proxy: str = "",
        timeout: int = 30,
        max_concurrent: int = 5,
        retry_times: int = 2,
    ) -> None:
        self._scraper = scraper
        self._headers = headers
        self._proxy = proxy.strip()
        self._timeout = max(1, timeout)
        self._max_concurrent = max(1, max_concurrent)
        self._retry_times = max(0, retry_times)

    async def download(
        self,
        urls: list[str],
        work_dir: Path,
        *,
        session: DownloadSession,
        on_progress: ImageProgressCallback | None = None,
    ) -> DownloadStats:
        if aiohttp is None:
            raise ChannelError("缺少运行依赖，暂时下不了，请联系管理员安装后重载插件")

        work_dir.mkdir(parents=True, exist_ok=True)
        stats = DownloadStats(total=len(urls))
        if not urls:
            return stats

        width = max(3, len(str(len(urls))))
        semaphore = asyncio.Semaphore(self._max_concurrent)
        tasks: list[asyncio.Task[bool]] = []

        async with aiohttp.ClientSession(
            # 代理只从配置来：开关关掉时是真直连，不受容器环境变量影响
            headers=self._headers, trust_env=False
        ) as client:
            for index, url in enumerate(urls, 1):
                suffix = Path(url).suffix or ".jpg"
                target = work_dir / f"{index:0{width}d}{suffix}"
                tasks.append(
                    asyncio.create_task(
                        self._fetch_one(
                            client,
                            semaphore,
                            url,
                            target,
                            session=session,
                            on_progress=on_progress,
                        )
                    )
                )
            try:
                results = await asyncio.gather(*tasks)
            finally:
                # 中断（插件重载/超时）时把还在飞的请求收掉，别让它们跑完再白写文件
                for task in tasks:
                    if not task.done():
                        task.cancel()

        stats.success = sum(1 for ok in results if ok)
        stats.failed = stats.total - stats.success
        return stats

    async def _fetch_one(
        self,
        client: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        url: str,
        target: Path,
        *,
        session: DownloadSession,
        on_progress: ImageProgressCallback | None,
    ) -> bool:
        async with semaphore:
            # 断点续跑：这一页上次已经下好（非空文件）就不再重下，重发一次命令只补缺的
            existing = find_existing_image(target)
            if existing is not None:
                session.done += 1
                try:
                    session.bytes_downloaded += existing.stat().st_size
                except OSError:
                    pass
                await emit(on_progress, session.state())
                return True

            ok = await self._fetch_with_retry(client, url, target, session)

            session.done += 1
            await emit(on_progress, session.state())
            return ok

    async def _fetch_with_retry(
        self,
        client: aiohttp.ClientSession,
        url: str,
        target: Path,
        session: DownloadSession,
    ) -> bool:
        challenged = False
        for attempt in range(self._retry_times + 1):
            if session.stopped:
                return False
            try:
                async with client.get(
                    url, timeout=self._timeout, proxy=self._proxy or None
                ) as response:
                    if response.status == 200:
                        payload = await response.read()
                        await asyncio.to_thread(write_image_atomically, target, payload)
                        session.bytes_downloaded += len(payload)
                        return True
                    if response.status == 404:
                        logger.warning(f"[{PLUGIN_NAME}] 图片 404，跳过：{url}")
                        return False
                    challenged = response.status in _CHALLENGE_STATUS
                    logger.debug(
                        f"[{PLUGIN_NAME}] 图片下载失败 {url}：HTTP {response.status}"
                        f"（第 {attempt + 1}/{self._retry_times + 1} 次）"
                    )
            except (asyncio.TimeoutError, OSError) as exc:
                logger.debug(
                    f"[{PLUGIN_NAME}] 图片下载异常 {url}：{exc}"
                    f"（第 {attempt + 1}/{self._retry_times + 1} 次）"
                )
            if attempt < self._retry_times:
                await asyncio.sleep(min(1 + attempt, 3))

        if challenged:
            return await self._fetch_via_scraper(url, target, session)
        return False

    async def _fetch_via_scraper(
        self, url: str, target: Path, session: DownloadSession
    ) -> bool:
        """图床出现风控时的兜底：同一条地址改由 cloudscraper（浏览器指纹）再取一次。"""
        logger.info(f"[{PLUGIN_NAME}] 图床可能触发风控，改用抓页器重取：{url}")
        try:
            payload = await asyncio.to_thread(self._scraper.fetch_bytes, url)
        except ChannelError as exc:
            # 兜底路径再失败就记这一张失败，不能让它掀翻整本下载
            logger.debug(f"[{PLUGIN_NAME}] 抓页器重取失败 {url}：{exc}")
            return False
        if not payload:
            return False
        await asyncio.to_thread(write_image_atomically, target, payload)
        session.bytes_downloaded += len(payload)
        return True

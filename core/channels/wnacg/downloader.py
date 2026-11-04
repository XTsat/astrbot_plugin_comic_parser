"""图片下载：并发抓图与失败重试。

图床是**独立域名**（如 `img5.wnimg2.cfd`），与站点不同域，图片地址带时效签名，所以：
- 不做地址缓存，拿到就下；
- 图床偶尔也回 Cloudflare 状态码（403/429/503），这时把**这一张**改走站内抓页器（浏览器
  指纹）再取一次，避免整本因为图床风控直接失败；
- 签名过期（403 且带 `verify` 的地址）重试无意义，交给调用方在失败数上体现。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger

from ....domain import ChannelError
from ....domain.constants import PLUGIN_NAME
from ....utils import write_image_atomically
from ...progress import emit
from ...channel import ProgressCallback
from .progress import DownloadSession
from .site import WnacgSite, sniff_image_suffix

if TYPE_CHECKING:  # 仅为类型标注；运行时按下面的惰性分支处理依赖缺失
    import aiohttp
else:
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - AstrBot 运行时自带 aiohttp
        aiohttp = None

# 图床返回这几种状态说明请求被风控挡下，而不是「这张图不存在」
_CHALLENGE_STATUS = frozenset({403, 429, 503})

ImageProgressCallback = ProgressCallback


@dataclass(slots=True)
class DownloadStats:
    """一次下载的结果统计。"""

    total: int = 0
    success: int = 0
    failed: int = 0


class ImageDownloader:
    """把一组图片地址下载到目录里，文件名按页序补零（001.jpg、002.jpg…）。"""

    def __init__(
        self,
        *,
        site: WnacgSite,
        headers: dict[str, str],
        proxy: str = "",
        timeout: int = 30,
        max_concurrent: int = 4,
        retry_times: int = 2,
    ) -> None:
        self._site = site
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
            raise ChannelError("缺少 aiohttp 依赖，无法下载图片")

        work_dir.mkdir(parents=True, exist_ok=True)
        stats = DownloadStats(total=len(urls))
        if not urls:
            return stats

        width = max(3, len(str(len(urls))))
        semaphore = asyncio.Semaphore(self._max_concurrent)
        tasks: list[asyncio.Task[bool]] = []

        async with aiohttp.ClientSession(
            headers=self._headers, trust_env=False
        ) as client:
            for index, url in enumerate(urls, 1):
                # 后缀不按图址猜，等拿到字节后按文件头定（站点图址的扩展名不可信）
                stem = work_dir / f"{index:0{width}d}"
                tasks.append(
                    asyncio.create_task(
                        self._fetch_one(
                            client,
                            semaphore,
                            url,
                            stem,
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
        stem: Path,
        *,
        session: DownloadSession,
        on_progress: ImageProgressCallback | None,
    ) -> bool:
        async with semaphore:
            ok = await self._fetch_with_retry(client, url, stem, session)

            session.done += 1
            await emit(on_progress, session.state())
            return ok

    async def _fetch_with_retry(
        self,
        client: aiohttp.ClientSession,
        url: str,
        stem: Path,
        session: DownloadSession,
    ) -> bool:
        challenged = False
        for attempt in range(self._retry_times + 1):
            if session.stop:
                return False
            try:
                async with client.get(
                    url, timeout=self._timeout, proxy=self._proxy or None
                ) as response:
                    if response.status == 200:
                        payload = await response.read()
                        suffix = sniff_image_suffix(payload)
                        if suffix is None:
                            # 200 但不是图片：多半是风控页/错误页，不能当成成功写进产物
                            logger.warning(
                                f"[{PLUGIN_NAME}] 收到的内容不是图片（{len(payload)} 字节），跳过：{url}"
                            )
                            return False
                        await asyncio.to_thread(
            write_image_atomically, stem.with_suffix(suffix), payload
        )
                        session.bytes_downloaded += len(payload)
                        return True
                    if response.status in _CHALLENGE_STATUS:
                        challenged = True
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
            return await self._fetch_via_site(url, stem, session)
        return False

    async def _fetch_via_site(
        self, url: str, stem: Path, session: DownloadSession
    ) -> bool:
        """图床出现风控时的兜底：同一条地址改由站内抓页器（浏览器指纹）再取一次。"""
        logger.info(f"[{PLUGIN_NAME}] wnacg 图床疑似风控，改用抓页器重取：{url}")
        payload = await self._site.fetch_bytes(url)
        if not payload:
            return False
        suffix = sniff_image_suffix(payload)
        if suffix is None:
            logger.debug(f"[{PLUGIN_NAME}] 抓页器取回的内容也不是图片：{url}")
            return False
        await asyncio.to_thread(
            write_image_atomically, stem.with_suffix(suffix), payload
        )
        session.bytes_downloaded += len(payload)
        return True

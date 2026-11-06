"""nhentai 官方 API v2 客户端（异步、纯 JSON）。

页面在 Cloudflare 后面（普通 HTTP 客户端一律 403，cloudscraper 也常常过不去），但
`/api/v2/...` 是纯 JSON 接口：实测不带任何凭据、不触发挑战就能读到作品详情与图床清单。
所以元信息优先走这里，HTML 抓取只作为 API 不可用时的兜底（见 `site.py`）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from astrbot.api import logger

from ....domain import ChannelError
from ....domain.constants import PLUGIN_NAME
from .constants import (
    CDN_API_PATH,
    DEFAULT_IMAGE_DOMAIN,
    GALLERY_API_PATH,
    SEARCH_API_PATH,
    USER_AGENT,
)


class GalleryNotFound(ChannelError):
    """作品不存在或已下架：这是确定结论，不该再退回页面抓取重试一遍。"""


if TYPE_CHECKING:  # 只为类型标注；运行时按下面的惰性分支处理依赖缺失
    import aiohttp
else:
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - AstrBot 运行时自带 aiohttp
        aiohttp = None


class NhentaiApi:
    """按域名清单依次尝试的 API v2 客户端。

    图床清单（`/api/v2/cdn`）只在第一次用到时取一次并缓存：它是站点级配置，逐本重复
    请求没有意义，失败时退回默认图床域名继续下载。
    """

    def __init__(
        self,
        *,
        domains: list[str],
        proxy: str = "",
        timeout: int = 30,
        user_agent: str = USER_AGENT,
    ) -> None:
        self._domains = domains
        self._proxy = proxy.strip()
        self._timeout = max(1, timeout)
        self._user_agent = user_agent
        self._image_servers: list[str] = []

    async def fetch_gallery(self, gallery_id: str) -> dict[str, Any]:
        """取作品详情（含 media_id、页数、每页相对路径、标签、标题）。"""
        payload = await self._get_json(
            GALLERY_API_PATH.format(gallery_id=gallery_id),
            not_found=GalleryNotFound(f"作品 {gallery_id} 不存在或已下架"),
        )
        if not isinstance(payload, dict):
            raise ChannelError("nhentai 接口返回了非预期的数据格式")
        return payload

    async def search(self, keyword: str, page: int = 1) -> dict[str, Any]:
        """按关键词搜索作品（API v2，匿名可用，25 条/页）。"""
        path = (
            f"{SEARCH_API_PATH}?query={quote(keyword, safe='')}"
            f"&page={max(1, int(page))}"
        )
        payload = await self._get_json(
            path, not_found=ChannelError("nhentai 搜索接口不存在")
        )
        if not isinstance(payload, dict):
            raise ChannelError("nhentai 搜索接口返回了非预期的数据格式")
        return payload

    async def image_servers(self) -> list[str]:
        """图床服务器清单；取不到就用默认图床域名，绝不让它挡住下载。"""
        if self._image_servers:
            return self._image_servers

        servers: list[str] = []
        try:
            payload = await self._get_json(
                CDN_API_PATH, not_found=ChannelError("图床配置接口不存在")
            )
            raw = payload.get("image_servers") if isinstance(payload, dict) else None
            if isinstance(raw, list):
                servers = [
                    str(item).rstrip("/")
                    for item in raw
                    if isinstance(item, str) and item.strip()
                ]
        except ChannelError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 获取图床清单失败，改用默认图床：{exc}")

        self._image_servers = servers or [DEFAULT_IMAGE_DOMAIN]
        return self._image_servers

    async def _get_json(self, path: str, *, not_found: ChannelError) -> object:
        if aiohttp is None:
            raise ChannelError("缺少 aiohttp 依赖，无法访问 nhentai 接口")
        if not self._domains:
            raise ChannelError("未配置任何 nhentai 站点域名，请在渠道配置里填写")

        failures: list[str] = []
        for domain in self._domains:
            url = f"https://{domain}{path}"
            try:
                return await self._request(url, not_found=not_found)
            except GalleryNotFound:
                # 作品不存在是确定结论，换域名也没用
                raise
            except ChannelError as exc:
                failures.append(f"{domain}：{exc}")

        raise ChannelError(f"nhentai 接口不可用：{'；'.join(failures)}")

    async def _request(self, url: str, *, not_found: ChannelError) -> object:
        """单次请求；非 200 一律翻译成带原因的 ChannelError，由调用方决定换域名还是报错。"""
        if aiohttp is None:
            raise ChannelError("缺少 aiohttp 依赖，无法访问 nhentai 接口")

        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        try:
            async with aiohttp.ClientSession(
                # 代理只从配置来：开关关掉时是真直连，不受容器环境变量影响
                headers=headers, trust_env=False
            ) as session, session.get(
                url, proxy=self._proxy or None, timeout=self._timeout
            ) as response:
                if response.status == 404:
                    raise not_found
                if response.status == 429:
                    raise ChannelError("触发限流（HTTP 429），稍后再试")
                if response.status != 200:
                    raise ChannelError(f"返回 HTTP {response.status}")
                return await response.json(content_type=None)
        except (asyncio.TimeoutError, OSError, aiohttp.ClientError) as exc:
            raise ChannelError(f"请求失败：{exc}") from exc

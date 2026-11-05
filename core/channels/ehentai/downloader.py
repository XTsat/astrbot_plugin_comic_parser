"""E-Hentai 请求节流与图片下载。

站点把「打开页面太快」直接判成自动化抓取，所以**页面请求**（作品页、图片页）走
`RequestGate` 串行限速；图片字节本身在 H@H 图床上，按并发数取，不占页面节流名额。

509（配额用尽）、sadpanda/登录墙、临时封禁分开抛：前两者由渠道转成「暂停保留断点」，
后者提示补 Cookie，不能混成普通失败反复重试。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger

from ....domain import ChannelError
from ....domain.constants import PLUGIN_NAME
from .api import EHentaiClient, EHBanned, EHLoginRequired, EHQuotaExceeded
from .constants import QUOTA_STATUS
from .site import (
    ImageLinks,
    is_banned,
    is_content_warning,
    is_login_wall,
    is_quota_gif,
    is_sadpanda,
    parse_gallery_page,
    parse_image_page,
    sniff_image_suffix,
)

# 这些状态码说明「这次没取到」而不是「这张图不存在」，值得重试
_RETRYABLE_STATUS = frozenset({403, 429, 502, 503, 504})


class RequestGate:
    """保证两次页面请求之间至少间隔 `interval` 秒。

    多个协程同时等闸门时串行放行：站点风控看的是**请求到达速率**，不是并发数，
    所以这里不能用「各等各的 sleep」，必须排队。
    """

    def __init__(self, interval: float) -> None:
        self._interval = max(0.0, float(interval))
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            delay = self._last + self._interval - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = loop.time()


class EHentaiDownloader:
    """页面限速 + 图片并发下载；不含作品页翻页与落盘编排（那在 `channel.py`）。"""

    def __init__(
        self,
        client: EHentaiClient,
        *,
        interval: float,
        retry_times: int,
        concurrency: int,
        include_original: bool,
    ) -> None:
        self._client = client
        self._gate = RequestGate(interval)
        self._retry_times = max(0, int(retry_times))
        self._semaphore = asyncio.Semaphore(max(1, int(concurrency)))
        self.include_original = include_original

    # ---- 页面 ----

    async def gallery_page(self, url: str, host: str) -> list[str]:
        text = await self._get_text(url)
        return parse_gallery_page(text, host)

    async def image_links(self, url: str, host: str) -> ImageLinks:
        text = await self._get_text(url)
        return parse_image_page(text, host)

    async def _get_text(self, url: str) -> str:
        await self._gate.wait()
        text, final_url = await self._client.get_text(url)
        _raise_if_blocked(text, final_url)
        if is_content_warning(text):
            raise ChannelError(
                "该作品被站点标记为内容警告，自动跳过失败；"
                "请先在浏览器打开作品页确认一次，再重发命令"
            )
        return text

    # ---- 图片 ----

    async def image(self, url: str) -> tuple[bytes, str]:
        """取一张图片，返回（字节，后缀）；配额/登录/封禁异常原样抛给上层。"""
        if is_quota_gif(url):
            raise EHQuotaExceeded("E-Hentai 返回 509 占位图：图片配额已用尽")

        async with self._semaphore:
            for attempt in range(self._retry_times + 1):
                payload, content_type, status, final_url = await self._client.get_bytes(url)
                if status == QUOTA_STATUS or is_quota_gif(final_url):
                    raise EHQuotaExceeded("E-Hentai 返回 509：图片配额已用尽")

                if status == 200:
                    if "text/html" in (content_type or "").lower():
                        text = payload.decode("utf-8", "replace")
                        if is_banned(text):
                            raise EHBanned(_BANNED_MESSAGE)
                        if is_login_wall(text) or is_sadpanda(final_url):
                            raise EHLoginRequired(
                                "该图片需要登录才能访问（Cookie 未配置、已失效或不是 exhentai 账号）"
                            )
                        raise ChannelError("图片地址返回了网页而不是图片，可能触发了站点风控")
                    suffix = sniff_image_suffix(content_type, payload)
                    if suffix is None:
                        raise ChannelError(
                            f"返回的内容不是可识别的图片（{content_type or '未知类型'}）"
                        )
                    return payload, suffix

                if status in _RETRYABLE_STATUS and attempt < self._retry_times:
                    logger.debug(
                        f"[{PLUGIN_NAME}] E-Hentai 图片 HTTP {status}，重试 "
                        f"({attempt + 1}/{self._retry_times})：{url}"
                    )
                    await asyncio.sleep(min(1 + attempt, 3))
                    continue
                raise ChannelError(f"图片请求返回 HTTP {status}")

        raise ChannelError("图片下载失败：重试次数已用尽")


_BANNED_MESSAGE = (
    "站点提示当前 IP 被临时封禁（翻页或下载太快），请过几小时或换网络后再试"
)


def _raise_if_blocked(text: str, final_url: str) -> None:
    """页面级风控：临时封禁 → 暂停；登录跳转/sadpanda → 提示 Cookie。"""
    if is_banned(text):
        raise EHBanned(_BANNED_MESSAGE)
    if (
        is_login_wall(text)
        or is_sadpanda(final_url)
        or "bounce_login" in (final_url or "")
    ):
        raise EHLoginRequired(
            "该页面需要登录才能访问（Cookie 未配置、已失效或不是 exhentai 账号）"
        )
    if is_quota_gif(final_url):
        raise EHQuotaExceeded("E-Hentai 返回 509：图片配额已用尽")

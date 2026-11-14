"""上游搜图接口的 HTTP 客户端：把一张图换成一页 JSON，不解释结果。

上游挡在 Cloudflare 之后，**触发挑战的是出口 IP 的请求密度**而不是单次请求长什么样：
同一出口几秒内连打十几次就会进挑战名单，之后几分钟内连正常请求也会被拦。所以这里的策略是
「少发、慢发、能不发就不发」——全局串行 + 最小间隔节流 + 同图缓存，真被拦了再用长退避重试。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import time
from typing import Any

import httpx
from PIL import Image

from ...domain import PluginError, PluginSettings
from .constants import (
    BROWSER_HEADERS,
    CACHE_MAX_ENTRIES,
    CACHE_TTL_SECONDS,
    IMAGE_JPEG_QUALITY,
    MAX_ATTEMPTS,
    MAX_UPLOAD_BYTES,
    MIN_INTERVAL_SECONDS,
    MIN_SCALE,
    NORMAL_FACTOR,
    RETRY_DELAYS,
    RETRY_STATUSES,
    SEARCH_PATH,
    STRICT_FACTOR,
)


class ImageSearchError(PluginError):
    """搜图失败：消息直接展示给用户。"""


# 进程级：并发搜图会排队，两次上游请求之间至少隔 MIN_INTERVAL_SECONDS
_UPSTREAM_LOCK = asyncio.Lock()
_LAST_CALL_AT = 0.0
# 进程级缓存：(图片哈希, 严格度) → (写入时刻, 响应 JSON)
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _cache_get(key: str) -> dict[str, Any] | None:
    """取缓存；过期即丢。"""
    hit = _CACHE.get(key)
    if not hit:
        return None
    stored_at, payload = hit
    if time.time() - stored_at > CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return payload


def _cache_put(key: str, payload: dict[str, Any]) -> None:
    """写入缓存，超过上限时按写入时间淘汰最旧的几条。"""
    if len(_CACHE) >= CACHE_MAX_ENTRIES:
        for stale in sorted(_CACHE, key=lambda k: _CACHE[k][0])[
            : len(_CACHE) - CACHE_MAX_ENTRIES + 1
        ]:
            _CACHE.pop(stale, None)
    _CACHE[key] = (time.time(), payload)


class ImageSearchClient:
    """按配置访问上游搜索接口。每次搜索独立短连接，不持有跨请求状态。"""

    def __init__(self, settings: PluginSettings) -> None:
        self.base_url = (settings.image_search_base_url or "").rstrip("/")
        self.timeout = float(settings.image_search_timeout)
        # 默认直连：上游对家用出口是放行的，而漫画下载用的代理多是机房/共享出口，
        # 经它反而会被 Cloudflare 弹挑战页（实测同一时刻直连 200、走代理 403）。
        # 需要时可在「搜图」里单独指定代理，或显式打开「沿用下载代理」。
        dedicated = (settings.image_search_proxy or "").strip()
        inherited = (settings.proxy or "").strip() if settings.image_search_use_proxy else ""
        self.proxy = dedicated or inherited
        # 宿主的 HTTP(S)_PROXY 环境变量在容器里常被配坏（例如写成 Python 列表字面量），
        # httpx 解析它会直接抛 InvalidURL，所以只认插件自己的代理配置，不读宿主环境

    async def search(self, image: bytes, *, strict: bool) -> dict[str, Any]:
        """提交图片并返回上游 JSON；同图命中缓存，真打上游时串行并节流。

        Args:
            image: 原始图片二进制（超过上限会自动缩放）。
            strict: 严格模式（系数更大，误报更少）。

        Raises:
            ImageSearchError: 重试用尽仍失败，或响应不是合法 JSON。
        """
        if not self.base_url:
            raise ImageSearchError("搜图还没配好服务地址，请联系管理员在插件配置的「搜图」里填写")
        payload = self._compress(image)
        factor = STRICT_FACTOR if strict else NORMAL_FACTOR
        cache_key = hashlib.sha256(payload + factor.encode()).hexdigest()
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
        url = f"{self.base_url}{SEARCH_PATH}"
        options: dict[str, Any] = {
            "timeout": self.timeout,
            "trust_env": False,
            "headers": dict(BROWSER_HEADERS),
        }
        if self.proxy:
            options["proxy"] = self.proxy

        async with _UPSTREAM_LOCK:
            # 拿到锁后先补足最小间隔：排队等待已经耗掉的时间也算数
            global _LAST_CALL_AT
            wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _LAST_CALL_AT)
            if _LAST_CALL_AT and wait > 0:
                await asyncio.sleep(wait)
            data = await self._post_with_retry(url, options, payload, factor)
            _LAST_CALL_AT = time.monotonic()
        _cache_put(cache_key, data)
        return data

    async def _post_with_retry(
        self, url: str, options: dict[str, Any], payload: bytes, factor: str
    ) -> dict[str, Any]:
        """发一次搜索请求；被挑战/限流/上游故障时按退避重试。"""
        last_error: ImageSearchError | None = None
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient(**options) as http:
                    resp = await http.post(
                        url,
                        files={"file": ("image.jpg", payload, "image/jpeg")},
                        data={"factor": factor},
                    )
            except (httpx.HTTPError, OSError) as exc:
                # 异常类型只进日志：群成员看到 ConnectError 也做不了什么
                last_error = ImageSearchError(
                    "搜图失败：暂时连不上搜图服务，稍后再试"
                )
                continue
            if resp.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                last_error = ImageSearchError(self._explain(resp.status_code))
                continue
            if resp.status_code >= 400:
                raise ImageSearchError(self._explain(resp.status_code))
            try:
                data = resp.json()
            except ValueError as exc:
                raise ImageSearchError("搜图失败：搜图服务返回的不是合法数据") from exc
            if not isinstance(data, dict):
                raise ImageSearchError("搜图失败：搜图服务返回的不是合法数据")
            return data
        raise last_error or ImageSearchError("搜图失败：暂时连不上搜图服务，稍后再试")

    @staticmethod
    def _explain(status: int) -> str:
        """把上游状态码翻成用户能行动的一句话，不要把挑战页 HTML 甩进聊天。"""
        if status == 403:
            return "搜图失败：上游站点暂时拦住了请求（浏览器校验），等几分钟再试；连着搜太频繁最容易触发"
        if status == 429:
            return "搜图失败：请求太频繁了，等一会儿再试"
        if status >= 500:
            return f"搜图失败：搜图服务暂时不可用（HTTP {status}），稍后再试"
        return f"搜图失败：HTTP {status}"

    @staticmethod
    def _compress(image: bytes) -> bytes:
        """超过上传上限时按比例缩图；缩到下限仍超限就原样上传，让上游自己判。"""
        if len(image) <= MAX_UPLOAD_BYTES:
            return image
        try:
            with Image.open(io.BytesIO(image)) as raw:
                img = raw.convert("RGB")
        except (OSError, ValueError) as exc:
            raise ImageSearchError("搜图失败：图片打不开，可能不是图片文件") from exc
        scale = 1.0
        while True:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY)
            if buf.tell() <= MAX_UPLOAD_BYTES or scale < MIN_SCALE:
                return buf.getvalue()
            scale *= 0.8
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))

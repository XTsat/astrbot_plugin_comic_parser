"""哔咔（PicACG）接口细节：请求签名、登录、漫画/章节/图片列表、单图下载。

这套 API 的两个坑——HMAC 请求签名、图片直链拼接——都封在这里，
渠道层只拿「漫画字典 / 章节列表 / 图片地址 / 落盘文件」，不碰协议细节。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ....domain import ChannelError
from ....domain.constants import PLUGIN_NAME
from .constants import (
    ACCEPT,
    API_KEY,
    APP_BUILD_VERSION,
    APP_CHANNEL,
    APP_PLATFORM,
    APP_UUID,
    APP_VERSION,
    BASE_URL,
    DEFAULT_SECRET_KEY,
    IMAGE_SERVER_BS,
    IMAGE_SERVER_DEFAULT,
    IMAGE_SERVER_TOBEIMG,
    MAX_PAGES,
    MIN_IMAGE_BYTES,
    PATH_COMIC,
    PATH_EPISODE_PAGES,
    PATH_EPISODES,
    PATH_SEARCH,
    PATH_SIGN_IN,
    USER_AGENT,
)

# 这三个节点的 path 前缀规则与普通节点不同，见 image_url()
_LEGACY_IMAGE_SERVERS = ("", IMAGE_SERVER_DEFAULT, IMAGE_SERVER_BS)

_HTTPX: Any = None


def http_module() -> Any:
    """惰性拿到 httpx。

    AstrBot 自带 httpx，但缺了也不该让整个插件加载失败，所以只在真正发请求时才 import。
    """
    global _HTTPX
    if _HTTPX is None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - AstrBot 环境里必然存在
            raise ChannelError("哔咔渠道依赖 httpx 未安装，请先安装后重载插件") from exc
        _HTTPX = httpx
    return _HTTPX


def image_url(media: Mapping[str, Any]) -> str:
    """把 media 字段拼成图片直链。

    上游路径有三种前缀，照搬「fileServer + /static/ + path」会让其中两种 404：
    `tobeimg/` 走老的 base64 编码规则或 img 节点，`tobs/` 要去掉前缀再挂到 B 节点上。
    """
    path = str(media.get("path") or "").lstrip("/")
    server = str(media.get("fileServer") or "").rstrip("/")
    if not path:
        return ""

    if path.startswith("tobeimg/"):
        if server in _LEGACY_IMAGE_SERVERS:
            return _decode_tobeimg_path(path)
        return f"{server}/{path[len('tobeimg/') :]}"
    if path.startswith("tobs/"):
        return f"{server or IMAGE_SERVER_BS}/static/{path[len('tobs/') :]}"
    return f"{server or IMAGE_SERVER_DEFAULT}/static/{path}"


def _decode_tobeimg_path(path: str) -> str:
    """老节点把绝对地址 base64 编进了文件名，解不出来时退回按 img 节点拼。"""
    filename = path.rsplit("/", 1)[-1]
    encoded = filename.rsplit(".", 1)[0]
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        decoded = ""
    if decoded.startswith(("http://", "https://")):
        return decoded
    return f"{IMAGE_SERVER_TOBEIMG}/{path[len('tobeimg/') :]}"


def sign(endpoint: str, timestamp: str, nonce: str, method: str, secret_key: str) -> str:
    """哔咔的请求签名：HMAC-SHA256(secret, lower(路径含 query + 时间戳 + nonce + 方法 + api-key))。

    参与签名的路径**不带前导斜杠**，query 必须一并算进去，否则签名永远对不上。
    """
    raw = f"{endpoint}{timestamp}{nonce}{method}{API_KEY}".lower()
    return hmac.new(secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()


class PicaClient:
    """一次会话内复用的哔咔客户端：负责签名、重试、翻页与落盘。"""

    def __init__(self, secret_key: str, proxy: str, timeout: int, retry_times: int) -> None:
        self.secret_key = secret_key or DEFAULT_SECRET_KEY
        self.proxy = proxy
        self.timeout = timeout
        self.retry_times = max(retry_times, 0)
        self.token = ""
        self._client: Any = None

    # ---- 连接 ----
    def _connect(self) -> Any:
        """建客户端。

        代理参数在 httpx 0.26 改过名（proxies → proxy），两种都试一遍，
        免得插件装在不同 AstrBot 版本上因为一个关键字直接报错。
        """
        httpx = http_module()
        options: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
            # 哔咔的接口与图片节点证书普遍不合规，只能跳过校验
            "verify": False,
        }
        if not self.proxy:
            # 显式直连：容器里常带系统代理变量，不关掉会绕到不存在的代理上
            options["trust_env"] = False
            return httpx.AsyncClient(**options)
        try:
            return httpx.AsyncClient(proxy=self.proxy, **options)
        except TypeError:
            return httpx.AsyncClient(proxies=self.proxy, **options)

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = self._connect()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- 请求 ----
    def _headers(self, endpoint: str, method: str) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        headers = {
            "api-key": API_KEY,
            "accept": ACCEPT,
            "app-channel": APP_CHANNEL,
            "app-version": APP_VERSION,
            "app-uuid": APP_UUID,
            "app-platform": APP_PLATFORM,
            "app-build-version": APP_BUILD_VERSION,
            "image-quality": "original",
            "content-type": "application/json; charset=UTF-8",
            "user-agent": USER_AGENT,
            "time": timestamp,
            "nonce": nonce,
            "signature": sign(endpoint, timestamp, nonce, method, self.secret_key),
        }
        if self.token:
            headers["authorization"] = self.token
        return headers

    async def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        require_token: bool = True,
    ) -> dict[str, Any]:
        """发一个 API 请求并返回解析后的 JSON，失败抛面向用户的 ChannelError。"""
        httpx = http_module()
        endpoint = path.lstrip("/")
        if params:
            query = "&".join(f"{key}={value}" for key, value in params.items())
            endpoint = f"{endpoint}?{query}"
        url = f"{BASE_URL}{endpoint}"
        if require_token and not self.token:
            raise ChannelError("哔咔接口需要登录：请在渠道配置里填写账号与密码")

        last_error = ""
        for attempt in range(self.retry_times + 1):
            if attempt:
                await asyncio.sleep(min(0.5 * attempt, 2.0))
            try:
                response = await self._ensure_client().request(
                    method, url, headers=self._headers(endpoint, method), json=body
                )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    f"[{PLUGIN_NAME}] 哔咔请求失败（第 {attempt + 1} 次）: {endpoint} · {last_error}"
                )
                continue

            payload = _as_payload(response)
            code = payload.get("code")
            if response.status_code == 200 and code in (200, None):
                # 实测：签名不对时服务端不回 401，而是回一个没有 data 的 200 success 占位。
                # 直接当成功会把「签名密钥不对」误报成「这本漫画不存在」，所以这里拦一道。
                if "data" not in payload:
                    raise ChannelError(
                        "哔咔返回了空响应：多半是签名密钥不匹配或客户端版本被拦，"
                        "请检查渠道配置里的签名密钥"
                    )
                return payload
            detail = payload.get("message") or payload.get("error") or ""
            last_error = f"HTTP {response.status_code} / code={code} / {detail}"

            # 5xx、限流值得重试；4xx（作品下架、密码错）重试只会白等
            if response.status_code >= 500 or code in (429, 500, 502, 503):
                logger.warning(
                    f"[{PLUGIN_NAME}] 哔咔请求异常（第 {attempt + 1} 次）: {endpoint} · {last_error}"
                )
                continue
            if code == 401:
                self.token = ""
                raise ChannelError("哔咔登录态已失效，请重新发送下载命令")
            raise ChannelError(f"哔咔接口返回错误：{last_error}")

        raise ChannelError(f"哔咔请求多次失败：{endpoint} · {last_error}")

    # ---- 业务接口 ----
    async def login(self, account: str, password: str) -> str:
        """账号密码换 token。哔咔没有匿名访问，所有接口都要求 token。"""
        if not account or not password:
            raise ChannelError(
                "哔咔渠道需要账号密码：请在插件配置的哔咔渠道里填写账号与密码"
            )
        payload = await self.request(
            "POST",
            PATH_SIGN_IN,
            body={"email": account, "password": password},
            require_token=False,
        )
        token = str((payload.get("data") or {}).get("token") or "")
        if not token:
            raise ChannelError("哔咔登录失败：接口没有返回 token，请检查账号密码")
        self.token = token
        return token

    async def search(self, keyword: str, page: int = 1) -> dict[str, Any]:
        """按关键词搜索漫画；返回 `data.comics` 分页对象（需登录）。"""
        payload = await self.request(
            "POST",
            PATH_SEARCH,
            params={"page": max(1, int(page))},
            body={"keyword": keyword, "categories": [], "s": "ua"},
        )
        return payload

    async def fetch_comic(self, comic_id: str) -> dict[str, Any]:
        payload = await self.request("GET", PATH_COMIC.format(comic_id=comic_id))
        comic = (payload.get("data") or {}).get("comic")
        if not isinstance(comic, dict):
            raise ChannelError("哔咔没有返回这本漫画的信息，可能 ID 不存在或已下架")
        return comic

    async def fetch_episodes(self, comic_id: str) -> list[dict[str, Any]]:
        """取全部章节（上游按页返回，这里按 pages 翻完并按 order 排序）。"""
        episodes: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            payload = await self.request(
                "GET", PATH_EPISODES.format(comic_id=comic_id), params={"page": page}
            )
            data = (payload.get("data") or {}).get("eps") or {}
            docs = data.get("docs") or []
            episodes.extend(item for item in docs if isinstance(item, dict))
            if page >= max(_as_int(data.get("pages"), 1), 1) or not docs:
                break
            page += 1
        episodes.sort(key=lambda item: _as_int(item.get("order"), 0))
        return episodes

    async def fetch_pages(self, comic_id: str, order: int) -> list[dict[str, Any]]:
        """取某章的全部图片条目（同样需要翻页）。"""
        pages: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            payload = await self.request(
                "GET",
                PATH_EPISODE_PAGES.format(comic_id=comic_id, order=order),
                params={"page": page},
            )
            data = (payload.get("data") or {}).get("pages") or {}
            docs = data.get("docs") or []
            pages.extend(item for item in docs if isinstance(item, dict))
            if page >= max(_as_int(data.get("pages"), 1), 1) or not docs:
                break
            page += 1
        return pages

    async def download_image(self, url: str, dest: Path) -> int:
        """下载单张图片到 dest，返回落盘字节数；失败抛 ChannelError。"""
        content = await self._image_content(url)
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(dest.write_bytes, content)
        return len(content)

    async def fetch_image_bytes(self, url: str) -> bytes | None:
        """取一张图的原始字节（不落盘）；失败返回 None。

        面板的「从渠道拉封面」只要一张图：`download_image` 那条路会建目录、写文件，
        封面不该在磁盘上留任何东西，所以把「取字节」这一段单独放出来共用。
        """
        try:
            return await self._image_content(url)
        except ChannelError:
            return None

    async def _image_content(self, url: str) -> bytes:
        """GET 图片字节：沿用哔咔图片节点的「降级 HTTP + 防盗链头 + 重试」规则。"""
        httpx = http_module()
        # 部分文件节点没有可用的 HTTPS 证书，降级到 HTTP 反而更稳
        target = url.replace("https://", "http://", 1) if url.startswith("https://") else url
        headers = {
            "user-agent": USER_AGENT,
            "referer": "https://picacomic.com/",
            "accept": "image/*",
        }
        last_error = ""
        for attempt in range(self.retry_times + 1):
            if attempt:
                await asyncio.sleep(min(0.5 * attempt, 2.0))
            try:
                response = await self._ensure_client().get(target, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            content = response.content
            if response.status_code == 200 and len(content) >= MIN_IMAGE_BYTES:
                return content
            last_error = f"HTTP {response.status_code} / {len(content)} 字节"
        raise ChannelError(f"图片下载失败：{last_error} · {url}")


def _as_payload(response: Any) -> dict[str, Any]:
    """把响应体解析成 dict；上游偶尔返回 HTML 错误页，这里兜住。"""
    try:
        payload = response.json()
    except ValueError:
        return {"code": response.status_code, "message": "响应不是 JSON"}
    return payload if isinstance(payload, dict) else {"code": response.status_code}


def _as_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default

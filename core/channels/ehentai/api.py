"""E-Hentai 的 HTTP 客户端与官方 `gdata` 元信息接口。

- 一个下载流程只开一个 `aiohttp.ClientSession`：整本要发上千次请求，逐次建会话会把
  连接复用完全浪费掉；页面与图片都走同一个客户端，登录与否只差一条 Cookie 头。
- `gdata` 是官方接口，匿名可用；作品页/图片页仍是 HTML，解析在 `site.py`。
- 509（图片配额用尽）与登录墙、临时封禁分别用独立异常标出来：前两者决定要不要保留
  断点暂停，后者决定要不要提示用户补 Cookie，混成普通 `ChannelError` 会误重试。
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from astrbot.api import logger

from ....domain import ChannelError
from ....domain.constants import PLUGIN_NAME
from ...channel import PROXY_SOURCE_DIRECT
from .constants import API_HOSTS, API_PATH, USER_AGENT

if TYPE_CHECKING:  # 只为类型标注；运行时按下面的惰性分支处理依赖缺失
    import aiohttp
else:
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - AstrBot 运行时自带 aiohttp
        aiohttp = None


class EHQuotaExceeded(ChannelError):
    """图片配额用尽（509）：保留断点、过一阵再续，而不是当网络失败重试。"""


class EHLoginRequired(ChannelError):
    """需要登录（或 Cookie 已失效）：匿名拿原图、exhentai 未登录时出现。"""


class EHBanned(ChannelError):
    """IP 被临时封禁（翻页/下载太快）：同样保留断点，让用户过几小时再试。"""


# aiohttp 只认 http(s) 代理；socks 要额外的 aiohttp-socks，这里不支持也不假装支持
_PROXY_SCHEMES = frozenset({"http", "https"})
# 站点在境外，直连不通时表现就是超时或连不上：只有这两类才值得提示「你可能需要代理」
_DIRECT_HINT_KEYWORDS = ("超时", "连不上")


def normalize_proxy(raw: str) -> str:
    """把用户填的代理串收敛成 aiohttp 吃得下的形式。

    配置里最常见的填法是 `127.0.0.1:7890`（浏览器和常见教程都这么写），缺 scheme 时
    aiohttp 会当成相对 URL 直接抛 ValueError，所以这里补 `http://`。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    return text if "://" in text else f"http://{text}"


def _exc_text(exc: BaseException) -> str:
    """异常的原话；消息为空时退回类名，避免报错变成半截「请求失败：」。"""
    text = str(exc).strip()
    return text or type(exc).__name__


def _endpoint_text(exc: BaseException) -> str:
    host = getattr(exc, "host", "") or ""
    port = getattr(exc, "port", None)
    if host and port:
        return f"{host}:{port}"
    return host or "目标主机"


def describe_request_error(exc: BaseException, timeout: int) -> str:
    """把传输层异常翻译成一句能照着排查的话。

    原始异常经常没有可读消息（`asyncio.TimeoutError` 的 str 就是空串），用户只能看到
    「请求失败：」——分不清是没网、没代理、代理挂了还是站点 5xx，所以按类型分别翻译。
    """
    if isinstance(exc, asyncio.TimeoutError):
        return f"超时（{timeout} 秒内没有响应）"
    if aiohttp is not None:
        if isinstance(exc, aiohttp.ClientProxyConnectionError):
            return f"连不上代理（{_exc_text(exc)}）"
        if isinstance(exc, aiohttp.ClientSSLError):
            # 证书过期、自签、系统根证书缺失都落这里；证书类异常的文本已说明原因
            return f"TLS 握手失败（{_exc_text(exc)}）"
        if isinstance(exc, aiohttp.ClientConnectorError):
            os_error = getattr(exc, "os_error", None)
            if isinstance(os_error, socket.gaierror):
                return f"连不上 {_endpoint_text(exc)}（域名解析失败）"
            if os_error is not None:
                return f"连不上 {_endpoint_text(exc)}（{_exc_text(os_error)}）"
            return f"连不上 {_endpoint_text(exc)}"
        if isinstance(exc, aiohttp.ServerDisconnectedError):
            return "服务器提前断开了连接"
        if isinstance(exc, aiohttp.ClientPayloadError):
            return f"响应不完整（{_exc_text(exc)}）"
        if isinstance(exc, aiohttp.ClientError):
            return _exc_text(exc)
    return _exc_text(exc)


def parse_cookie_string(raw: str) -> dict[str, str]:
    """把浏览器里复制的一整段 Cookie 拆成字典；容忍换行、多余分号与空段。"""
    cookies: dict[str, str] = {}
    # 用户常从 DevTools 连「Cookie:」前缀一起复制，去掉它再拆
    cleaned = re.sub(r"^\s*cookie\s*:\s*", "", (raw or "").strip(), flags=re.I)
    for part in cleaned.replace("\r", "\n").split("\n"):
        for item in part.split(";"):
            name, sep, value = item.partition("=")
            name = name.strip()
            value = value.strip()
            if sep and name and value:
                cookies[name] = value
    return cookies


def has_login_cookies(cookies: dict[str, str]) -> bool:
    """登录态的最小集：`ipb_member_id` + `ipb_pass_hash`（gallery-dl / EhViewer 同口径）。

    `igneous` 是 exhentai 域专用，不作为登录判定；缺它时 exhentai 会回 sadpanda，
    由上层提示用户补。
    """
    return bool(cookies.get("ipb_member_id") and cookies.get("ipb_pass_hash"))


class EHentaiClient:
    """带浏览器 UA、可选登录 Cookie 的共享 HTTP 会话。"""

    def __init__(
        self,
        *,
        proxy: str = "",
        proxy_source: str = "",
        timeout: int = 30,
        cookie: str = "",
        user_agent: str = USER_AGENT,
    ) -> None:
        # 代理来源（「直连」/「代理」/「宿主代理」）只用于失败提示：直连失败时要提醒配代理
        self._proxy_source = (proxy_source or "").strip()
        self._timeout = max(1, int(timeout))
        self._proxy = normalize_proxy(proxy)
        if self._proxy:
            scheme = urlsplit(self._proxy).scheme.lower()
            if scheme not in _PROXY_SCHEMES:
                raise ChannelError(
                    f"代理地址的协议 {scheme}:// 不受支持：E-Hentai 渠道只能走 http 代理，"
                    "请在插件配置的「代理地址」里换成 http:// 代理"
                )
        self.cookies = parse_cookie_string(cookie)
        # 内容警告页只是「确认已满 18 岁」的过场，浏览器点一次会写入 nw=session；
        # 下载场景没有可点的按钮，直接带上等价 Cookie，避免整本卡在警告页
        self.cookies.setdefault("nw", "session")
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if self.cookies:
            headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()
            )
        self._headers = headers
        self._session: aiohttp.ClientSession | None = None

    @property
    def logged_in(self) -> bool:
        return has_login_cookies(self.cookies)

    async def __aenter__(self) -> EHentaiClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    # ---- 请求 ----

    async def get_text(self, url: str) -> tuple[str, str]:
        """取 HTML 正文，返回（正文，最终 URL）；最终 URL 用来识别登录跳转。"""
        body, _content_type, status, final_url = await self._request("GET", url)
        if status == 509:
            raise EHQuotaExceeded("E-Hentai 返回 509：图片配额已用尽")
        if status != 200:
            raise ChannelError(f"页面请求返回 HTTP {status}")
        return body.decode("utf-8", "replace"), final_url

    async def get_bytes(self, url: str) -> tuple[bytes, str, int, str]:
        """取图片原始字节，返回（字节，Content-Type，状态码，最终 URL）。

        状态码交给调用方判断：图片下载要把 509 / 风控 / 登录墙分开处理。
        """
        return await self._request("GET", url)

    async def post_json(self, url: str, payload: dict[str, Any]) -> object:
        body, _content_type, status, final_url = await self._request(
            "POST", url, json_body=payload
        )
        if status == 509:
            raise EHQuotaExceeded("E-Hentai 返回 509：图片配额已用尽")
        if status == 403:
            # 站点在 Cloudflare 后面：UA 缺失、出口 IP 被判定为滥用时都回 403
            raise ChannelError(
                "E-Hentai 接口返回 HTTP 403：请求被站点拒绝"
                "（常见于出口 IP 被风控，换代理线路或过一阵再试）"
            )
        if status != 200:
            raise ChannelError(f"E-Hentai 接口返回 HTTP {status}")
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise ChannelError("E-Hentai 接口返回的不是合法 JSON") from exc

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[bytes, str, int, str]:
        if aiohttp is None:
            raise ChannelError("缺少 aiohttp 依赖，无法访问 E-Hentai")
        session = self._ensure_session()
        try:
            async with session.request(
                method,
                url,
                json=json_body,
                proxy=self._proxy or None,
            ) as response:
                body = await response.read()
                return (
                    body,
                    response.headers.get("Content-Type", ""),
                    response.status,
                    str(response.url),
                )
        except (asyncio.TimeoutError, OSError, ValueError, aiohttp.ClientError) as exc:
            reason = describe_request_error(exc, self._timeout)
            raise ChannelError(f"请求失败：{reason}") from exc

    @property
    def direct_hint(self) -> str:
        """直连失败时的排障提示，由元信息入口拼在报错末尾（每个入口各拼一遍太长）。

        走了代理就不再提示——那时连不通的原因在代理本身，指向配置只会误导。
        """
        if self._proxy_source and self._proxy_source != PROXY_SOURCE_DIRECT:
            return ""
        return (
            "；当前为直连，E-Hentai 在多数网络下需要代理，"
            "请在插件配置的「网络 → 代理地址」或渠道的「使用代理」里设置"
        )

    def _ensure_session(self) -> aiohttp.ClientSession:
        if aiohttp is None:
            raise ChannelError("缺少 aiohttp 依赖，无法访问 E-Hentai")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._headers,
                # 代理只从配置来：开关关掉时是真直连，不受容器环境变量影响
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            )
        return self._session


async def fetch_gdata(
    client: EHentaiClient, gid: str, token: str
) -> dict[str, Any]:
    """调 `gdata` 拿单本元信息；两个 API 入口并发尝试，都失败才报错。

    并发而不是串行：两个入口内容一致，直连被丢包时串行要白等两轮超时
    （30 秒 ×2 才报错），并发后最坏只等一轮。
    """
    payload: dict[str, Any] = {
        "method": "gdata",
        "gidlist": [[int(gid) if gid.isdigit() else gid, token]],
        "namespace": 1,
    }
    results = await asyncio.gather(
        *(
            client.post_json(f"https://{host}{API_PATH}", payload)
            for host in API_HOSTS
        ),
        return_exceptions=True,
    )
    failures: list[str] = []
    rejected: str = ""
    for host, result in zip(API_HOSTS, results):
        if isinstance(result, EHQuotaExceeded):
            raise result
        if isinstance(result, BaseException):
            failures.append(f"{host}：{result}")
            logger.debug(f"[{PLUGIN_NAME}] gdata 入口 {host} 不可用：{result}")
            continue
        if not isinstance(result, dict):
            failures.append(f"{host}：响应不是 JSON 对象")
            continue
        error = result.get("error")
        if error:
            # 站点明确回了业务错误（参数/权限/作品不可见），换入口也没用，留到最后统一报
            rejected = f"{host}：{error}"
            continue
        metadata = result.get("gmetadata")
        if isinstance(metadata, list) and metadata and isinstance(metadata[0], dict):
            return metadata[0]
        failures.append(f"{host}：响应里没有 gmetadata")
    if rejected:
        logger.warning(f"[{PLUGIN_NAME}] E-Hentai 接口拒绝请求：{rejected}")
        raise ChannelError("E-Hentai 拒绝了这次请求：作品可能不可见，或参数不对")
    detail = "；".join(failures) or "所有入口都没有返回作品数据"
    logger.warning(f"[{PLUGIN_NAME}] E-Hentai 接口不可用：{detail}")
    unreachable = any(
        keyword in failure
        for failure in failures
        for keyword in _DIRECT_HINT_KEYWORDS
    )
    raise ChannelError(
        "E-Hentai 接口这会儿连不上，稍后再试"
        f"{client.direct_hint if unreachable else ''}"
    )

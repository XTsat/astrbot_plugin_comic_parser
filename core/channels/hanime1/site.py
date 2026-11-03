"""Hanime1 站点接入：抓页、解析、图床地址构造。

Hanime1 的漫画区（`/comic/<ID>`）内容是 nhentai 供源：详情页给出作品信息与页数，
阅读页（`/comic/<ID>/<页>`）给出 `media_id`、每页扩展名，原图在 nhentai 图床
`https://i.nhentai.net/galleries/<media_id>/<页>.<扩展名>`。

本模块只做三件事：

- **抓页**：站点在 Cloudflare 后面，普通 HTTP 客户端多半直接拿到 403 或挑战页，因此走
  cloudscraper（浏览器指纹模拟）。它是同步库，调用点一律丢线程池，不阻塞事件循环。
- **解析**：先按页面里明确的结构取（详情页标题块、阅读页 `data-*` 属性），取不到再退回
  正则兜底——站点改版时至少还有一条降级链路，而不是整本下载直接失败。
- **拼地址**：按页序把 `media_id` 与扩展名拼成原图地址。

图片下载不走这里：图床是独立域名、直连更快，见 `downloader.py`。
"""

from __future__ import annotations

import html as html_module
import json
import re
import threading
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from types import ModuleType
from typing import Protocol

from astrbot.api import logger

from ....domain import ChannelError
from ....domain.constants import PLUGIN_NAME
from .constants import (
    DEFAULT_USER_AGENT,
    IMAGE_EXTENSIONS,
    MAX_PAGES,
    MIN_IMAGE_BYTES,
)

# ---- 详情页 ----

# 中文标题块（`<h4 class="title comics-metadata-margin-top">`），内部是 before/pretty/after 三段
_TITLE_BLOCK = re.compile(
    r'<h[45][^>]*class="[^"]*\bcomics-metadata-margin-top\b[^"]*"[^>]*>(.*?)</h[45]>',
    re.DOTALL | re.IGNORECASE,
)
# 罗马字/英文标题块，作为中文标题缺失时的退路
_ALT_TITLE_BLOCK = re.compile(
    r'<h[345][^>]*class="[^"]*\bcomics-metadata-top-row\b[^"]*"[^>]*>(.*?)</h[345]>',
    re.DOTALL | re.IGNORECASE,
)
_SPAN_PRETTY = re.compile(
    r'<span[^>]*class="[^"]*\bpretty\b[^"]*"[^>]*>(.*?)</span>', re.DOTALL | re.IGNORECASE
)
_SPAN_BEFORE = re.compile(
    r'<span[^>]*class="[^"]*\bbefore\b[^"]*"[^>]*>(.*?)</span>', re.DOTALL | re.IGNORECASE
)
# 页面 <title>：“标题 - H動漫/裏番/漫畫/線上看 - Hanime1.me”，标题本身可能带 &nbsp;
_PAGE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_TITLE_SUFFIX = re.compile(r"\s*[-–]\s*H動漫.*$", re.IGNORECASE)

# 封面与页缩略图里同时带着 media_id：…/galleries/<media_id>/cover.jpg、…/<页>t.jpg
_COVER = re.compile(r"/galleries/(\d+)/cover\.", re.IGNORECASE)
_THUMB_PAGE = re.compile(
    r"/galleries/(\d+)/(\d+)t\.(jpg|jpeg|png|webp|gif)", re.IGNORECASE
)
# 标签与同人：<a ... href=".../tags/xxx"><div ...>中出</div></a>
_TAG_LINK = re.compile(
    r'href="[^"]*/tags/[^"]*"[^>]*>\s*<div[^>]*>([^<]{1,40})</div>', re.IGNORECASE
)
_PARODY_LINK = re.compile(
    r'href="[^"]*/parodies/[^"]*"[^>]*>\s*<div[^>]*>([^<]{1,40})</div>', re.IGNORECASE
)
# 作者在标题 before 段的方括号里：[社团 (作者)] 取括号内，[作者] 直接取方括号内
_AUTHOR_IN_BRACKET = re.compile(r"[（(]\s*([^（()）]{1,40}?)\s*[)）]")
_BRACKET_TEXT = re.compile(r"[\[【]([^\[\]【】]{1,40})[\]】]")

# ---- 阅读页 ----

_DATA_ID = re.compile(r'data-id="(\d+)"', re.IGNORECASE)
_DATA_PAGES = re.compile(r'data-pages="(\d+)"', re.IGNORECASE)
_DATA_PREFIX = re.compile(r'data-prefix="([^"]+)"', re.IGNORECASE)
# extensions 是塞进 textarea 的 JSON 字符串（引号被转义成 &quot;）
_EXTENSIONS = re.compile(r"innerHTML\s*=\s*'(\[[^']*\]|&quot;.*?&quot;)'", re.IGNORECASE)

# 被 Cloudflare 拦下时页面里会有这些特征
_CHALLENGE_MARKERS = (
    "Attention Required",
    "cf-wrapper",
    "cf-error-details",
    "Just a moment",
    "challenge-platform",
)


@dataclass(slots=True)
class ParsedComic:
    """一次解析出的作品信息：下载需要 media_id、页数与每页扩展名。"""

    comic_id: str
    media_id: str = ""
    title: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    page_count: int = 0
    extensions: list[str] = field(default_factory=list)
    # 阅读页自己给出的图床基地址（例如 https://i2.nhentai.net/galleries/<media_id>/）：
    # 图床编号会变（i / i2 / i3），以页面为准比写死域名可靠
    image_base: str = ""

    @property
    def resolved_page_count(self) -> int:
        return len(self.extensions) or self.page_count


# ---- 解析 ----


def parse_detail(comic_id: str, page: str) -> ParsedComic:
    """解析作品详情页：标题、作者、标签、media_id 与页数。

    页数优先按缩略图里出现的最大页码取（`<页>t.jpg`），比数容器个数稳：站点插入广告位
    或改版换容器都不会让页数错位。
    """
    media_id, page_count = _scan_thumbnails(page)
    title, author = _parse_titles(page)
    if not title:
        title = _parse_page_title(page)
    if not title and not media_id:
        raise ChannelError(
            f"无法解析 Hanime1 作品 {comic_id} 的页面，站点结构可能已变化"
        )
    return ParsedComic(
        comic_id=comic_id,
        media_id=media_id,
        title=title,
        author=author,
        tags=_parse_tags(page),
        page_count=page_count,
    )


def parse_reader(comic_id: str, page: str, parsed: ParsedComic | None = None) -> ParsedComic:
    """解析阅读页：拿到权威的 media_id、页数与每页扩展名。

    阅读页的 `data-pages` 与 `extensions` 是站点自己渲染图片时用的数据，比详情页的缩略图
    更准，所以只要有阅读页就以它为准。
    """
    base = parsed or ParsedComic(comic_id=comic_id)
    media_id = _first(_DATA_ID, page) or base.media_id
    prefix = _first(_DATA_PREFIX, page)
    if prefix and not media_id:
        media_id = _media_id_from_prefix(prefix)

    pages_raw = _first(_DATA_PAGES, page)
    page_count = int(pages_raw) if pages_raw and pages_raw.isdigit() else base.page_count
    extensions = _parse_extensions(page)

    if not media_id:
        raise ChannelError(
            f"无法从 Hanime1 阅读页解析出作品 {comic_id} 的图片编号，站点结构可能已变化"
        )
    if not extensions and page_count:
        # 扩展名缺失时按 jpg 兜底：图床对错误扩展名会回 404，宁可全部按最常见格式试
        extensions = [".jpg"] * page_count
    if len(extensions) > MAX_PAGES:
        raise ChannelError(
            f"Hanime1 作品 {comic_id} 返回了 {len(extensions)} 页，超过上限 {MAX_PAGES}，已中止"
        )
    return ParsedComic(
        comic_id=comic_id,
        media_id=media_id,
        title=base.title,
        author=base.author,
        tags=base.tags,
        page_count=page_count or len(extensions),
        extensions=extensions,
        image_base=prefix or base.image_base,
    )


def build_image_urls(image_base: str, media_id: str, extensions: list[str]) -> list[str]:
    """按页序拼出原图地址：`<图床>/galleries/<media_id>/<从 1 开始的页码><扩展名>`。

    `image_base` 允许两种填法：只给图床域名（`https://i2.nhentai.net`），或直接给阅读页里的
    完整前缀（`https://i2.nhentai.net/galleries/<media_id>/`）。后者路径里已经带了
    `galleries/<media_id>`，再拼一次就会得到 `/galleries/x/galleries/x/1.jpg`。
    """
    base = image_base.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    prefix = base if base.endswith(f"/galleries/{media_id}") else f"{base}/galleries/{media_id}"
    return [f"{prefix}/{index}{suffix}" for index, suffix in enumerate(extensions, 1)]


def build_page_url(domain: str, path: str) -> str:
    """把域名清单里的一项拼成页面地址；已带协议头的直接当基地址用（本地自测走这条）。"""
    base = domain.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    return f"{base}{path}"


def looks_like_challenge(body: str) -> bool:
    """响应体是不是 Cloudflare 的挑战/拦截页（有些拦截会以 200 返回）。"""
    sample = body[:4096]
    return any(marker in sample for marker in _CHALLENGE_MARKERS)


# ---- 解析内部 ----


def _scan_thumbnails(page: str) -> tuple[str, int]:
    """从页缩略图地址里取 media_id 与页数（页码取最大值，不看 DOM 顺序）。"""
    media_id = ""
    highest = 0
    for matched in _THUMB_PAGE.finditer(page):
        media_id = media_id or matched.group(1)
        highest = max(highest, int(matched.group(2)))
    if not media_id:
        covered = _COVER.search(page)
        if covered is not None:
            media_id = covered.group(1)
    return media_id, highest


def _parse_titles(page: str) -> tuple[str, str]:
    """取中文标题块（缺失时退回英文块）里的标题与作者。"""
    for pattern in (_TITLE_BLOCK, _ALT_TITLE_BLOCK):
        block = pattern.search(page)
        if block is None:
            continue
        body = block.group(1)
        title = _clean(_first(_SPAN_PRETTY, body))
        if not title:
            continue
        author = _author_from_before(_clean(_first(_SPAN_BEFORE, body)))
        return title, author
    return "", ""


def _author_from_before(before: str) -> str:
    """`[ひとのふんどし (ゆきよし真水)]` 取括号内的作者名，`[西崎えいむ]` 取方括号内的。"""
    if not before:
        return ""
    matched = _AUTHOR_IN_BRACKET.search(before)
    if matched is not None:
        return matched.group(1).strip()
    matched = _BRACKET_TEXT.search(before)
    return matched.group(1).strip() if matched is not None else ""


def _parse_page_title(page: str) -> str:
    matched = _PAGE_TITLE.search(page)
    if matched is None:
        return ""
    title = _clean(matched.group(1))
    title = _TITLE_SUFFIX.sub("", title)
    return title.strip()


def _parse_tags(page: str) -> list[str]:
    tags: list[str] = []
    for pattern in (_TAG_LINK, _PARODY_LINK):
        for matched in pattern.finditer(page):
            name = _clean(matched.group(1))
            if name and name not in tags:
                tags.append(name)
    return tags[:20]


def _parse_extensions(page: str) -> list[str]:
    """阅读页内嵌的 extensions 数组 → 每页扩展名（`j`→`.jpg`）。"""
    matched = _EXTENSIONS.search(page)
    if matched is None:
        return []
    raw = html_module.unescape(matched.group(1)).strip()
    if not raw.startswith("["):
        raw = f"[{raw}]"
    try:
        letters = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(letters, list):
        return []
    return [IMAGE_EXTENSIONS.get(str(letter).strip().lower(), ".jpg") for letter in letters]


def _media_id_from_prefix(prefix: str) -> str:
    matched = re.search(r"/galleries/(\d+)/", prefix)
    return matched.group(1) if matched is not None else ""


def _first(pattern: re.Pattern[str], text: str) -> str:
    matched = pattern.search(text)
    return matched.group(1) if matched is not None else ""


def _clean(raw: str) -> str:
    """去标签、解实体、压空白：页面里标题常混着 &nbsp; 与多余换行。"""
    text = re.sub(r"<[^>]+>", "", raw or "")
    text = html_module.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


# ---- 抓页 ----


def import_cloudscraper() -> ModuleType | None:
    """惰性 import cloudscraper；未安装时返回 None，由渠道的依赖检查给用户提示。"""
    try:
        import cloudscraper
    except ImportError:
        return None
    return cloudscraper


class _ScraperResponse(Protocol):
    status_code: int
    text: str
    content: bytes


class _ScraperSession(Protocol):
    headers: MutableMapping[str, str]
    proxies: dict[str, str]
    trust_env: bool

    def get(self, url: str, timeout: float) -> _ScraperResponse: ...


def _network_error_types() -> tuple[type[BaseException], ...]:
    """cloudscraper 底下是 requests，网络异常要按它的类型捕获；依赖缺失时退回 OSError。"""
    try:
        import requests
    except ImportError:
        return (OSError,)
    return (requests.RequestException, OSError)


class PageScraper:
    """抓页器：会话按进程复用，并用一把线程锁串行化。

    cloudscraper 底下是 `requests.Session`，同一个会话被多个线程并发使用不可靠（连接池与
    Cloudflare cookie 会互相打架）；抓页本身是低频操作，串行化不会拖慢图片下载。
    """

    def __init__(
        self,
        *,
        timeout: int,
        proxy: str = "",
        user_agent: str = DEFAULT_USER_AGENT,
        fetch_proxy: str = "",
    ) -> None:
        self._timeout = max(1, timeout)
        # 连接阶段单独设短超时：域名被墙时 30 秒的连接超时会把上层「取元信息 120 秒」
        # 直接耗光，用户在聊天里只会看到「获取漫画信息超时」，看不到真正原因
        self._connect_timeout = min(10, self._timeout)
        self._proxy = proxy.strip()
        self._user_agent = user_agent
        # 读取代理（如 https://r.jina.ai/）：本机 IP 被 Cloudflare 拦下时，
        # 由对方 IP 取页面、以 HTML 原样返回，图片仍直连图床
        self._fetch_proxy = fetch_proxy.strip()
        self._lock = threading.Lock()
        self._session: _ScraperSession | None = None

    def fetch_text(self, url: str) -> str:
        """抓页面 HTML；非 200 与 Cloudflare 拦截一律翻译成面向用户的 ChannelError。

        配了读取代理时先请求代理：代理限流或故障就退回直连再试一次，避免单一第三方服务
        把整条链路拖死（直连能吃下页面时，用户甚至感知不到代理被动用过）。
        """
        if self._fetch_proxy:
            try:
                return self._fetch_text(url, via_fetch_proxy=True)
            except ChannelError as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}] 读取代理取页失败，改直连再试：{url} · {exc}"
                )
        return self._fetch_text(url, via_fetch_proxy=False)

    def _fetch_text(self, url: str, *, via_fetch_proxy: bool) -> str:
        target = self._via_fetch_proxy(url) if via_fetch_proxy else url
        response = self._get(target)
        if response.status_code == 404:
            raise ChannelError(f"作品不存在或已下架：{url}")
        if response.status_code in {403, 429, 503} or looks_like_challenge(response.text):
            raise ChannelError(
                f"站点返回 HTTP {response.status_code}，被 Cloudflare 拦截了：{url}"
                "（在渠道配置里填「读取代理地址」（例如 https://r.jina.ai/）代取页面，"
                "或换一个能过站点的代理出口）"
            )
        if response.status_code != 200:
            raise ChannelError(f"站点返回 HTTP {response.status_code}：{url}")
        return response.text

    def fetch_bytes(self, url: str) -> bytes | None:
        """抓二进制内容（图床退回路径用）；失败返回 None，由调用方记为该张失败。

        图片一律直连：读取代理是给页面 HTML 用的，转二进制它不保证原样返回。
        """
        response = self._get(url)
        if response.status_code != 200 or len(response.content) < MIN_IMAGE_BYTES:
            return None
        return response.content

    def _via_fetch_proxy(self, url: str) -> str:
        """按读取代理改写地址；没配代理时原样返回。"""
        if not self._fetch_proxy:
            return url
        return f"{self._fetch_proxy.rstrip('/')}/{url}"

    def _get(self, url: str) -> _ScraperResponse:
        with self._lock:
            session = self._session_or_raise()
            # 读取代理要代取整页，首包慢是常态：给它更长的读超时
            read_timeout = (
                max(self._timeout, 60) if self._fetch_proxy else self._timeout
            )
            try:
                return session.get(url, timeout=(self._connect_timeout, read_timeout))
            except _network_error_types() as exc:
                raise ChannelError(f"请求 {url} 失败：{exc}") from exc

    def _session_or_raise(self) -> _ScraperSession:
        if self._session is not None:
            return self._session
        session = self._open_session()
        headers = {
            "User-Agent": self._user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            # 站点对跨站来源比较敏感，带上本站来源头能少挨一次拦截
            "Referer": "https://hanime1.me/",
        }
        if self._fetch_proxy:
            # 读取代理默认回 markdown，要原样 HTML 才能继续用同一套解析
            headers["X-Return-Format"] = "html"
        session.headers.update(headers)
        if self._proxy:
            session.proxies = {"http": self._proxy, "https": self._proxy}
            session.trust_env = False
        else:
            # 显式关掉环境代理：容器里常带系统代理变量，不覆盖会把请求绕到不存在的代理上
            session.proxies = {}
            session.trust_env = False
        self._session = session
        return session

    def _open_session(self) -> _ScraperSession:
        """开一个抓页会话：优先 cloudscraper（能自己过挑战页），退而用 requests。

        配了读取代理时代取方已经过了 Cloudflare，本机有没有 cloudscraper 都能跑；
        没有它又直连的站点，才会走到「被拦」的报错上。
        """
        cloudscraper = import_cloudscraper()
        if cloudscraper is not None:
            return cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "desktop": True}
            )
        try:
            import requests
        except ImportError as exc:
            raise ChannelError(
                "Hanime1 渠道缺少抓页依赖：请安装 cloudscraper 或 requests 后重载插件"
            ) from exc
        return requests.Session()

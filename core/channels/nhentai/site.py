"""nhentai 数据解析与页面抓取兜底。

- **主路径**：`api.py` 取官方 API v2 的 JSON，本模块的 `parse_api_gallery` 把它收敛成
  `ParsedGallery`（media_id + 每页相对路径）。
- **兜底路径**：API 打不开时才抓作品页 HTML（`PageScraper` + cloudscraper），用
  `parse_html_gallery` 解析。页面在 Cloudflare 后面，这条路经常 403，所以只是兜底。

图床地址统一由 `build_image_urls` 拼，两个来源的产物都落到 `ParsedGallery.page_paths`。
"""

from __future__ import annotations

import html as html_module
import json
import re
import threading
from dataclasses import dataclass, field
from types import ModuleType
from typing import Protocol

from astrbot.api import logger

from ....domain import ChannelError
from ....domain.constants import PLUGIN_NAME
from .constants import DEFAULT_IMAGE_DOMAIN, DEFAULT_SITE_DOMAINS, USER_AGENT

# 页面 JSON 里 pages[].t 是单字符类型标记，对应原图扩展名（HTML 兜底才会用到）
_IMAGE_EXTENSIONS: dict[str, str] = {"j": ".jpg", "p": ".png", "g": ".gif", "w": ".webp"}

# 页面内嵌数据有两种形态：新版 <script id="gallery-data">，老版 window._gallery = JSON.parse("…")
_SCRIPT_GALLERY_DATA = re.compile(
    r'<script[^>]*id=["\']gallery-data["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_LEGACY_GALLERY_DATA = re.compile(
    r"window\._gallery\s*=\s*JSON\.parse\((.*?)\)\s*;", re.DOTALL
)

# HTML 兜底：缩略图地址里同时带着 media_id、页码与扩展名（…/galleries/<media_id>/<页>t.jpg）
_THUMB_PAGE = re.compile(
    r"/galleries/(\d+)/(\d+)t\.(jpg|jpeg|png|webp|gif)", re.IGNORECASE
)
_PRETTY_TITLE = re.compile(
    r'<h1[^>]*class="[^"]*\btitle\b[^"]*"[^>]*>.*?<span[^>]*class="[^"]*\bpretty\b[^"]*"[^>]*>(.*?)</span>',
    re.DOTALL | re.IGNORECASE,
)

# 这些 tag 组是站点统计信息，不是作品标签，进元信息只会污染展示
_SKIPPED_TAG_TYPES = frozenset(
    {
        "language",
        "languages",
        "category",
        "categories",
        "translated",
        "page",
        "pages",
        "uploaded",
    }
)


@dataclass(slots=True)
class ParsedGallery:
    """一本作品下载所需的信息：media_id 与每页相对图床的路径。"""

    comic_id: str
    media_id: str
    title: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    page_paths: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.page_paths)


# ---- 解析 ----


def parse_api_gallery(comic_id: str, payload: dict[str, object]) -> ParsedGallery:
    """把 `/api/v2/galleries/<id>` 的 JSON 收敛成 `ParsedGallery`。"""
    media_id = str(payload.get("media_id") or "").strip()
    if not media_id:
        logger.warning(f"[{PLUGIN_NAME}] nhentai 接口没有返回作品 {comic_id} 的 media_id")
        raise ChannelError("nhentai 没有返回这本作品的图片信息：可能已下架，稍后再试")

    raw_pages = payload.get("pages")
    page_paths: list[str] = []
    if isinstance(raw_pages, list):
        for item in raw_pages:
            path = str(item.get("path") or "").strip() if isinstance(item, dict) else ""
            if path:
                page_paths.append(path)
    if not page_paths:
        # 少数响应只带 num_pages 不带 pages，按官方路径规则补齐即可
        pages_count = payload.get("num_pages")
        count = pages_count if isinstance(pages_count, int) and pages_count > 0 else 0
        page_paths = [
            f"galleries/{media_id}/{index}.jpg" for index in range(1, count + 1)
        ]
    if not page_paths:
        logger.warning(f"[{PLUGIN_NAME}] nhentai 接口没有返回作品 {comic_id} 的任何页面")
        raise ChannelError("nhentai 没有返回这本作品的任何页面：可能已下架，稍后再试")

    author, tags = _tags_from_response(payload.get("tags"))
    return ParsedGallery(
        comic_id=comic_id,
        media_id=media_id,
        title=_title_from_response(payload.get("title")),
        author=author,
        tags=tags,
        page_paths=page_paths,
    )


def _title_from_response(raw: object) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in ("pretty", "english", "japanese"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tags_from_response(raw: object) -> tuple[str, list[str]]:
    """取作者与可见标签；站点统计类 tag 一律丢弃。"""
    if not isinstance(raw, list):
        return "", []
    author = ""
    tags: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tag_type = str(item.get("type") or "").strip().lower()
        if tag_type in _SKIPPED_TAG_TYPES:
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        if tag_type == "artist" and not author:
            author = name
            continue
        tags.append(name)
    return author, tags


def parse_html_gallery(comic_id: str, page: str) -> ParsedGallery:
    """HTML 兜底解析：优先读页面内嵌 JSON，读不到再靠缩略图地址反推。"""
    data = _load_gallery_json(page)
    if data is not None:
        parsed = _parse_from_page_json(comic_id, data)
        if parsed is not None:
            return parsed
    parsed = _parse_from_thumbnails(comic_id, page)
    if parsed is not None:
        return parsed
    raise ChannelError(f"无法解析 nhentai 作品 {comic_id} 的页面，站点结构可能已变化")


def _load_gallery_json(page: str) -> dict[str, object] | None:
    for pattern in (_SCRIPT_GALLERY_DATA, _LEGACY_GALLERY_DATA):
        matched = pattern.search(page)
        if matched is None:
            continue
        loaded = _decode_json(matched.group(1).strip())
        if isinstance(loaded, dict):
            return loaded
    return None


def _decode_json(raw: str) -> object | None:
    """解析 JSON 文本；老版页面把 JSON 又编码成字符串字面量，因此允许再解一层。"""
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(loaded, str):
        try:
            return json.loads(loaded)
        except json.JSONDecodeError:
            return None
    return loaded


def _parse_from_page_json(
    comic_id: str, data: dict[str, object]
) -> ParsedGallery | None:
    media_id = str(data.get("media_id") or "").strip()
    images = data.get("images")
    pages = images.get("pages") if isinstance(images, dict) else None
    if not media_id or not isinstance(pages, list):
        return None

    page_paths: list[str] = []
    for index, item in enumerate(pages, 1):
        if not isinstance(item, dict):
            return None
        suffix = _IMAGE_EXTENSIONS.get(str(item.get("t") or ""), ".jpg")
        page_paths.append(f"galleries/{media_id}/{index}{suffix}")
    if not page_paths:
        return None

    author, tags = _tags_from_response(data.get("tags"))
    return ParsedGallery(
        comic_id=comic_id,
        media_id=media_id,
        title=_title_from_response(data.get("title")),
        author=author,
        tags=tags,
        page_paths=page_paths,
    )


def _parse_from_thumbnails(comic_id: str, page: str) -> ParsedGallery | None:
    """最粗暴的兜底：靠缩略图地址反推 media_id、页数与每页扩展名。

    以页码（地址里的 `<页>t.<扩展名>`）为准而不是 DOM 顺序，站点插入广告位或改版换容器
    都不会让页码错位。
    """
    pages: dict[int, str] = {}
    media_id = ""
    for matched in _THUMB_PAGE.finditer(page):
        media_id = media_id or matched.group(1)
        suffix = matched.group(3).lower()
        pages.setdefault(
            int(matched.group(2)), ".jpg" if suffix in {"jpg", "jpeg"} else f".{suffix}"
        )
    if not media_id or not pages:
        return None

    title_match = _PRETTY_TITLE.search(page)
    title = html_module.unescape(title_match.group(1)).strip() if title_match else ""
    return ParsedGallery(
        comic_id=comic_id,
        media_id=media_id,
        title=title,
        page_paths=[
            f"galleries/{media_id}/{number}{pages[number]}" for number in sorted(pages)
        ],
    )


def build_image_urls(image_base: str, page_paths: list[str]) -> list[str]:
    """把图床域名与每页相对路径拼成完整地址。"""
    base = image_base.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    return [f"{base}/{path.lstrip('/')}" for path in page_paths]


def default_image_base() -> str:
    return DEFAULT_IMAGE_DOMAIN


def default_site_domains() -> list[str]:
    return [
        domain.strip() for domain in DEFAULT_SITE_DOMAINS.splitlines() if domain.strip()
    ]


# ---- 页面抓取（只有兜底路径用得到） ----


def import_cloudscraper() -> ModuleType | None:
    """惰性 import cloudscraper；未安装时返回 None，兜底路径直接跳过。"""
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
    headers: object
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
        user_agent: str = USER_AGENT,
    ) -> None:
        self._timeout = max(1, timeout)
        self._proxy = proxy.strip()
        self._user_agent = user_agent
        self._lock = threading.Lock()
        self._session: _ScraperSession | None = None

    def fetch_text(self, url: str) -> str:
        """抓页面 HTML；非 200 一律翻译成面向用户的 ChannelError。"""
        response = self._get(url)
        if response.status_code == 404:
            logger.warning(f"[{PLUGIN_NAME}] nhentai 页面 404：{url}")
            raise ChannelError("作品不存在或已下架，确认一下 ID 或链接")
        if response.status_code != 200:
            logger.warning(
                f"[{PLUGIN_NAME}] nhentai 页面 HTTP {response.status_code}：{url}"
            )
            raise ChannelError(
                f"页面返回 HTTP {response.status_code}：多半是站点风控，稍后重试或开代理"
            )
        return response.text

    def fetch_bytes(self, url: str) -> bytes | None:
        """抓二进制内容（图床退回路径用）；失败返回 None，由调用方记为该张失败。"""
        response = self._get(url)
        if response.status_code != 200:
            return None
        return response.content

    def _get(self, url: str) -> _ScraperResponse:
        with self._lock:
            session = self._session_or_raise()
            try:
                return session.get(url, timeout=self._timeout)
            except _network_error_types() as exc:
                logger.warning(f"[{PLUGIN_NAME}] nhentai 请求 {url} 失败：{exc}")
                raise ChannelError("请求没发出去：稍后再试；一直失败请开代理") from exc

    def _session_or_raise(self) -> _ScraperSession:
        if self._session is not None:
            return self._session
        cloudscraper = import_cloudscraper()
        if cloudscraper is None:
            raise ChannelError(
                "页面抓取兜底需要 cloudscraper，请安装依赖后重载插件（或直接用 API 路径）"
            )
        session: _ScraperSession = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        )
        session.headers.update(
            {
                "User-Agent": self._user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        if self._proxy:
            session.proxies = {"http": self._proxy, "https": self._proxy}
        else:
            # 显式关掉环境代理：容器里常带系统代理变量，不覆盖会把请求绕到不存在的代理上
            session.proxies = {}
        session.trust_env = False
        self._session = session
        return session

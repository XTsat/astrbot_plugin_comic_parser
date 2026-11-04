"""wnacg 站内抓取与页面解析。

站点在 Cloudflare 后面，抓页有三条硬约束（都来自上游实现的实测结论，照抄即可）：

1. **每分钟 50 次**：超过 60 次/分钟会直接回 429，所以分页请求统一走滑动窗口限速；
2. **必须桌面 UA**：移动 UA 会拿到移动版布局，下面的解析规则全部失效；
3. **游客会 404**：部分图集与「仅登录可见」的分类对未登录访客返回 404，因此 `/photos-` 的
   404 不能一律当成「作品不存在」，要提示「可能需要登录」。

图址页（`-gallery-`）返回的图片地址是**协议相对 URL + 时效签名**：
`//img5.wnimg2.cfd/data/4015/00/001.jpg?verify=<时间戳>-<签名>`。签名随图址下发，
所以拿到图址就要尽快下载，不要缓存复用、也不要去掉 query。
"""

from __future__ import annotations

import asyncio
import html as html_module
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

from ....domain import ChannelError
from ....domain.constants import PLUGIN_NAME
from .constants import PAGE_REQUESTS_PER_MINUTE, USER_AGENT

if TYPE_CHECKING:  # 仅为类型标注；运行时按下面的惰性分支处理依赖缺失
    import aiohttp
else:
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - AstrBot 运行时自带 aiohttp
        aiohttp = None

# 这几种状态说明请求被 Cloudflare 挡下，而不是「页面不存在」
_CHALLENGE_STATUS = frozenset({403, 429, 503})

# 详情页的 `<h2>` 就是作品标题
_TITLE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.DOTALL | re.IGNORECASE)
# 标签区：`<div class="addtags">標籤：<a class="tagshow" …>`，取其中的 a 文本
_TAGS_BLOCK = re.compile(
    r'class="addtags"[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE
)
_TAG_LINK = re.compile(
    r"<a[^>]*class=\"[^\"]*\btagshow\b[^\"]*\"[^>]*>(.*?)</a>",
    re.DOTALL | re.IGNORECASE,
)
# 合辑的章节入口：data-chid 是子相册自己的 aid（不是章节序号）
_CHAPTER_LINK = re.compile(
    r"<a[^>]*class=\"[^\"]*\btagshow\b[^\"]*\"[^>]*data-chid=\"(\d+)\"[^>]*>(.*?)</a>",
    re.DOTALL | re.IGNORECASE,
)
# 章节列表自身的翻页：/photos-index-aid-401500-page-2.html
_CHAPTER_PAGE_HREF = re.compile(r"/photos-index-aid-\d+-page-(\d+)\.html")
# 图址：协议相对 URL，可能带 ?verify=<时间戳>-<签名>
_IMAGE_URL = re.compile(
    r"""//[^\s"'\\]+\.(?:jpe?g|png|webp|gif)(?:\?[^\s"'\\]*)?""", re.IGNORECASE
)
_TAG_STRIP = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_FIRST_DIGITS = re.compile(r"\d+")
# 用户可能贴站内链接：/photos-index-aid-<aid>[-sid-<sid>].html
_ALBUM_IN_TEXT = re.compile(
    r"/photos-(?:index|slide|list|gallery)(?:-page-\d+)?-aid-(\d+)(?:-sid-(\d+))?"
)


@dataclass(slots=True)
class AlbumPage:
    """一次详情页解析的结果。"""

    title: str = ""
    tags: list[str] = field(default_factory=list)
    # 合辑才有：子相册的 (aid, 标题)，按站内原始顺序（新→旧）；调用方负责反转成阅读顺序
    chapter_ids: list[tuple[str, str]] = field(default_factory=list)
    # 章节列表自身的页数（合辑章节多时才 >1）
    chapter_last_page: int = 1


def chapter_page_count(document: str) -> int:
    """章节列表最多翻到第几页：取分页器里出现过的最大页码。"""
    pages = [int(value) for value in _CHAPTER_PAGE_HREF.findall(document)]
    return max(pages) if pages else 1


def parse_album(document: str) -> AlbumPage:
    """解析详情页：标题、标签与合辑章节。

    标题缺失时按「游客可能拿不到内容」处理——上游遇到这种情况也是提示需要登录。
    """
    matched_title = _TITLE.search(document)
    title = _clean(matched_title.group(1)) if matched_title else ""
    if not title:
        raise ChannelError("没有解析到作品标题，可能是内容不存在或需要登录 wnacg")

    tags: list[str] = []
    block = _TAGS_BLOCK.search(document)
    if block:
        for raw in _TAG_LINK.findall(block.group(1)):
            tag = _clean(raw)
            if tag and tag not in tags:
                tags.append(tag)

    chapters: list[tuple[str, str]] = []
    seen: set[str] = set()
    for aid, raw_name in _CHAPTER_LINK.findall(document):
        if aid in seen:
            continue
        seen.add(aid)
        chapters.append((aid, _clean(raw_name) or aid))

    return AlbumPage(
        title=title,
        tags=tags,
        chapter_ids=chapters,
        chapter_last_page=chapter_page_count(document),
    )


def parse_chapter_ids(document: str) -> list[tuple[str, str]]:
    """只解析章节入口（合辑章节列表的第 2、3… 页用，这些页没有标题可依赖）。"""
    chapters: list[tuple[str, str]] = []
    seen: set[str] = set()
    for aid, raw_name in _CHAPTER_LINK.findall(document):
        if aid in seen:
            continue
        seen.add(aid)
        chapters.append((aid, _clean(raw_name) or aid))
    return chapters


def parse_gallery_images(document: str) -> list[str]:
    """从图址页里抠出全部图片地址，补全协议并去重（保持页面顺序）。"""
    urls: list[str] = []
    seen: set[str] = set()
    for raw in _IMAGE_URL.findall(document):
        # 页面里给的是协议相对地址（`//img…`），补成 https 直链
        url = "https://" + raw.lstrip("/")
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


# 按文件头判断真实格式：站点的图址扩展名不可信
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF8", ".gif"),
)


def sniff_image_suffix(payload: bytes) -> str | None:
    """按文件头判断图片真实后缀；不是图片则返回 None。

    站点的图址扩展名不可信：实测 `009.jpg` 实际是 WebP、`041.png` 才是 PNG。按真实格式
    落盘，下游（PDF/长图打包、用户解压后看图）才不会踩「扩展名与内容不符」；
    顺带把「200 但返回风控页/HTML」这种假成功挡掉，不写进产物。
    """
    if len(payload) < 12:
        return None
    for signature, suffix in _IMAGE_SIGNATURES:
        if payload.startswith(signature):
            return suffix
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"
    if payload[4:8] == b"ftyp" and payload[8:12] in (b"avif", b"avis"):
        return ".avif"
    return None


def gallery_path(gallery_id: str) -> str:
    """详情页路径 → 图址页路径（把 `-index-` 换成 `-gallery-`）。"""
    return f"/photos-gallery-aid-{gallery_id}.html"


def album_path(gallery_id: str) -> str:
    return f"/photos-index-aid-{gallery_id}.html"


def chapter_list_path(gallery_id: str, page: int) -> str:
    return f"/photos-index-aid-{gallery_id}-page-{page}.html"


def _clean(fragment: str) -> str:
    """去标签、反转义、压空白：页面里的标题与标签都带零碎的 HTML。"""
    return _WHITESPACE.sub(" ", html_module.unescape(_TAG_STRIP.sub("", fragment))).strip()


class WnacgSite:
    """站内页面抓取器：镜像降级 + 限速 + Cloudflare 兜底。

    会话在渠道的一次任务里复用（`async with`），因此 Cloudflare 的 cookie 能跨请求留存；
    图片下载不走这里（图床是独立域名、并发在 `downloader.py`），只有图片被风控时才回落到
    `fetch_bytes()` 用浏览器指纹重取一次。
    """

    def __init__(
        self, *, domains: list[str], timeout: int, proxy: str = ""
    ) -> None:
        self._domains = [item.strip().strip("/") for item in domains if item.strip()]
        if not self._domains:
            raise ChannelError("未配置任何 wnacg 站点域名，请在渠道配置里填写")
        self._timeout = max(1, timeout)
        self._proxy = proxy.strip()
        self._session: Any = None
        self._request_times: list[float] = []
        self._throttle_lock = asyncio.Lock()
        self._scraper: Any = None
        self._scraper_lock = threading.Lock()

    # ---- 生命周期 ----
    async def __aenter__(self) -> WnacgSite:
        if aiohttp is None:
            raise ChannelError("缺少 aiohttp 依赖，无法访问 wnacg")
        self._session = aiohttp.ClientSession(
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            # 代理只从配置来：开关关掉时是真直连，不受容器环境变量影响
            trust_env=False,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ---- 对外抓取：站内路径（会按镜像清单降级） ----
    async def fetch_page(self, path: str) -> str:
        last_error: Exception | None = None
        for domain in self._domains:
            url = f"https://{domain}{path}"
            try:
                return await self._get_text(url)
            except ChannelError as exc:
                last_error = exc
                logger.warning(
                    f"[{PLUGIN_NAME}] wnacg 抓取 {url} 失败，换下一个域名：{exc}"
                )
        raise ChannelError(f"wnacg 页面抓取失败：{last_error}")

    # ---- 对外抓取：图床绝对地址（图片风控时由下载器调用） ----
    async def fetch_bytes(self, url: str) -> bytes | None:
        try:
            return await self._get_bytes(url)
        except ChannelError as exc:
            logger.debug(f"[{PLUGIN_NAME}] wnacg 图床兜底取图失败 {url}：{exc}")
            return None

    @property
    def referer(self) -> str:
        return f"https://{self._domains[0]}/"

    # ---- 内部 ----
    async def _get_text(self, url: str) -> str:
        status, payload = await self._request(url, binary=False)
        text = payload if isinstance(payload, str) else payload.decode("utf-8", "ignore")
        if text.strip():
            return text
        raise ChannelError(f"页面返回空内容（HTTP {status}）：{url}")

    async def _get_bytes(self, url: str) -> bytes:
        status, payload = await self._request(url, binary=True)
        if status != 200 or not payload:
            raise ChannelError(f"图片请求失败（HTTP {status}）：{url}")
        return payload

    async def _request(self, url: str, *, binary: bool) -> tuple[int, Any]:
        """发一次请求；遇到 Cloudflare 状态码时改用浏览器指纹兜底一次。"""
        await self._throttle()
        assert self._session is not None
        try:
            async with self._session.get(
                url,
                timeout=self._timeout,
                proxy=self._proxy or None,
                headers={"Referer": self.referer},
            ) as response:
                status = response.status
                if status == 200:
                    payload: Any = (
                        await response.read() if binary else await response.text()
                    )
                    return status, payload
        except (asyncio.TimeoutError, OSError) as exc:
            raise ChannelError(f"请求 {url} 失败：{exc}") from exc

        if status in _CHALLENGE_STATUS:
            logger.info(
                f"[{PLUGIN_NAME}] wnacg 返回 HTTP {status}（疑似风控），改用抓页器重试：{url}"
            )
            return await asyncio.to_thread(self._fetch_via_scraper, url, binary)
        if status == 404:
            raise ChannelError(
                "内容不存在或需要登录 wnacg（游客访问部分图集与分类会返回 404）"
            )
        raise ChannelError(f"页面返回 HTTP {status}：{url}")

    async def _throttle(self) -> None:
        """滑动窗口限速：超过配额就等到窗口里最老的那次请求过期。"""
        while True:
            wait = 0.0
            async with self._throttle_lock:
                now = time.monotonic()
                self._request_times = [
                    stamp for stamp in self._request_times if now - stamp <= 60
                ]
                if len(self._request_times) < PAGE_REQUESTS_PER_MINUTE:
                    self._request_times.append(now)
                    return
                wait = 60 - (now - self._request_times[0])
            logger.info(
                f"[{PLUGIN_NAME}] wnacg 已达 {PAGE_REQUESTS_PER_MINUTE} 次/分钟上限，等待 {wait:.0f}s"
            )
            await asyncio.sleep(max(wait, 0.5))

    def _fetch_via_scraper(self, url: str, binary: bool) -> tuple[int, Any]:
        """Cloudflare 兜底：用 cloudscraper 的浏览器指纹再取一次（同步，跑在线程里）。"""
        session = self._scraper_or_raise()
        with self._scraper_lock:
            response = session.get(url, timeout=self._timeout)
        if response.status_code != 200:
            raise ChannelError(
                f"抓页器返回 HTTP {response.status_code}（多半是 Cloudflare 挑战）：{url}"
            )
        return response.status_code, response.content if binary else response.text

    def _scraper_or_raise(self) -> Any:
        if self._scraper is not None:
            return self._scraper
        try:
            import cloudscraper
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的降级路径
            raise ChannelError(
                "绕过 wnacg 的 Cloudflare 风控需要 cloudscraper，请安装依赖后重载插件"
            ) from exc
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        )
        scraper.headers.update({"User-Agent": USER_AGENT})
        scraper.trust_env = False
        # 显式给定代理：为空时置空字典，避免容器里的系统代理变量把请求绕到不存在的代理
        scraper.proxies = (
            {"http": self._proxy, "https": self._proxy} if self._proxy else {}
        )
        self._scraper = scraper
        return scraper


def normalize_url_id(raw: str) -> str | None:
    """从站内链接里取出相册 ID；合辑子相册链接带 sid 时以 sid 为准（它才是相册 itself）。"""
    matched = _ALBUM_IN_TEXT.search(raw)
    if not matched:
        return None
    aid, sid = matched.group(1), matched.group(2)
    return sid or aid


def first_digits(raw: str) -> str | None:
    matched = _FIRST_DIGITS.search(raw)
    return matched.group(0) if matched else None

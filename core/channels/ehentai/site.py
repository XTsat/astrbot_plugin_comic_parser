"""E-Hentai 页面解析：作品页缩略图、图片页地址、图片字节嗅探。

站点没有「一次拿全图片地址」的接口（`gdata` 只给元信息），所以整本下载靠两层 HTML：

- 作品页 `/g/<gid>/<token>/?p=N`：每页 20（或用户设置的 40/100）个缩略图，链接形如
  `/s/<pagehash>/<gid>-<index>`，指向图片页；
- 图片页 `/s/<pagehash>/<gid>-<index>`：`#img` 是 1280px 重采样图（匿名可下），
  「Download original」是 `/fullimg/...` 原图（需要登录）。

HTML 里没有稳定版本号，站点改版时这里最先坏；解析失败会给出「站点结构可能已变化」，
不会静默返回空列表。
"""

from __future__ import annotations

import html as html_module
import re
from urllib.parse import quote
from dataclasses import dataclass, field

from astrbot.api import logger

from ....domain import ChannelError
from ....domain.constants import PLUGIN_NAME
from .api import EHQuotaExceeded

# 缩略图链接：`/s/<pagehash>/<gid>-<index>`，主机名可能省略
_THUMB_LINK = re.compile(
    r'href="((?:https?://[^"]+)?/s/[0-9A-Za-z]+/(\d+)-(\d+))"', re.IGNORECASE
)
# 图片页里的主图：`<img id="img" src="…">`（属性顺序不固定，先取整段标签再找 src）
_IMG_TAG = re.compile(r"<img[^>]*\bid=[\"']img[\"'][^>]*>", re.IGNORECASE)
_SRC_ATTR = re.compile(r"\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
# 原图链接：`/fullimg/<gid>/<page>/<imgkey>/<filename>`
_FULLIMG_LINK = re.compile(
    r"href=[\"']((?:https?://[^\"']+)?/fullimg/[^\"']+)[\"']", re.IGNORECASE
)
_ARTIST_PREFIX = "artist:"


@dataclass(slots=True)
class GalleryMeta:
    """`gdata` 返回的单本元信息，字段名与站点 JSON 对齐。"""

    gid: str = ""
    token: str = ""
    title: str = ""
    title_jpn: str = ""
    category: str = ""
    uploader: str = ""
    filecount: int = 0
    filesize: int = 0
    tags: list[str] = field(default_factory=list)

    @property
    def display_title(self) -> str:
        """优先原名，没有再退回英文标题；都为空时用 gid 兜底。"""
        return self.title_jpn.strip() or self.title.strip() or self.gid

    @property
    def author(self) -> str:
        """作者取 artist 标签（可能多个），没有 artist 时退回上传者。"""
        artists = [
            tag.split(":", 1)[1].strip()
            for tag in self.tags
            if tag.lower().startswith(_ARTIST_PREFIX)
        ]
        names = list(dict.fromkeys(name for name in artists if name))
        return "、".join(names) or self.uploader.strip()


@dataclass(slots=True)
class ImageLinks:
    """一张图片页上能拿到的两个地址：重采样必在，原图可能要登录。"""

    resample: str
    original: str = ""


def parse_gallery_meta(item: dict[str, object]) -> GalleryMeta:
    """把 `gdata` 的单条 gmetadata 收敛成 `GalleryMeta`；不可见/已删除直接报错。"""
    error = item.get("error")
    if error:
        logger.warning(f"[{PLUGIN_NAME}] E-Hentai 作品不可用：{error}")
        raise ChannelError("E-Hentai 说这本作品不可用：可能已下架，或当前账号看不到")
    if _as_bool(item.get("expunged")):
        raise ChannelError("这本作品已被站点删除，无法下载")

    filecount = _as_int(item.get("filecount"))
    if filecount <= 0:
        raise ChannelError("作品页数为 0，可能已被删除或当前不可见")

    raw_tags = item.get("tags")
    tags = [
        str(tag).strip()
        for tag in raw_tags
        if isinstance(tag, str) and tag.strip()
    ] if isinstance(raw_tags, list) else []

    return GalleryMeta(
        gid=str(item.get("gid") or "").strip(),
        token=str(item.get("token") or "").strip(),
        title=_clean_text(item.get("title")),
        title_jpn=_clean_text(item.get("title_jpn")),
        category=_clean_text(item.get("category")),
        uploader=_clean_text(item.get("uploader")),
        filecount=filecount,
        filesize=_as_int(item.get("filesize")),
        tags=tags,
    )


def gallery_url(host: str, gid: str, token: str, page: int = 0) -> str:
    """作品页地址；`page` 是站点分页（从 0 开始），0 时不带查询串。"""
    base = f"https://{host}/g/{gid}/{token}/"
    return base if page <= 0 else f"{base}?p={page}"


def parse_gallery_page(html_text: str, host: str) -> list[str]:
    """从作品页里解析图片页地址，按作品内页序排序。

    同一页可能因为推荐位/重复渲染出现同一个链接，这里按 URL 去重；
    `index` 是作品内页码（1 起），用作排序依据。
    """
    found: list[tuple[int, str]] = []
    for href, _gid, index in _THUMB_LINK.findall(html_text):
        try:
            order = int(index)
        except ValueError:
            continue
        found.append((order, _absolute(host, href)))
    found.sort(key=lambda pair: pair[0])

    urls: list[str] = []
    seen: set[str] = set()
    for _order, url in found:
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_image_page(html_text: str, host: str) -> ImageLinks:
    """从图片页里取重采样图与原图地址；被登录墙/配额挡住时抛对应异常。"""
    tag_match = _IMG_TAG.search(html_text)
    if tag_match is None:
        raise ChannelError("图片页里没有找到图片地址，站点结构可能已变化")
    src_match = _SRC_ATTR.search(tag_match.group(0))
    if src_match is None:
        raise ChannelError("图片页里的图片标签缺少 src，站点结构可能已变化")

    resample = _absolute(host, src_match.group(1))
    if is_quota_gif(resample):
        raise EHQuotaExceeded("E-Hentai 返回 509 占位图：图片配额已用尽")

    original = ""
    fullimg_match = _FULLIMG_LINK.search(html_text)
    if fullimg_match is not None:
        original = _absolute(host, fullimg_match.group(1))
        if is_quota_gif(original):
            original = ""
    return ImageLinks(resample=resample, original=original)


# ---- 搜索结果 ----

# 搜索结果行：`<tr class="gtr">`，标题在 `div.glink`，页数在 `glmeta`（"123 pages"）
_SEARCH_ROW = re.compile(
    r'<tr[^>]*class="[^"]*\bgtr\b[^"]*"[^>]*>(.*?)</tr>',
    re.DOTALL | re.IGNORECASE,
)
_SEARCH_GALLERY = re.compile(
    r'href="(?:https?://[^"/]+)?/g/(\d+)/([0-9a-fA-F]{10})/?"', re.IGNORECASE
)
_SEARCH_TITLE = re.compile(
    r'class="[^"]*\bglink\b[^"]*"[^>]*>(.*?)</',
    re.DOTALL | re.IGNORECASE,
)
_SEARCH_PAGES = re.compile(r"(\d+)\s*pages?", re.IGNORECASE)
_SEARCH_PAGE_LINK = re.compile(r"[?&]page=(\d+)", re.IGNORECASE)


@dataclass(slots=True)
class SearchHit:
    """搜索结果里的一条作品；ID 是 gid + token 两段。"""

    gid: str
    token: str
    title: str = ""
    page_count: int = 0


def ehentai_search_url(host: str, keyword: str, page: int = 0) -> str:
    """站内搜索页；`page` 从 0 开始（站点分页是 0 基）。"""
    return (
        f"https://{host}/?f_search={quote(keyword, safe='')}"
        f"&page={max(0, int(page))}"
    )


def ehentai_popular_url(host: str) -> str:
    """站点「Popular Right Now」页：按近期收藏热度排的一屏作品，没有时间窗参数。"""
    return f"https://{host}/popular"


def parse_search_results(
    document: str, page: int = 0
) -> tuple[list[SearchHit], bool]:
    """解析搜索页；返回（条目，是否还有下一页）。

    行结构缺失时退回「按作品链接找附近标题」的兜底；解析不到就返回空列表，
    由上层按「没搜到」展示，而不是抛未预期异常。分页链接是 0 基的绝对页码，
    所以要和当前页比较才能判断后面还有没有。
    """
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for row in _SEARCH_ROW.findall(document):
        matched = _SEARCH_GALLERY.search(row)
        if matched is None:
            continue
        gid, token = matched.group(1), matched.group(2).lower()
        if gid in seen:
            continue
        seen.add(gid)
        title_match = _SEARCH_TITLE.search(row)
        title = _clean_text(title_match.group(1)) if title_match else ""
        pages_match = _SEARCH_PAGES.search(row)
        hits.append(
            SearchHit(
                gid=gid,
                token=token,
                title=title,
                page_count=int(pages_match.group(1)) if pages_match else 0,
            )
        )

    if not hits:
        for matched in _SEARCH_GALLERY.finditer(document):
            gid, token = matched.group(1), matched.group(2).lower()
            if gid in seen:
                continue
            window = document[max(0, matched.start() - 800) : matched.end() + 800]
            title_match = _SEARCH_TITLE.search(window)
            if title_match is None:
                continue
            seen.add(gid)
            pages_match = _SEARCH_PAGES.search(window)
            hits.append(
                SearchHit(
                    gid=gid,
                    token=token,
                    title=_clean_text(title_match.group(1)),
                    page_count=int(pages_match.group(1)) if pages_match else 0,
                )
            )

    pages = [int(value) for value in _SEARCH_PAGE_LINK.findall(document)]
    has_more = max(pages) > page if pages else len(hits) >= 25
    return hits, has_more


# ---- 风控/异常页识别 ----


def is_login_wall(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker.lower() in lowered for marker in _LOGIN_MARKERS)


# 只认「登录页正文」的特征：普通页面的导航栏里也有 bounce_login 链接，
# 拿整页 HTML 搜 bounce_login 会把每一本正常作品都误判成需要登录
_LOGIN_MARKERS = (
    "This page requires you to log on",
    "You must be logged in to view this",
    "Sad Panda",
)


def is_banned(text: str) -> bool:
    lowered = (text or "").lower()
    return "temporarily banned" in lowered or "automated mirroring" in lowered


def is_content_warning(text: str) -> bool:
    lowered = (text or "").lower()
    return (
        "offensive for everyone" in lowered
        or "content warning" in lowered
        or "has been flagged as offensive" in lowered
    )


def is_quota_gif(url: str) -> bool:
    return "509.gif" in (url or "").lower()


def is_sadpanda(url: str) -> bool:
    return "sadpanda" in (url or "").lower()


# ---- 图片嗅探 ----


def sniff_image_suffix(content_type: str, payload: bytes) -> str | None:
    """按 Content-Type 与文件头判断图片后缀；不是图片时返回 None。

    站点图址的扩展名并不可信（H@H 会给 `.webp` 也可能给无扩展名路径），
    落盘名必须按实际字节定，否则打包时会被当成非图片漏掉。
    """
    ctype = (content_type or "").lower()
    if "image/webp" in ctype:
        return ".webp"
    if "image/jpeg" in ctype or "image/jpg" in ctype:
        return ".jpg"
    if "image/png" in ctype:
        return ".png"
    if "image/gif" in ctype:
        return ".gif"
    if "image/avif" in ctype:
        return ".avif"
    if "image/bmp" in ctype:
        return ".bmp"

    head = payload[:16]
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:2] == b"BM":
        return ".bmp"
    if head[4:12] == b"ftypavif":
        return ".avif"
    return None


# ---- 内部工具 ----


def _absolute(host: str, url: str) -> str:
    """把页面里的相对地址补成绝对地址；已经是绝对地址的原样返回。"""
    value = html_module.unescape((url or "").strip())
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("/"):
        return f"https://{host}{value}"
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"https://{host}/{value.lstrip('/')}"


def _clean_text(raw: object) -> str:
    return html_module.unescape(str(raw or "")).strip()


def _as_int(raw: object) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def _as_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "1", "yes"}
    return False

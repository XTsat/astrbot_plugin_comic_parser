"""Hanime1 漫画渠道的 `BaseChannel` 实现：拿 ID → 取元信息 → 下载图片。

站点细节（抓页、解析、图床地址构造）在 `site.py`，图片下载在 `downloader.py`，
打包与加密完全交给上层，所以这里看不到任何 zip/pdf/密码相关逻辑。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, ClassVar

from astrbot.api import logger

from ....domain import (
    ChannelError,
    ComicMeta,
    DownloadedComic,
    DownloadError,
    PluginSettings,
    RankingItem,
    RankingPage,
    RankingUnsupportedError,
    SearchItem,
    SearchPage,
    SearchQuery,
)
from ....domain.constants import PLUGIN_NAME
from ....utils import host_matches, split_link
from ...channel import (
    BaseChannel,
    CancelToken,
    ProgressCallback,
    is_search_proxy_override,
    split_option_list,
)
from ...progress import ProgressHeartbeat
from ...search_query import fallback_note, plain_search_text
from ...domain_race import ProbeFn, http_probe
from .constants import (
    DEFAULT_FETCH_PROXY,
    DEFAULT_USER_AGENT,
    IMAGE_HOST_FALLBACKS,
    DEFAULT_IMAGE_CONCURRENCY,
    DEFAULT_IMAGE_DOMAINS,
    DEFAULT_IMAGE_RETRY_TIMES,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SITE_DOMAINS,
    MIN_IMAGE_BYTES,
    PATH_DETAIL,
    PATH_READER,
)
from .downloader import ImageDownloader
from .progress import (
    DownloadSession,
    cancel_active_downloads,
    format_size,
    register_session,
    unregister_session,
)
from .site import (
    PageScraper,
    ParsedComic,
    build_image_urls,
    build_page_url,
    challenge_markers,
    comic_ranking_path,
    comic_search_path,
    parse_comic_search,
    parse_detail,
    parse_reader,
    search_has_more,
)

# 作品页 /comic/<ID>，也兼容 /comic/<ID>/<页> 与分享文本
_COMIC_IN_TEXT = re.compile(r"/comic/(\d+)", re.IGNORECASE)
# 视频页 /watch?v=<ID>：本渠道只做漫画，输入视频链接时给一句明确提示
_VIDEO_IN_TEXT = re.compile(r"/watch\?v=\d+", re.IGNORECASE)
_FIRST_DIGITS = re.compile(r"\d+")


# 榜单窗口 → 站内热度档位。Hanime1 只有「本日/本週/所有」三档，没有月榜
_BOARD_SORT = {"day": "popular-today", "week": "popular-week", "all": "popular"}


class Hanime1Channel(BaseChannel):
    """按作品 ID 下载整本 Hanime1 漫画。

    两级域名：页面抓取失败（Cloudflare 拦截、镜像维护）就换下一个站点域名；
    图床单独配一份域名清单，因为原图在 nhentai 图床，与页面域名不同源。
    """

    name: ClassVar[str] = "hanime1"
    command: ClassVar[str] = "hanime1"
    # 站点简称的首字母有两种写法（ha / hm），都认；展示与计数都用第一个
    command_aliases: ClassVar[tuple[str, ...]] = ("hm", "ha", "hanime")
    display_name: ClassVar[str] = "Hanime1 漫画"
    website: ClassVar[str] = "https://hanime1.me"
    link_template: ClassVar[str] = "https://hanime1.me/comic/<ID>"
    default_use_proxy: ClassVar[bool] = True
    # cloudscraper 不是硬依赖：配了读取代理时用 requests 也能抓页，所以只在真正需要时提示
    requires: ClassVar[tuple[str, ...]] = ("aiohttp",)

    def __init__(self, options: Mapping[str, Any], settings: PluginSettings) -> None:
        super().__init__(options, settings)
        # fetch_meta 刚抓过详情页，download 直接复用，省掉一次 Cloudflare 抓取
        self._parsed: dict[str, ParsedComic] = {}
        # 抓页会话带 Cloudflare 的 cookie，跨「取元信息」与「下载」复用能少挨一次挑战
        self._scraper_instance: PageScraper | None = None

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        """内置渠道默认启用：域名用官方站，并发与重试取稳一点的默认值。"""
        return {
            "enable": True,
            "site_domains": DEFAULT_SITE_DOMAINS,
            "image_domains": "",
            "request_timeout": DEFAULT_REQUEST_TIMEOUT,
            "max_concurrent_images": DEFAULT_IMAGE_CONCURRENCY,
            "retry_times": DEFAULT_IMAGE_RETRY_TIMES,
            "use_proxy": True,
            "fetch_proxy": DEFAULT_FETCH_PROXY,
        }

    # ---- ID 解析 ----
    def match_link(self, text: str) -> str | None:
        # `/comic/<id>` 是通用路径，别的站点（如哔咔网页版）也用同一个形状，
        # 所以这里必须先认域名，否则会把人家的链接抢过来
        host, path = split_link(text)
        domains = self.opt_list("site_domains") or split_option_list(DEFAULT_SITE_DOMAINS)
        if not host_matches(host, domains):
            return None
        matched = _COMIC_IN_TEXT.search(path)
        return matched.group(1) if matched else None

    def normalize_id(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            raise ChannelError(
                f"请提供作品 ID 或链接，例如 /{self.display_command} 100445"
            )
        if _VIDEO_IN_TEXT.search(text):
            raise ChannelError(
                "这看起来是 Hanime1 的视频链接，本渠道只下载漫画；"
                f"请换成漫画链接（…/comic/<ID>）或直接给数字 ID"
            )

        matched = _COMIC_IN_TEXT.search(text)
        if matched:
            return matched.group(1)
        if text.isdigit():
            return text
        raise ChannelError("无法从输入中识别出 Hanime1 漫画 ID，请直接给出数字 ID")

    # ---- 元信息 ----
    async def fetch_meta(self, comic_id: str, work_dir: Path) -> ComicMeta:
        parsed = await self._load_detail(comic_id)
        self._parsed[comic_id] = parsed
        return self._build_meta(comic_id, parsed)

    # ---- 下载 ----
    async def download(
        self,
        comic_id: str,
        work_dir: Path,
        on_progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> DownloadedComic:
        parsed = self._parsed.pop(comic_id, None)
        if parsed is None:
            # 插件重载或任务重试会走到这里：不依赖前一次 fetch_meta 的缓存，重新抓页
            parsed = await self._load_detail(comic_id)

        # 阅读页是图片清单的权威来源：media_id、页数、每页扩展名都在它的 data-* 与内嵌数组里
        parsed = await self._load_reader(parsed)
        image_base = await self._resolve_image_base(parsed)
        urls = build_image_urls(image_base, parsed.media_id, parsed.extensions)
        if not urls:
            raise DownloadError(
                "没有解析到任何图片地址，站点结构可能已变化；请在插件日志里确认抓取是否成功"
            )

        session = DownloadSession(comic_id=comic_id, total=len(urls), cancel=cancel)
        register_session(session)
        logger.info(
            f"[{PLUGIN_NAME}] 开始下载 {self.display_name} {comic_id}："
            f"{len(urls)} 页，图床 {image_base}，网络 {self.proxy_summary()}"
        )
        heartbeat = ProgressHeartbeat(session.state, on_tick=on_progress)
        heartbeat.start()
        try:
            stats = await self._build_downloader().download(
                urls, work_dir, session=session, on_progress=on_progress
            )
        finally:
            await heartbeat.stop()
            unregister_session(session)

        if stats.success <= 0:
            raise DownloadError(
                f"图片一张都没下载成功（共 {stats.total} 张），可能是图床被风控拦截或网络不通，"
                "请在渠道配置里开启代理后重试"
            )
        if stats.failed:
            logger.warning(
                f"[{PLUGIN_NAME}] {self.display_name} {comic_id} 有 "
                f"{stats.failed}/{stats.total} 张图片失败，产物将缺页"
            )
        logger.info(
            f"[{PLUGIN_NAME}] 下载结束 {self.display_name} {comic_id}："
            f"{stats.success}/{stats.total} 张 · {format_size(session.bytes_downloaded)} · "
            f"耗时 {session.elapsed():.1f}s"
        )
        return DownloadedComic(
            meta=self._build_meta(comic_id, parsed),
            source_dir=work_dir,
            image_count=stats.success,
            failed_images=stats.failed,
        )

    def cancel_downloads(self) -> list[str]:
        """插件卸载时调用：让进行中的下载尽快收工并返回其状态（供上层写日志）。"""
        return cancel_active_downloads()

    # ---- 内部 ----
    def _build_meta(self, comic_id: str, parsed: ParsedComic) -> ComicMeta:
        return ComicMeta(
            channel=self.name,
            comic_id=comic_id,
            title=parsed.title,
            author=parsed.author,
            page_count=parsed.resolved_page_count,
            tags=parsed.tags,
        )

    async def _resolve_image_base(self, parsed: ParsedComic) -> str:
        """选一个真能连上的图床基地址。

        候选顺序：手动配置 → 阅读页给的 `data-prefix` → 内置镜像。之所以要探一遍而不是直接用
        页面给的地址：nhentai 的图床是一组镜像（i / i2 / i3 / i4），`i2` 常常只有 AAAA 记录，
        纯 IPv4 网络直接连不上——不探测就整本全失败。
        """
        candidates: list[str] = list(self.opt_list("image_domains"))
        if parsed.image_base:
            candidates.append(parsed.image_base)
        for host in IMAGE_HOST_FALLBACKS:
            candidates.append(f"https://{host}/galleries/{parsed.media_id}/")

        # 同一个图床换个写法只留第一次出现的那条，避免自己跟自己抢排名
        unique: list[str] = []
        seen: set[str] = set()
        for base in candidates:
            host = urlsplit(base).netloc or base
            if host in seen:
                continue
            seen.add(host)
            unique.append(base)
        if not unique:
            return DEFAULT_IMAGE_DOMAINS

        ranked = await self.ordered_domains(
            unique, self._image_probe(parsed), label="图床"
        )
        if ranked:
            return ranked[0]

        # 测速关闭或全部探测失败：退回逐个探活，至少保证「连不上的往后排」
        for base in unique:
            if await self._image_host_alive(base, parsed):
                return base
            host = urlsplit(base).netloc or base
            logger.warning(f"[{PLUGIN_NAME}] 图床 {host} 连不上，换下一个")

        logger.warning(
            f"[{PLUGIN_NAME}] 所有图床都探测失败，仍按 {unique[0]} 继续尝试"
        )
        return unique[0]

    def _image_probe(self, parsed: ParsedComic) -> ProbeFn:
        """图床探测面：拿一张真图量，既是探活也是测速（图床只影响下载，独立于页面域名）。"""
        suffix = parsed.extensions[0] if parsed.extensions else ".jpg"
        return http_probe(
            lambda base: build_image_urls(base, parsed.media_id, [suffix])[0],
            proxy=self._proxy(),
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": "https://hanime1.me/",
            },
        )

    async def _image_host_alive(self, base: str, parsed: ParsedComic) -> bool:
        """探一张图判断图床是否可用：能拿到真图片才算通。"""
        suffix = parsed.extensions[0] if parsed.extensions else ".jpg"
        url = build_image_urls(base, parsed.media_id, [suffix])[0]
        try:
            import aiohttp
        except ImportError:  # 缺 aiohttp 时不做探测，交给下载阶段报错
            return True
        timeout = self.opt_int("request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1)
        try:
            async with aiohttp.ClientSession(trust_env=not self._proxy()) as client:
                async with client.get(
                    url,
                    proxy=self._proxy() or None,
                    timeout=min(timeout, 20),
                    headers={"Referer": "https://hanime1.me/"},
                ) as response:
                    if response.status != 200:
                        return False
                    payload = await response.read()
                    return len(payload) >= 512
        except (asyncio.TimeoutError, OSError):
            return False

    async def _load_detail(self, comic_id: str) -> ParsedComic:
        """按站点域名清单依次抓详情页；被拦或解析失败就换下一个域名。"""
        return await self._fetch_and_parse(
            PATH_DETAIL.format(comic_id=comic_id),
            lambda page: parse_detail(comic_id, page),
            f"Hanime1 作品 {comic_id}",
        )

    async def _load_reader(self, parsed: ParsedComic) -> ParsedComic:
        """抓阅读页第 1 页，拿到图片清单；失败时退回详情页里已知的页数。"""
        try:
            return await self._fetch_and_parse(
                PATH_READER.format(comic_id=parsed.comic_id, page=1),
                lambda page: parse_reader(parsed.comic_id, page, parsed),
                f"Hanime1 阅读页 {parsed.comic_id}",
            )
        except ChannelError as exc:
            if parsed.extensions or parsed.page_count:
                logger.warning(
                    f"[{PLUGIN_NAME}] 阅读页抓取失败，改用详情页页数兜底：{exc}"
                )
                fallback = parsed.extensions or [".jpg"] * parsed.page_count
                return ParsedComic(
                    comic_id=parsed.comic_id,
                    media_id=parsed.media_id,
                    title=parsed.title,
                    author=parsed.author,
                    tags=parsed.tags,
                    page_count=parsed.page_count or len(fallback),
                    extensions=fallback,
                )
            raise

    async def _fetch_and_parse(
        self,
        path: str,
        parse: Callable[[str], ParsedComic],
        what: str,
    ) -> ParsedComic:
        domains = await self._site_domains(path)
        if not domains:
            raise ChannelError("未配置任何 Hanime1 站点域名，请在渠道配置里填写")

        scraper = self._scraper()
        last_error: Exception | None = None
        for domain in domains:
            url = build_page_url(domain, path)
            try:
                page = await asyncio.to_thread(scraper.fetch_text, url)
                return parse(page)
            except ChannelError as exc:
                last_error = exc
                logger.warning(f"[{PLUGIN_NAME}] 抓取 {url} 失败，换下一个域名：{exc}")

        detail = f"：{last_error}" if last_error is not None else ""
        raise ChannelError(f"{what} 抓取失败{detail}")

    # ---- 域名测速择优 ----
    async def _site_domains(self, path: str) -> list[str]:
        """按实测速度排好序的站点域名清单；测速不可用时就是配置顺序。"""
        domains = self.opt_list("site_domains") or split_option_list(DEFAULT_SITE_DOMAINS)
        if len(domains) < 2:
            return domains
        ranked = await self.ordered_domains(
            domains, self._page_probe(path), label="站点域名"
        )
        return ranked or domains

    def _page_probe(self, path: str) -> ProbeFn:
        """站点域名的探测面就是接下来要抓的那个页面，但**直连**走一遍。

        页面抓取默认经读取代理（jina），那条链路测到的是代理的延迟与缓存，而图片下载
        永远直连——按代理速度排域名只会把最慢的直连节点排到第一。挑战页以 200 返回，
        所以还要按拦截标记判死，不能只看状态码。
        """
        return http_probe(
            lambda domain: build_page_url(domain, path),
            proxy=self._proxy(),
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            reject_markers=challenge_markers(),
        )

    def _scraper(self) -> PageScraper:
        # 搜索超时后的代理重试必须用新的抓页器：缓存里那个绑定的是上一次的直连代理
        if self._scraper_instance is None or is_search_proxy_override():
            scraper = PageScraper(
                timeout=self.opt_int(
                    "request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1
                ),
                proxy=self._proxy(),
                fetch_proxy=self.opt_str("fetch_proxy", DEFAULT_FETCH_PROXY),
            )
            if not is_search_proxy_override():
                self._scraper_instance = scraper
            return scraper
        return self._scraper_instance

    # ---- 封面 ----
    async def fetch_cover(self, comic_id: str) -> bytes | None:
        """只取阅读页第 1 页的原图，供面板的「从渠道拉封面」用。

        和下载走同一条取址链路（详情页 → 阅读页第 1 页 → 图床择优），但只 GET 这一张、不落盘：
        阅读页是 media_id 与每页扩展名的权威来源，绕开它就等于猜图片地址。
        """
        try:
            parsed = await self._load_detail(comic_id)
            parsed = await self._load_reader(parsed)
            suffix = parsed.extensions[0] if parsed.extensions else ".jpg"
            image_base = await self._resolve_image_base(parsed)
            url = build_image_urls(image_base, parsed.media_id, [suffix])[0]
            payload = await self._fetch_cover_bytes(url)
        except ChannelError as exc:
            logger.warning(
                f"[{PLUGIN_NAME}] ⚠️ 取封面失败 {self.display_name} {comic_id}：{exc}"
            )
            return None
        if not payload:
            logger.warning(
                f"[{PLUGIN_NAME}] ⚠️ 取封面失败 {self.display_name} {comic_id}："
                "图床没有返回可用图片（可能风控、防盗链或图片地址已失效）"
            )
            return None
        return payload

    async def _fetch_cover_bytes(self, url: str) -> bytes | None:
        """GET 一张图的原始字节；只给「只要一张」的封面路径用，不进下载器（不落盘、不重试）。"""
        try:
            import aiohttp
        except ImportError:  # 缺 aiohttp 时交给依赖检查报错，封面直接算取不到
            return None
        timeout = self.opt_int("request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1)
        proxy = self._proxy()
        try:
            async with aiohttp.ClientSession(
                headers={
                    # 图床校验来源与 UA：缺了 Referer 部分节点会直接回 403
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Referer": "https://hanime1.me/",
                },
                # 代理只从配置来：开关关掉时是真直连，不受容器环境变量影响
                trust_env=False,
            ) as client:
                async with client.get(
                    url, proxy=proxy or None, timeout=timeout
                ) as response:
                    if response.status != 200:
                        return None
                    payload = await response.read()
        except (asyncio.TimeoutError, OSError, aiohttp.ClientError):
            return None
        # 小于这个体积的多半是防盗链的 HTML 错误页，当图片返回会让面板显示裂图
        return payload if len(payload) >= MIN_IMAGE_BYTES else None

    # ---- 搜索 ----
    async def search(
        self, query: SearchQuery, limit: int = 5, page: int = 1
    ) -> SearchPage:
        """漫画区搜索；视频搜索页不返回漫画，所以固定走 `/comics/search`。

        `hanime1.me` 的漫画区已迁走，搜索只在 `hanimeone.me` 有内容；域名按配置顺序试，
        某个域名返回 0 条时继续试下一个，避免用户把 `hanime1.me` 填在前面就永远搜不到。
        搜索页只收 ``query`` 一个关键词，没有字段筛选参数；前缀条件按关键词搜索并标注。
        """
        keyword = plain_search_text(query)
        scraper = self._scraper()
        domains = self.opt_list("site_domains") or split_option_list(
            DEFAULT_SITE_DOMAINS
        )
        ordered = [domain for domain in domains if "hanimeone" in domain] + [
            domain for domain in domains if "hanimeone" not in domain
        ]
        last_error: Exception | None = None
        for domain in ordered:
            url = build_page_url(domain, comic_search_path(keyword, page=page))
            try:
                document = await asyncio.to_thread(scraper.fetch_text, url)
            except ChannelError as exc:
                last_error = exc
                logger.warning(
                    f"[{PLUGIN_NAME}] Hanime1 搜索页抓取失败，换下一个域名：{exc}"
                )
                continue
            hits = parse_comic_search(document)
            if not hits:
                continue
            items = [
                SearchItem(
                    channel=self.name,
                    comic_id=hit.comic_id,
                    title=hit.title,
                    cover_url=hit.cover_url,
                )
                for hit in hits[: max(1, limit)]
            ]
            return SearchPage(
                channel=self.name,
                keyword=query.raw,
                items=items,
                has_more=len(hits) > len(items)
                or search_has_more(document, page, len(hits)),
                note=fallback_note(query),
            )
        if last_error is not None:
            raise ChannelError(f"Hanime1 搜索失败：{last_error}") from last_error
        return SearchPage(
            channel=self.name,
            keyword=query.raw,
            note=fallback_note(query),
        )

    # ---- 排行榜 ----
    ranking_boards: ClassVar[tuple[str, ...]] = ("day", "week", "all")

    async def ranking(
        self, board: str, page: int = 1, limit: int = 10
    ) -> RankingPage:
        """漫画区热度列表：同一个搜索页，关键词留空、按站内热度档位排序。

        Hanime1 的漫画区由 nhentai 供源，热度档位只有「本日/本週/所有」三档，
        没有月榜——所以 `ranking_boards` 不声明 ``month``。
        """
        sort = _BOARD_SORT.get(board)
        if sort is None:
            raise RankingUnsupportedError(f"Hanime1 没有「{board}」这一档榜单")
        scraper = self._scraper()
        domains = self.opt_list("site_domains") or split_option_list(
            DEFAULT_SITE_DOMAINS
        )
        # 与搜索一致：漫画区只在 hanimeone 系列域名有内容，先试它们
        ordered = [domain for domain in domains if "hanimeone" in domain] + [
            domain for domain in domains if "hanimeone" not in domain
        ]
        last_error: Exception | None = None
        for domain in ordered:
            url = build_page_url(domain, comic_ranking_path(sort, page=page))
            try:
                document = await asyncio.to_thread(scraper.fetch_text, url)
            except ChannelError as exc:
                last_error = exc
                logger.warning(
                    f"[{PLUGIN_NAME}] Hanime1 榜单页抓取失败，换下一个域名：{exc}"
                )
                continue
            hits = parse_comic_search(document)
            if not hits:
                continue
            items = [
                RankingItem(
                    channel=self.name,
                    comic_id=hit.comic_id,
                    title=hit.title,
                    cover_url=hit.cover_url,
                    rank=index + 1,
                )
                for index, hit in enumerate(hits[: max(1, limit)])
            ]
            return RankingPage(
                channel=self.name,
                board=board,
                items=items,
                has_more=len(hits) > len(items)
                or search_has_more(document, page, len(hits)),
            )
        if last_error is not None:
            raise ChannelError(f"Hanime1 排行榜获取失败：{last_error}") from last_error
        return RankingPage(channel=self.name, board=board)

    def _build_downloader(self) -> ImageDownloader:
        site = self._single_option("site_domains", DEFAULT_SITE_DOMAINS)
        return ImageDownloader(
            scraper=self._scraper(),
            headers={
                # 图床校验来源与 UA：缺了 Referer 部分节点会直接回 403
                "Referer": f"{build_page_url(site, '/')}",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            proxy=self._proxy(),
            timeout=self.opt_int("request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1),
            max_concurrent=self.opt_int(
                "max_concurrent_images", DEFAULT_IMAGE_CONCURRENCY, minimum=1
            ),
            retry_times=self.opt_int(
                "retry_times", DEFAULT_IMAGE_RETRY_TIMES, minimum=0
            ),
        )

    def _proxy(self) -> str:
        """本渠道这一趟要走的代理（详见 `BaseChannel.resolve_proxy`）。

        顺序：渠道开关关掉 → 直连；插件配了全局代理 → 用它；没配 → 用 AstrBot 本体代理；
        都没有 → 直连。开关关掉时不会偷偷用环境变量。
        """
        return self.resolve_proxy()[0]

    def _single_option(self, key: str, default: str) -> str:
        """取清单里的第一个域名（图床/Referer 只需要一个）。"""
        values = self.opt_list(key) or split_option_list(default)
        if not values:
            raise ChannelError(f"渠道配置项 {key} 为空，请填写至少一个域名")
        return values[0]

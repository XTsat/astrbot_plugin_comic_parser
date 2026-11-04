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
)
from ....domain.constants import PLUGIN_NAME
from ....utils import host_matches, split_link
from ...channel import BaseChannel, ProgressCallback, split_option_list
from .constants import (
    DEFAULT_FETCH_PROXY,
    IMAGE_HOST_FALLBACKS,
    DEFAULT_IMAGE_CONCURRENCY,
    DEFAULT_IMAGE_DOMAINS,
    DEFAULT_IMAGE_RETRY_TIMES,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SITE_DOMAINS,
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
    parse_detail,
    parse_reader,
)

# 作品页 /comic/<ID>，也兼容 /comic/<ID>/<页> 与分享文本
_COMIC_IN_TEXT = re.compile(r"/comic/(\d+)", re.IGNORECASE)
# 视频页 /watch?v=<ID>：本渠道只做漫画，输入视频链接时给一句明确提示
_VIDEO_IN_TEXT = re.compile(r"/watch\?v=\d+", re.IGNORECASE)
_FIRST_DIGITS = re.compile(r"\d+")


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
    link_example: ClassVar[str] = "https://hanime1.me/comic/100445"
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

        session = DownloadSession(comic_id=comic_id, total=len(urls))
        register_session(session)
        logger.info(
            f"[{PLUGIN_NAME}] 开始下载 {self.display_name} {comic_id}："
            f"{len(urls)} 页，图床 {image_base}，网络 {self.proxy_summary()}"
        )
        try:
            stats = await self._build_downloader().download(
                urls, work_dir, session=session, on_progress=on_progress
            )
        finally:
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

        tried: set[str] = set()
        for base in candidates:
            host = urlsplit(base).netloc or base
            if host in tried:
                continue
            tried.add(host)
            if await self._image_host_alive(base, parsed):
                return base
            logger.warning(f"[{PLUGIN_NAME}] 图床 {host} 连不上，换下一个")

        logger.warning(
            f"[{PLUGIN_NAME}] 所有图床都探测失败，仍按 {candidates[0]} 继续尝试"
        )
        return candidates[0] if candidates else DEFAULT_IMAGE_DOMAINS

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
        domains = self.opt_list("site_domains") or split_option_list(DEFAULT_SITE_DOMAINS)
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

    def _scraper(self) -> PageScraper:
        if self._scraper_instance is None:
            self._scraper_instance = PageScraper(
                timeout=self.opt_int(
                    "request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1
                ),
                proxy=self._proxy(),
                fetch_proxy=self.opt_str("fetch_proxy", DEFAULT_FETCH_PROXY),
            )
        return self._scraper_instance

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

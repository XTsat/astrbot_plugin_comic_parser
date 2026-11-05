"""nhentai 渠道的 `BaseChannel` 实现：拿 ID → 取元信息 → 下载图片。

元信息优先走官方 API v2（`api.py`，纯 JSON、不触发 Cloudflare 挑战），API 不可用时才退回
抓作品页 HTML（`site.py`，需要 cloudscraper）。图片下载在 `downloader.py`，
打包与加密完全交给上层，所以这里看不到任何 zip/pdf/密码相关逻辑。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
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
from ....utils import split_link
from ...channel import BaseChannel, CancelToken, ProgressCallback, split_option_list
from ...progress import ProgressHeartbeat
from ...domain_race import ProbeFn, http_probe
from .api import GalleryNotFound, NhentaiApi
from .constants import (
    CDN_API_PATH,
    DEFAULT_IMAGE_CONCURRENCY,
    DEFAULT_IMAGE_RETRY_TIMES,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SITE_DOMAINS,
    USER_AGENT,
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
    ParsedGallery,
    build_image_urls,
    default_image_base,
    default_site_domains,
    parse_api_gallery,
    parse_html_gallery,
)

_GALLERY_IN_TEXT = re.compile(r"/g/(\d+)")
_FIRST_DIGITS = re.compile(r"\d+")


class NhentaiChannel(BaseChannel):
    """按作品 ID 下载整本 nhentai 作品。

    两级降级：官方 API → 页面抓取。API 走 JSON 接口，通常一次就过；页面抓取要过
    Cloudflare，失败率高，只在 API 打不开时兜底。
    """

    name: ClassVar[str] = "nhentai"
    command: ClassVar[str] = "nhentai"
    command_aliases: ClassVar[tuple[str, ...]] = ("nh",)
    display_name: ClassVar[str] = "nhentai"
    website: ClassVar[str] = "https://nhentai.net"
    link_template: ClassVar[str] = "https://nhentai.net/g/<ID>/"
    default_use_proxy: ClassVar[bool] = True
    requires: ClassVar[tuple[str, ...]] = ("aiohttp",)

    def __init__(self, options: Mapping[str, Any], settings: PluginSettings) -> None:
        super().__init__(options, settings)
        # fetch_meta 刚取过元信息，download 直接复用，省掉一次接口请求
        self._parsed: dict[str, ParsedGallery] = {}
        # 抓页会话带 Cloudflare cookie，兜底路径跨调用复用能少挨一次挑战
        self._scraper_instance: PageScraper | None = None

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        """内置渠道默认启用：域名用官方站，图床留空自动取，并发与重试取稳一点的默认值。"""
        return {
            "enable": True,
            "site_domains": DEFAULT_SITE_DOMAINS,
            "image_domains": "",
            "request_timeout": DEFAULT_REQUEST_TIMEOUT,
            "max_concurrent_images": DEFAULT_IMAGE_CONCURRENCY,
            "retry_times": DEFAULT_IMAGE_RETRY_TIMES,
            "use_proxy": True,
        }

    # ---- ID 解析 ----
    def match_link(self, text: str) -> str | None:
        # `/g/<id>` 是 nhentai 独有的作品路径（镜像站结构一致），不必再认域名
        _, path = split_link(text)
        matched = _GALLERY_IN_TEXT.search(path)
        return matched.group(1) if matched else None

    def normalize_id(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            raise ChannelError(f"请提供作品 ID 或链接，例如 /{self.display_command} 123456")

        matched = _GALLERY_IN_TEXT.search(text)
        if matched:
            return matched.group(1)
        matched = _FIRST_DIGITS.search(text)
        if matched:
            return matched.group(0)
        raise ChannelError("无法从输入中识别出 nhentai 作品 ID，请直接给出数字 ID")

    # ---- 元信息 ----
    async def fetch_meta(self, comic_id: str, work_dir: Path) -> ComicMeta:
        parsed = await self._load_gallery(comic_id)
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
            # 插件重载或任务重试会走到这里：不依赖前一次 fetch_meta 的缓存，重新取一次
            parsed = await self._load_gallery(comic_id)

        image_base = await self._image_base(parsed)
        site = (await self._site_domains())[0]
        urls = build_image_urls(image_base, parsed.page_paths)
        if not urls:
            raise DownloadError("没有解析到任何图片地址，站点结构可能已变化")

        session = DownloadSession(comic_id=comic_id, total=len(urls), cancel=cancel)
        register_session(session)
        logger.info(
            f"[{PLUGIN_NAME}] 开始下载 nhentai {comic_id}：{len(urls)} 页，图床 {image_base}，"
            f"网络 {self.proxy_summary()}"
        )
        heartbeat = ProgressHeartbeat(session.state, on_tick=on_progress)
        heartbeat.start()
        try:
            stats = await self._build_downloader(site).download(
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
                f"[{PLUGIN_NAME}] nhentai {comic_id} 有 {stats.failed}/{stats.total} 张图片失败，产物将缺页"
            )
        logger.info(
            f"[{PLUGIN_NAME}] 下载结束 nhentai {comic_id}：{stats.success}/{stats.total} 张 · "
            f"{format_size(session.bytes_downloaded)} · 耗时 {session.elapsed():.1f}s"
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
    def _build_meta(self, comic_id: str, parsed: ParsedGallery) -> ComicMeta:
        return ComicMeta(
            channel=self.name,
            comic_id=comic_id,
            title=parsed.title,
            author=parsed.author,
            page_count=parsed.page_count,
            tags=parsed.tags,
        )

    async def _load_gallery(self, comic_id: str) -> ParsedGallery:
        """先走官方 API；接口不可用时才退回页面抓取（要过 Cloudflare，成功率和网络有关）。"""
        domains = await self._site_domains()
        try:
            payload = await self._api(domains).fetch_gallery(comic_id)
            return parse_api_gallery(comic_id, payload)
        except GalleryNotFound:
            raise
        except ChannelError as api_error:
            logger.warning(
                f"[{PLUGIN_NAME}] nhentai 接口取元信息失败，改抓页面兜底：{api_error}"
            )
            return await self._load_gallery_from_page(comic_id, api_error, domains)

    async def _load_gallery_from_page(
        self, comic_id: str, api_error: ChannelError, domains: list[str]
    ) -> ParsedGallery:
        if not domains:
            raise ChannelError("未配置任何 nhentai 站点域名，请在渠道配置里填写")

        scraper = self._scraper()
        last_error: Exception = api_error
        for domain in domains:
            url = f"https://{domain}/g/{comic_id}/"
            try:
                page = await asyncio.to_thread(scraper.fetch_text, url)
                return parse_html_gallery(comic_id, page)
            except ChannelError as exc:
                last_error = exc
                logger.warning(f"[{PLUGIN_NAME}] 抓取 {url} 失败，换下一个域名：{exc}")
        raise ChannelError(f"nhentai 作品 {comic_id} 取元信息失败：{last_error}")

    # ---- 域名测速择优 ----
    async def _site_domains(self) -> list[str]:
        """按实测速度排好序的站点域名清单；测速不可用时就是配置顺序。"""
        domains = self.opt_list("site_domains") or default_site_domains()
        if len(domains) < 2:
            return domains
        ranked = await self.ordered_domains(
            domains, self._site_probe(), label="站点域名"
        )
        return ranked or domains

    def _site_probe(self) -> ProbeFn:
        """站点域名的探测面用 `/api/v2/cdn`：无需凭据、不触发 CF，而且本来每本都要打一次。

        不拿首页或作品页当探针——那些页面在 Cloudflare 后面，服务器直连常常直接 403，
        会把「这个域名慢」和「被拦住了」混成一件事。
        """
        return http_probe(
            lambda domain: f"https://{domain}{CDN_API_PATH}",
            proxy=self._proxy(),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    async def _image_base(self, parsed: ParsedGallery) -> str:
        """图床地址：手填优先；留空用接口给的图床清单。多候选时按实测速度挑最快的。

        站点域名只决定元信息快慢，整本耗时几乎全在图床上，所以这一层更值得测：
        nhentai 的图床是一组镜像（i / i2 / i3 …），挑错一个就是整本慢。
        """
        configured = split_option_list(self.options.get("image_domains"))
        if configured:
            candidates = configured
        else:
            candidates = await self._api(await self._site_domains()).image_servers()
        candidates = [item.rstrip("/") for item in candidates if item]
        if not candidates:
            return default_image_base()
        if len(candidates) < 2 or not parsed.page_paths:
            return candidates[0]

        page = parsed.page_paths[0]
        site = (await self._site_domains())[0]
        probe = http_probe(
            lambda base: f"{base.rstrip('/')}/{page.lstrip('/')}",
            proxy=self._proxy(),
            headers={
                # 图床校验来源与 UA：缺了 Referer 部分节点会直接回 403
                "User-Agent": USER_AGENT,
                "Referer": f"https://{site}/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        ranked = await self.ordered_domains(candidates, probe, label="图床")
        return (ranked or candidates)[0]

    def _api(self, domains: list[str] | None = None) -> NhentaiApi:
        return NhentaiApi(
            domains=domains
            or self.opt_list("site_domains")
            or default_site_domains(),
            proxy=self._proxy(),
            timeout=self.opt_int("request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1),
        )

    def _scraper(self) -> PageScraper:
        if self._scraper_instance is None:
            self._scraper_instance = PageScraper(
                timeout=self.opt_int(
                    "request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1
                ),
                proxy=self._proxy(),
            )
        return self._scraper_instance

    def _build_downloader(self, site: str) -> ImageDownloader:
        return ImageDownloader(
            scraper=self._scraper(),
            headers={
                # 图床校验来源与 UA：缺了 Referer 部分节点会直接回 403
                "Referer": f"https://{site}/",
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

"""紳士漫畫（wnacg）渠道的 `BaseChannel` 实现：拿 ID → 取元信息 → 下载图片。

站点是「相册（aid）→ 章节/图址页 → 图片直链」的结构，与 JM 相近：

- **单本相册**：详情页只有标题与标签，图址页 `/photos-gallery-aid-<aid>.html` 直接列出整本图址；
- **合辑**：详情页带章节入口，每个入口的 `data-chid` 是一个**子相册自己的 aid**，
  所以合辑要按章节逐个取图址页，图片落进 `work_dir/01`、`02`… 子目录（`core/packer.py`
  的 `collect_images` 本来就按「章节目录/页码」递归收集并做自然排序）。
  **注意**：合辑这条分支目前只有上游实现的依据，本地抓到的样本都是单本相册，尚未拿真实合辑
  跑通过——首次遇到合辑出问题时先查这里（`_load_album` 的章节分页与排序）。

打包、加密、发送都在上层，这里看不到任何 zip/pdf/密码逻辑。
"""

from __future__ import annotations

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
    SearchItem,
    SearchPage,
    SearchQuery,
)
from ....domain.constants import PLUGIN_NAME
from ....utils import split_link
from ...channel import BaseChannel, CancelToken, ProgressCallback
from ...progress import ProgressHeartbeat
from ...search_query import fallback_note, plain_search_text
from .constants import (
    DEFAULT_IMAGE_CONCURRENCY,
    DEFAULT_IMAGE_RETRY_TIMES,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SITE_DOMAINS,
    USER_AGENT,
)
from .downloader import DownloadStats, ImageDownloader
from .progress import (
    DownloadSession,
    cancel_active_downloads,
    format_size,
    register_session,
    unregister_session,
)
from .site import (
    AlbumPage,
    WnacgSite,
    album_path,
    chapter_list_path,
    first_digits,
    gallery_path,
    normalize_url_id,
    parse_album,
    parse_chapter_ids,
    parse_gallery_images,
    parse_search_results,
    search_path,
)


class WnacgChannel(BaseChannel):
    """按作品/相册 ID 下载整本紳士漫畫作品。"""

    name: ClassVar[str] = "wnacg"
    command: ClassVar[str] = "wnacg"
    command_aliases: ClassVar[tuple[str, ...]] = ("ssmh", "wn")
    display_name: ClassVar[str] = "紳士漫畫"
    website: ClassVar[str] = "https://www.wnacg.site"
    link_template: ClassVar[str] = "https://www.wnacg.site/photos-index-aid-<ID>.html"
    # 大陆线路实测可直连，默认直连；超时或被墙时由用户自己打开「使用代理」
    default_use_proxy: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ("aiohttp",)

    def __init__(self, options: Mapping[str, Any], settings: PluginSettings) -> None:
        super().__init__(options, settings)
        # fetch_meta 刚解析过的详情直接复用，download 不必再抓一次详情页
        self._albums: dict[str, AlbumPage] = {}

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        """内置渠道默认启用：域名用实测可用的两个镜像，代理默认关（大陆可直连）。"""
        return {
            "enable": True,
            "site_domains": DEFAULT_SITE_DOMAINS,
            "request_timeout": DEFAULT_REQUEST_TIMEOUT,
            "max_concurrent_images": DEFAULT_IMAGE_CONCURRENCY,
            "retry_times": DEFAULT_IMAGE_RETRY_TIMES,
            "use_proxy": False,
        }

    # ---- ID 解析 ----
    def match_link(self, text: str) -> str | None:
        # `/photos-…-aid-<id>.html` 是本站独有的相册路径形状，镜像域名换掉也不影响识别
        _, path = split_link(text)
        return normalize_url_id(path) if path else None

    def normalize_id(self, raw: str) -> str:
        """站内链接取 aid（合辑子相册链接取 sid），纯数字直接当相册 ID。"""
        text = (raw or "").strip()
        if not text:
            raise ChannelError(f"请提供作品 ID 或链接，例如 /{self.display_command} 401500")

        from_url = normalize_url_id(text)
        if from_url:
            return from_url
        digits = first_digits(text)
        if digits:
            return digits
        raise ChannelError("无法从输入中识别出紳士漫畫作品 ID，请直接给出数字 ID")

    # ---- 元信息 ----
    async def fetch_meta(self, comic_id: str, work_dir: Path) -> ComicMeta:
        async with self._site() as site:
            album = await self._load_album(site, comic_id)
        self._albums[comic_id] = album
        return self._build_meta(comic_id, album)

    # ---- 下载 ----
    async def download(
        self,
        comic_id: str,
        work_dir: Path,
        on_progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> DownloadedComic:
        album = self._albums.pop(comic_id, None)
        session = DownloadSession(comic_id=comic_id, cancel=cancel)
        register_session(session)
        totals = DownloadStats()
        galleries_parsed = 0
        heartbeat = ProgressHeartbeat(session.state, on_tick=on_progress)
        heartbeat.start()
        try:
            async with self._site() as site:
                if album is None:
                    # 插件重载或任务重试会走到这里：重新解析一次详情，不依赖上一次的缓存
                    album = await self._load_album(site, comic_id)

                # 单本相册：整本的图址都在自己的图址页；合辑：每个章节一个子相册
                targets = album.chapter_ids or [(comic_id, album.title)]
                logger.info(
                    f"[{PLUGIN_NAME}] 开始下载 wnacg {comic_id}：{len(targets)} 个相册，"
                    f"网络 {self.proxy_summary()}"
                )

                for index, (gallery_id, chapter_name) in enumerate(targets, 1):
                    if session.stopped:
                        logger.warning(f"[{PLUGIN_NAME}] wnacg {comic_id} 收到中断，提前收工")
                        break

                    document = await site.fetch_page(gallery_path(gallery_id))
                    urls = parse_gallery_images(document)
                    if not urls:
                        logger.warning(
                            f"[{PLUGIN_NAME}] wnacg 相册 {gallery_id} 没解析到图址，跳过："
                            f"{chapter_name}"
                        )
                        continue

                    galleries_parsed += 1
                    session.total += len(urls)
                    target_dir = (
                        work_dir if len(targets) == 1 else work_dir / f"{index:02d}"
                    )
                    stats = await self._build_downloader(site).download(
                        urls, target_dir, session=session, on_progress=on_progress
                    )
                    totals.total += stats.total
                    totals.success += stats.success
                    totals.failed += stats.failed
        finally:
            await heartbeat.stop()
            unregister_session(session)

        if galleries_parsed == 0:
            raise DownloadError(
                "没有解析到任何图片地址，可能是内容需要登录 wnacg 才能查看，或站点结构已变化"
            )
        if totals.success <= 0:
            raise DownloadError(
                f"图片一张都没下载成功（共 {totals.total} 张），可能是图床风控、图址签名过期"
                "或网络不通；可在渠道配置里开启代理后重试"
            )
        if totals.failed:
            logger.warning(
                f"[{PLUGIN_NAME}] wnacg {comic_id} 有 {totals.failed}/{totals.total} 张图片失败，产物将缺页"
            )
        logger.info(
            f"[{PLUGIN_NAME}] 下载结束 wnacg {comic_id}：{totals.success}/{totals.total} 张 · "
            f"{format_size(session.bytes_downloaded)} · 耗时 {session.elapsed():.1f}s"
        )
        return DownloadedComic(
            meta=self._build_meta(comic_id, album),
            source_dir=work_dir,
            image_count=totals.success,
            failed_images=totals.failed,
        )

    # ---- 搜索 ----
    async def search(
        self, query: SearchQuery, limit: int = 5, page: int = 1
    ) -> SearchPage:
        """站内搜索页；复用 WnacgSite 的镜像降级、限速与 Cloudflare 兜底。

        搜索页只收 ``q`` 一个关键词，没有字段筛选参数；前缀条件按关键词搜索并标注。
        """
        keyword = plain_search_text(query)
        site = self._site()
        async with site:
            document = await site.fetch_page(search_path(keyword, page=page))
        hits, has_more = parse_search_results(document, page=page)
        items = [
            SearchItem(
                channel=self.name,
                comic_id=hit.comic_id,
                title=hit.title,
                page_count=hit.page_count,
                cover_url=hit.cover_url,
            )
            for hit in hits[: max(1, limit)]
        ]
        return SearchPage(
            channel=self.name,
            keyword=query.raw,
            items=items,
            has_more=has_more or len(hits) > len(items),
            note=fallback_note(query),
        )

    def cancel_downloads(self) -> list[str]:
        """插件卸载时调用：让进行中的下载尽快收工并返回其状态（供上层写日志）。"""
        return cancel_active_downloads()

    # ---- 内部 ----
    def _build_meta(self, comic_id: str, album: AlbumPage) -> ComicMeta:
        return ComicMeta(
            channel=self.name,
            comic_id=comic_id,
            title=album.title,
            page_count=len(album.chapter_ids),  # 0 表示单本相册
            # 详情页标签动辄几十个，全塞进结果摘要只会喧宾夺主
            tags=album.tags[:12],
        )

    async def _load_album(self, site: WnacgSite, comic_id: str) -> AlbumPage:
        """抓详情页；合辑的章节入口可能分页，逐页补全。"""
        album = parse_album(await site.fetch_page(album_path(comic_id)))
        if album.chapter_last_page > 1:
            for page in range(2, album.chapter_last_page + 1):
                document = await site.fetch_page(chapter_list_path(comic_id, page))
                for item in parse_chapter_ids(document):
                    if item not in album.chapter_ids:
                        album.chapter_ids.append(item)
        # 站内章节按「新→旧」排列，反转一次得到阅读顺序（与打包顺序一致）
        album.chapter_ids.reverse()
        return album

    def _site(self) -> WnacgSite:
        domains = self.opt_list("site_domains")
        if not domains:
            raise ChannelError("未配置任何 wnacg 站点域名，请在渠道配置里填写")
        return WnacgSite(
            domains=domains,
            timeout=self.opt_int("request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1),
            proxy=self._proxy(),
        )

    def _build_downloader(self, site: WnacgSite) -> ImageDownloader:
        return ImageDownloader(
            site=site,
            headers={
                # 图床校验来源与 UA：缺了 Referer 部分节点会直接回 403
                "User-Agent": USER_AGENT,
                "Referer": site.referer,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            proxy=self._proxy(),
            timeout=self.opt_int("request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1),
            max_concurrent=self.opt_int(
                "max_concurrent_images", DEFAULT_IMAGE_CONCURRENCY, minimum=1
            ),
            retry_times=self.opt_int("retry_times", DEFAULT_IMAGE_RETRY_TIMES, minimum=0),
        )

    def _proxy(self) -> str:
        """本渠道这一趟要走的代理（详见 `BaseChannel.resolve_proxy`）。"""
        return self.resolve_proxy()[0]


__all__ = ["WnacgChannel"]

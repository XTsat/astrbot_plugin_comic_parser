"""JM 漫画渠道的 `BaseChannel` 实现：拿 ID → 取元信息 → 下载图片。

打包与加密完全交给上层，所以这里看不到任何 zip/pdf/密码相关逻辑。
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import time
from pathlib import Path
from typing import Any, ClassVar

from astrbot.api import logger

from ....domain import (
    ChannelError,
    ComicMeta,
    DownloadError,
    DownloadedComic,
    SearchItem,
    SearchPage,
)
from ....domain.constants import IMAGE_SUFFIXES, PLUGIN_NAME
from ....utils import format_size, split_link
from ...channel import BaseChannel, CancelToken, ProgressCallback, split_option_list
from ...progress import ProgressHeartbeat, ProgressState
from .constants import (
    DEFAULT_API_DOMAINS,
    DEFAULT_HTML_DOMAINS,
    DEFAULT_REQUEST_RETRY_TIMES,
    DEFAULT_REQUEST_TIMEOUT,
)
from .option import build_option, import_jmcomic
from .progress import (
    DownloadSession,
    cancel_active_downloads,
    progress_downloader_class,
    register_session,
    unregister_session,
)
from .speed import build_probe

_ALBUM_IN_TEXT = re.compile(r"/(?:album|photo)/(\d+)")
_FIRST_DIGITS = re.compile(r"\d+")


def _count_downloaded_images(downloader: Any, source_dir: Path) -> int:
    """已成功下载的图片张数。

    以 jmcomic 下载器自己的成功记录为准：它只随版本增删属性（2.7.10 里是
    download_success_dict），没有稳定的「计数器」字段，硬读某个名字会在升级后
    把「下载成功」误判成 0 张。记录缺失时退回数落盘文件，仍然给出可信结果。
    """
    success = getattr(downloader, "download_success_dict", None)
    if isinstance(success, dict):
        recorded = sum(
            len(images) for photos in success.values() for images in photos.values()
        )
        if recorded:
            return recorded
    if not source_dir.exists():
        return 0
    return sum(
        1 for path in source_dir.rglob("*") if path.suffix.lower() in IMAGE_SUFFIXES
    )


class JMComicChannel(BaseChannel):
    name: ClassVar[str] = "jmcomic"
    command: ClassVar[str] = "jmcomic"
    command_aliases: ClassVar[tuple[str, ...]] = ("jm",)
    display_name: ClassVar[str] = "JM 漫画"
    website: ClassVar[str] = "https://18comic.vip"
    link_template: ClassVar[str] = "https://18comic.vip/album/<ID>"
    default_use_proxy: ClassVar[bool] = False
    requires: ClassVar[tuple[str, ...]] = ("jmcomic",)

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        """内置渠道默认启用：域名交给 jmcomic 自动获取，其余取推荐值。"""
        return {
            "enable": True,
            "client_type": "api",
            "image_suffix": ".jpg",
            "retry_times": DEFAULT_REQUEST_RETRY_TIMES,
            "request_timeout": DEFAULT_REQUEST_TIMEOUT,
            "max_concurrent_photos": 3,
            "max_concurrent_images": 5,
            "use_proxy": False,
        }

    # ---- ID 解析 ----
    def match_link(self, text: str) -> str | None:
        # 域名跟着上游镜像天天换，所以只认路径特征：锁死域名反而认不出用户实际拿到的镜像站
        _, path = split_link(text)
        matched = _ALBUM_IN_TEXT.search(path)
        return matched.group(1) if matched else None

    def normalize_id(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            raise ChannelError(f"请提供漫画 ID 或链接，例如 /{self.display_command} 123456")

        matched = _ALBUM_IN_TEXT.search(text)
        if matched:
            return matched.group(1)
        matched = _FIRST_DIGITS.search(text)
        if matched:
            return matched.group(0)
        raise ChannelError("无法从输入中识别出 JM 漫画 ID，请直接给出数字 ID")

    # ---- 元信息 ----
    async def fetch_meta(self, comic_id: str, work_dir: Path) -> ComicMeta:
        jmcomic = import_jmcomic()
        if jmcomic is None:
            raise ChannelError("jmcomic 未安装，请先安装依赖后重载插件")

        domains = await self._ranked_domains(jmcomic, comic_id, work_dir)
        option = await asyncio.to_thread(
            build_option, self, jmcomic, work_dir, domains=domains
        )
        return await asyncio.to_thread(self._fetch_meta_sync, jmcomic, option, comic_id)

    def _fetch_meta_sync(self, jmcomic: Any, option: Any, comic_id: str) -> ComicMeta:
        client = option.new_jm_client()
        album = client.get_album_detail(jmcomic.JmcomicText.parse_to_jm_id(comic_id))
        return ComicMeta(
            channel=self.name,
            comic_id=str(album.id),
            title=str(getattr(album, "title", "") or ""),
            author=str(getattr(album, "author", "") or ""),
            page_count=len(album) if hasattr(album, "__len__") else 0,
        )

    # ---- 域名测速择优 ----
    async def _ranked_domains(
        self, jmcomic: Any, comic_id: str, work_dir: Path
    ) -> list[str] | None:
        """下载前给候选域名测速排序；返回 None 表示「不干预，交给 jmcomic 自己选域名」。

        手填了域名清单就测那一份；用自动域名时测 jmcomic 手上那份（可能已从上游更新），
        都拿不到才退回内置清单。测速全灭时返回 None——那种情况下更该让 jmcomic 去上游
        拉最新域名，而不是把我们这份旧清单钉死。
        """
        client_type = self.opt_str("client_type", "api")
        impl = "html" if client_type.startswith("html") else "api"
        candidates = self._candidate_domains(jmcomic, impl)
        if len(candidates) < 2:
            return None
        probe = build_probe(
            self,
            jmcomic,
            comic_id,
            work_dir,
            # 探测有自己的超时（配置项），再被单请求超时兜一道：探测线程不会因为外层
            # 取消就立刻停，给够了才不至于把「慢但能用」的域名全判死
            timeout=min(
                self.settings.probe_timeout,
                self.opt_int("request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1),
            ),
            proxy=self.resolve_proxy(default_enabled=False)[0],
        )
        return await self.ordered_domains(candidates, probe, label="站点域名")

    def _candidate_domains(self, jmcomic: Any, impl: str) -> list[str]:
        """候选域名：手填清单 → jmcomic 当前的自动域名 → 内置清单。"""
        configured = self.opt_list("html_domains" if impl == "html" else "api_domains")
        if configured:
            return configured
        config = getattr(jmcomic, "JmModuleConfig", None)
        auto: Any = None
        if config is not None:
            if impl == "html":
                auto = getattr(config, "DOMAIN_HTML_LIST", None)
            else:
                auto = getattr(config, "DOMAIN_API_UPDATED_LIST", None) or getattr(
                    config, "DOMAIN_API_LIST", None
                )
        return split_option_list(auto) or split_option_list(
            DEFAULT_HTML_DOMAINS if impl == "html" else DEFAULT_API_DOMAINS
        )

    # ---- 下载 ----
    async def download(
        self,
        comic_id: str,
        work_dir: Path,
        on_progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> DownloadedComic:
        jmcomic = import_jmcomic()
        if jmcomic is None:
            raise ChannelError("jmcomic 未安装，请先安装依赖后重载插件")

        domains = await self._ranked_domains(jmcomic, comic_id, work_dir)
        option = await asyncio.to_thread(
            build_option, self, jmcomic, work_dir, domains=domains
        )
        loop = asyncio.get_running_loop()

        def report(state: ProgressState) -> None:
            if on_progress is None:
                return
            try:
                future = asyncio.run_coroutine_threadsafe(on_progress(state), loop)
                # 结果无人等待，挂个回调吞掉异常，避免 "Future exception was never retrieved"
                future.add_done_callback(lambda f: f.cancelled() or f.exception())
            except RuntimeError:
                # 事件循环已关闭（插件重载中），丢弃这次进度即可
                pass

        session = DownloadSession(album_id=comic_id)
        if cancel is not None:
            # 任务到点暂停/放弃时靠它叫停线程池里的下载
            session.stop = cancel.event
        register_session(session)
        try:
            async with ProgressHeartbeat(session.state, on_tick=on_progress):
                return await asyncio.to_thread(
                    self._download_sync, jmcomic, option, comic_id, report, session
                )
        finally:
            unregister_session(session)

    # ---- 搜索 ----
    async def search(
        self, keyword: str, limit: int = 5, page: int = 1
    ) -> SearchPage:
        """站内搜索；强制走 API 客户端（HTML 搜索页在 Cloudflare 后面，实测 403）。

        搜索不需要先测速：jmcomic 自己会按域名清单重试，测速反而会让搜索多等一轮。
        """
        jmcomic = import_jmcomic()
        if jmcomic is None:
            raise ChannelError("jmcomic 未安装，请先安装依赖后重载插件")

        option = await asyncio.to_thread(
            build_option,
            self,
            jmcomic,
            Path(tempfile.gettempdir()),
            impl="api",
        )
        result = await asyncio.to_thread(
            self._search_sync, option, keyword, page
        )
        items: list[SearchItem] = []
        for album_id, info in getattr(result, "content", [])[: max(1, limit)]:
            if not isinstance(info, dict):
                continue
            items.append(
                SearchItem(
                    channel=self.name,
                    comic_id=str(album_id),
                    title=str(info.get("name") or ""),
                    author=_author_text(info.get("author")),
                    tags=[
                        str(tag)
                        for tag in (info.get("tags") or [])
                        if str(tag).strip()
                    ],
                )
            )
        total = _to_int(getattr(result, "total", 0))
        page_count = _to_int(getattr(result, "page_count", 1))
        return SearchPage(
            channel=self.name,
            keyword=keyword,
            items=items,
            total=total,
            has_more=page_count > page,
        )

    def _search_sync(self, option: Any, keyword: str, page: int) -> Any:
        """在线程里跑 jmcomic 的同步搜索；库异常统一翻译成用户文案。"""
        try:
            client = option.new_jm_client()
            return client.search_site(keyword, page=max(1, int(page)))
        except Exception as exc:  # noqa: BLE001 - 第三方库异常类型不稳定，统一转 ChannelError
            raise ChannelError(f"JM 搜索失败：{exc}") from exc

    def cancel_downloads(self) -> list[str]:
        """插件卸载时调用：取消本渠道正在跑的下载并返回其状态（供上层写日志）。"""
        return cancel_active_downloads()

    def _download_sync(
        self,
        jmcomic: Any,
        option: Any,
        comic_id: str,
        report: Any,
        session: DownloadSession,
    ) -> DownloadedComic:
        album_id = jmcomic.JmcomicText.parse_to_jm_id(comic_id)
        downloader = progress_downloader_class(jmcomic)(option, report, session)
        started = time.monotonic()
        photo_workers = self.opt_int("max_concurrent_photos", 3, minimum=1)
        image_workers = self.opt_int("max_concurrent_images", 5, minimum=1)
        logger.info(
            f"[{PLUGIN_NAME}] 请求下载 {comic_id}"
            f"（并发 章节×{photo_workers} / 图片×{image_workers}，"
            f"网络 {self.proxy_summary()}）"
        )

        try:
            with downloader:
                album = downloader.download_album(album_id)
        except jmcomic.DownloadCancelledException as exc:
            logger.info(f"[{PLUGIN_NAME}] 下载被取消：{session.summary()}")
            raise DownloadError("下载已被插件重载中断，请重新请求") from exc

        source_dir = Path(option.dir_rule.decide_album_root_dir(album))
        failed_images = len(getattr(downloader, "download_failed_image", []) or [])
        failed_photos = len(getattr(downloader, "download_failed_photo", []) or [])
        image_count = _count_downloaded_images(downloader, source_dir)
        elapsed = max(time.monotonic() - started, 0.001)
        rate_size = format_size(int(session.bytes_downloaded / elapsed * 60))
        logger.info(
            f"[{PLUGIN_NAME}] 下载结束 {album_id}：成功 {image_count} 张 · "
            f"{format_size(session.bytes_downloaded)}，"
            f"失败 {failed_images} 张图片 / {failed_photos} 章节，"
            f"耗时 {elapsed:.1f}s（均 {image_count / elapsed * 60:.1f} 张/分、"
            f"{rate_size}/分）"
        )

        if image_count <= 0:
            if failed_images or failed_photos:
                raise DownloadError(
                    f"图片全部下载失败（{failed_images} 张图片、{failed_photos} 个章节失败），"
                    "请稍后重试，或在渠道配置里改用代理/自定义域名"
                )
            raise DownloadError("没有下载到任何图片，可能本子 ID 不存在或已被下架")

        meta = ComicMeta(
            channel=self.name,
            comic_id=str(album.id),
            title=str(getattr(album, "title", "") or ""),
            author=str(getattr(album, "author", "") or ""),
            page_count=len(album) if hasattr(album, "__len__") else 0,
        )
        return DownloadedComic(
            meta=meta,
            source_dir=source_dir,
            image_count=image_count,
            # 章节级失败意味着该章图片一张都没拿到，同样计入「失败」让用户知道产物不完整
            failed_images=failed_images + failed_photos,
        )


def _author_text(raw: object) -> str:
    if isinstance(raw, (list, tuple)):
        return "、".join(str(item).strip() for item in raw if str(item).strip())
    return str(raw or "").strip()


def _to_int(raw: object) -> int:
    try:
        return max(0, int(str(raw)))
    except (TypeError, ValueError):
        return 0

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
    DownloadCancelled,
    DownloadError,
    DownloadedComic,
    SearchItem,
    SearchPage,
    SearchQuery,
)
from ....domain.constants import IMAGE_SUFFIXES, PLUGIN_NAME
from ....utils import format_size, split_link
from ...channel import (
    BaseChannel,
    CancelToken,
    ProgressCallback,
    is_search_proxy_override,
    split_option_list,
)
from ...progress import ProgressHeartbeat, ProgressState
from ...search_query import field_label
from .constants import (
    COVER_TIMEOUT,
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

# 已知图片文件头：图床偶尔以 200 回错误页/HTML，原样交给面板只会显示一张裂图
_IMAGE_SIGNATURES: tuple[bytes, ...] = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8")


def _looks_like_image(payload: bytes) -> bool:
    return payload.startswith(_IMAGE_SIGNATURES) or (
        payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
    )


def _cover_url(domain: str, album_id: int) -> str:
    """按图床/站点域名拼封面地址；域名可带协议也可只写主机名。"""
    host = domain.strip().rstrip("/")
    if not host:
        return ""
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    return f"{host}/media/albums/{album_id}.jpg"


def _host_of(url: str) -> str:
    """提示里只留主机名：完整地址太长，用户关心的是「哪家图床挂了」。"""
    return url.split("//", 1)[-1].split("/", 1)[0]


def _album_id(jmcomic: Any, comic_id: str) -> int | None:
    """把 ID 收敛成 jmcomic 认的相册号；认不出来返回 None，由调用方给用户文案。"""
    try:
        return int(jmcomic.JmcomicText.parse_to_jm_id(comic_id))
    except Exception:  # 解析失败抛的是 jmcomic 自家的 JmcomicException，类型不稳定，统一当「认不出」
        return None


# jmcomic 的 search_* 方法按 main_tag 区分搜索域：0 综合 / 1 作品 / 2 作者 / 3 标签 / 4 角色
_NATIVE_SEARCH_METHODS = {
    "tag": "search_tag",
    "author": "search_author",
    "actor": "search_actor",
    "work": "search_work",
}


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
    # 直连搜索经常被上游限速；提示里直接给出可操作的开关，而不是只说超时
    search_timeout_hint: ClassVar[str] = (
        "JM 直连搜索可能很慢，可在 JM 渠道配置里打开「使用代理」，"
        "或打开/调大「搜索」里的「自动代理重试」"
    )

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

    # ---- 封面 ----
    async def fetch_cover(self, comic_id: str) -> bytes | None:
        """只取相册封面图，供面板的「从渠道拉封面」用。

        JM 的封面是独立的一张图（`/media/albums/<aid>.jpg`，不加密、不参与页序），所以这条
        路径既不取相册详情、也不碰下载器：单发 GET 就够，不落盘。

        上游图床域名常年换，`JmModuleConfig.DOMAIN_IMAGE_LIST` 里实测有一半已经超时，而
        jmcomic 自带的取地址工具是**随机挑一个**——挑中就成、挑不中就报「取不到」，这也是
        以前「拉封面时好时坏」的来源。这里把所有候选域名并发试一遍，第一个拿到图就算成功；
        全失败时把每个域名的结果写进 `self.cover_error`，面板原样显示给用户。
        """
        self.cover_error = ""
        jmcomic = import_jmcomic()
        if jmcomic is None:
            self.cover_error = "jmcomic 没装上，装好依赖再重载插件"
            logger.warning(
                f"[{PLUGIN_NAME}] ⚠️ 取封面失败 {self.display_name} {comic_id}：jmcomic 未安装"
            )
            return None

        album_id = _album_id(jmcomic, comic_id)
        if album_id is None:
            self.cover_error = f"作品 ID 看着不对：{comic_id}"
            logger.warning(
                f"[{PLUGIN_NAME}] ⚠️ 取封面失败 {self.display_name} {comic_id}：ID 认不出来"
            )
            return None

        urls = self._cover_urls(jmcomic, album_id)
        payload, errors = await self._fetch_first_image(jmcomic, urls)
        if payload:
            return payload

        detail = "；".join(errors[:6]) if errors else "没有可用的图床域名"
        self.cover_error = f"{len(urls)} 个候选图床都没取到图：{detail}"
        logger.warning(
            f"[{PLUGIN_NAME}] ⚠️ 取封面失败 {self.display_name} {comic_id}：{self.cover_error}"
        )
        return None

    def _cover_urls(self, jmcomic: Any, album_id: int) -> list[str]:
        """候选封面地址：jmcomic 的图床清单在前，配置里的 API / 网页域名在后。

        实测 API 域名与网页域名也能直接回 `/media/albums/<aid>.jpg`，所以它们不只是兜底：
        图床清单整批挂掉时，这一批往往还活着。
        """
        urls: list[str] = []
        seen: set[str] = set()

        def add(url: str) -> None:
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

        text_api = getattr(jmcomic, "JmcomicText", None)
        image_domains = getattr(
            getattr(jmcomic, "JmModuleConfig", None), "DOMAIN_IMAGE_LIST", ()
        )
        for domain in image_domains or ():
            try:
                add(text_api.get_album_cover_url(album_id, image_domain=str(domain)))
            except (AttributeError, TypeError, ValueError):
                # 第三方工具签名变了就退回自己拼地址：路径本身多年没变过
                add(_cover_url(str(domain), album_id))

        impl = "html" if self.opt_str("client_type", "api").startswith("html") else "api"
        key = "html_domains" if impl == "html" else "api_domains"
        default_domains = DEFAULT_HTML_DOMAINS if impl == "html" else DEFAULT_API_DOMAINS
        for domain in self.opt_list(key) or split_option_list(default_domains):
            add(_cover_url(str(domain), album_id))
        return urls

    async def _fetch_first_image(
        self, jmcomic: Any, urls: list[str]
    ) -> tuple[bytes | None, list[str]]:
        """并发请求所有候选地址，第一个回图的胜出；返回（图片字节, 每个失败的简述）。

        封面只有几十 KB，与其按顺序一个个等超时，不如全部发出去抢第一个 200。
        """
        if not urls:
            return None, ["没有可用的图床域名"]
        try:
            import aiohttp
        except ImportError:  # 缺 aiohttp 时交给依赖检查报错，封面直接算取不到
            return None, ["缺少 aiohttp，装上依赖再试"]

        headers: dict[str, str] = dict(jmcomic.JmModuleConfig.APP_HEADERS_TEMPLATE)
        headers.update(jmcomic.JmModuleConfig.APP_HEADERS_IMAGE)
        proxy = self.resolve_proxy(default_enabled=False)[0]
        timeout = min(
            self.opt_int("request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1),
            COVER_TIMEOUT,
        )

        errors: list[str] = []
        async with aiohttp.ClientSession(headers=headers, trust_env=False) as client:
            tasks = {
                asyncio.create_task(self._get_image(client, url, proxy, timeout)): url
                for url in urls
            }
            try:
                pending = set(tasks)
                while pending:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        try:
                            payload, reason = task.result()
                        except Exception as exc:  # 兜住意外异常，一张封面不该把整页拖挂
                            payload, reason = None, type(exc).__name__
                        if payload:
                            return payload, errors
                        errors.append(f"{_host_of(tasks[task])} {reason}".strip())
            finally:
                # 已经拿到图之后剩下的请求就没意义了，不取消会一直占着连接等超时
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return None, errors

    async def _get_image(
        self, client: Any, url: str, proxy: str | None, timeout: int
    ) -> tuple[bytes | None, str]:
        """GET 一张图：返回（字节, 失败原因）；原因只写短句，留给面板拼提示用。"""
        import aiohttp

        try:
            async with client.get(
                url,
                proxy=proxy or None,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status != 200:
                    return None, f"HTTP {response.status}"
                payload = await response.read()
        except asyncio.TimeoutError:
            return None, f"{timeout} 秒没响应"
        except (OSError, aiohttp.ClientError):
            return None, "连不上"
        return (payload, "") if _looks_like_image(payload) else (None, "回来的不是图片")

    # ---- 搜索 ----
    async def search(
        self, query: SearchQuery, limit: int = 5, page: int = 1
    ) -> SearchPage:
        """站内搜索；强制走 API 客户端（HTML 搜索页在 Cloudflare 后面，实测 403）。

        JM 的 ``main_tag`` 一次只能选一个搜索域：只有一个字段、且没有附加关键词时走原生
        ``search_tag`` / ``search_author`` / ``search_actor`` / ``search_work``；多字段、
        附加关键词或跨字段排除时退回综合搜索（``-`` 排除在综合搜索里仍然生效），并在结果
        里说明。搜索不需要先测速：jmcomic 自己会按域名清单重试，测速反而会让搜索多等一轮。
        """
        jmcomic = import_jmcomic()
        if jmcomic is None:
            raise ChannelError("jmcomic 未安装，请先安装依赖后重载插件")

        keyword, method, note = self._build_search(query)
        # 开了自动代理时，直连只给一次短请求：外层超时后 to_thread 里的请求仍会跑完，
        # 把 jmcomic 默认的 30 秒 × 5 次重试带进后台会白占连接，留给代理重试即可。
        auto_proxy_ready = bool(
            self.settings.search_auto_proxy and self.settings.proxy
        )
        # 直连只给一次短请求；代理重试按「自动代理重试额外时间」给预算，不能沿用
        # 直连那个被压短的超时，否则搜索超时配得小的时候代理也来不及返回。
        if is_search_proxy_override():
            budget = max(1, int(self.settings.search_auto_proxy_timeout) - 1)
            retry_times = 0
        elif auto_proxy_ready:
            budget = max(1, int(self.settings.search_timeout) - 1)
            retry_times = 0
        else:
            budget = 0
            retry_times = None
        request_timeout = (
            min(
                self.opt_int("request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1),
                budget,
            )
            if budget
            else None
        )
        option = await asyncio.to_thread(
            build_option,
            self,
            jmcomic,
            Path(tempfile.gettempdir()),
            impl="api",
            for_search=True,
            retry_times=retry_times,
            request_timeout=request_timeout,
        )
        result = await asyncio.to_thread(
            self._search_sync, option, keyword, page, method
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
            keyword=query.raw,
            items=items,
            total=total,
            has_more=page_count > page,
            note=note,
        )

    @staticmethod
    def _build_search(query: SearchQuery) -> tuple[str, str, str]:
        """返回（搜索串，jmcomic 方法名，降级说明）。"""
        positives = list(query.includes)
        negatives = list(query.excludes)
        keyword = query.keyword

        if not positives:
            terms = [keyword] if keyword else []
            terms.extend(f"-{item.value}" for item in negatives)
            return " ".join(terms), "search_site", ""

        fields = {item.field for item in positives}
        field = next(iter(fields)) if len(fields) == 1 else ""
        native = field in _NATIVE_SEARCH_METHODS
        same_field_negatives = all(item.field == field for item in negatives)
        if native and not keyword and same_field_negatives:
            terms = [item.value for item in positives]
            terms.extend(f"-{item.value}" for item in negatives)
            return " ".join(terms), _NATIVE_SEARCH_METHODS[field], ""

        terms = [item.value for item in positives]
        if keyword:
            terms.append(keyword)
        terms.extend(f"-{item.value}" for item in negatives)
        if not field:
            note = "已按综合搜索（JM 单次只能按一个字段精确搜索）"
        elif not native:
            note = f"JM 不支持按{field_label(field)}筛选，已按综合搜索"
        elif keyword:
            note = "已按综合搜索（同时搜索关键词）"
        elif not same_field_negatives:
            note = "已按综合搜索（排除条件跨字段）"
        else:
            note = "已按综合搜索（JM 单次只能按一个字段精确搜索）"
        return " ".join(terms), "search_site", note

    def _search_sync(
        self, option: Any, keyword: str, page: int, method: str
    ) -> Any:
        """在线程里跑 jmcomic 的同步搜索；库异常统一翻译成用户文案。"""
        try:
            client = option.new_jm_client()
            search = getattr(client, method, None) or client.search_site
            return search(keyword, page=max(1, int(page)))
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
            # 不是失败而是「被打断」：抛 DownloadCancelled 才不会进重试，进度与去留由任务层说清
            logger.info(f"[{PLUGIN_NAME}] 下载被取消：{session.summary()}")
            raise DownloadCancelled("下载已被插件重载中断") from exc

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

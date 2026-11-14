"""E-Hentai 渠道的 `BaseChannel` 实现：拿 ID → 取元信息 → 逐页下载。

链路是「官 API 拿元信息 + 两层 HTML 拿图片地址」：

- `gdata`（`api.py`）一次请求拿到标题/页数/标签，匿名可用；
- 作品页 `?p=N` 列缩略图，图片页 `#img` 是 1280px 重采样图（匿名可下），
  「Download original」是原图（需要登录 Cookie）；
- 默认匿名 = 降级画质；填了 Cookie 且开启「优先原图」时，逐页先试原图、失败回退重采样。

站点按 IP 记图片配额、对翻页速率敏感，所以页面请求串行限速、图片小并发，
遇到 509/临时封禁**不重试**，直接转成 `DownloadPaused` 保留断点——重发命令从已下好的页接着下。
打包、加密、发送都在上层，这里看不到任何 zip/pdf/密码逻辑。
"""

from __future__ import annotations

import asyncio
import json
import os
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
    DownloadPaused,
    PluginSettings,
    RankingItem,
    RankingPage,
    RankingUnsupportedError,
    SearchItem,
    SearchPage,
    SearchQuery,
)
from ....domain.constants import IMAGE_SUFFIXES, PLUGIN_NAME
from ....utils import (
    find_existing_image,
    host_matches,
    split_link,
    write_image_atomically,
)
from ...channel import BaseChannel, CancelToken, ProgressCallback, split_option_list
from ...domain_race import ProbeFn, http_probe
from ...progress import ProgressHeartbeat, emit
from .api import (
    EHLoginRequired,
    EHQuotaExceeded,
    EHentaiClient,
    EHBanned,
    fetch_gdata,
    has_login_cookies,
    parse_cookie_string,
)
from .constants import (
    DEFAULT_IMAGE_CONCURRENCY,
    DEFAULT_IMAGE_RETRY_TIMES,
    DEFAULT_MAX_PAGES,
    DEFAULT_REQUEST_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SITE_DOMAINS,
    USER_AGENT,
)
from .downloader import EHentaiDownloader
from .progress import (
    DownloadSession,
    cancel_active_downloads,
    format_size,
    register_session,
    unregister_session,
)
from .site import (
    GalleryMeta,
    ehentai_popular_url,
    ehentai_search_url,
    gallery_url,
    is_banned,
    is_login_wall,
    parse_gallery_meta,
    parse_search_results,
)

# 作品页路径：`/g/<gid>/<10 位十六进制 token>/`。必须锚定整段路径，
# 否则 nhentai 的 `/g/<id>/` 形状会把 E-Hentai 链接抢走（另一处已同步收紧）
_GALLERY_PATH = re.compile(r"^/g/(\d+)/([0-9a-fA-F]{10})/?(?:[?#].*)?$")
# 用户可能贴完整链接、省略协议头的链接，或夹在分享文本里的链接
_GALLERY_IN_TEXT = re.compile(
    r"(?:https?://)?(?:www\.)?(?:e-hentai|exhentai)\.org/g/(\d+)/([0-9a-fA-F]{10})",
    re.IGNORECASE,
)
# 裸 ID 的常见写法：`gid:token`、`gid-token`、`gid token`、`gid/token`；
# 从地址栏只复制路径时末尾常带斜杠，一并认掉
_BARE_ID = re.compile(r"^(\d+)\s*[:\-/\s]\s*([0-9a-fA-F]{10})\s*/?\s*$")
_EH_HOSTS = ("e-hentai.org", "exhentai.org")
_PAGES_CACHE = ".ehentai-pages.json"


class EHentaiChannel(BaseChannel):
    """按 `gid + token` 下载整本 E-Hentai 作品。"""

    name: ClassVar[str] = "ehentai"
    command: ClassVar[str] = "ehentai"
    command_aliases: ClassVar[tuple[str, ...]] = ("eh",)
    display_name: ClassVar[str] = "E-Hentai"
    website: ClassVar[str] = "https://e-hentai.org"
    link_template: ClassVar[str] = "https://e-hentai.org/g/<gid>/<token>/"
    link_hint: ClassVar[str] = (
        "<gid> 填作品页里的数字 ID，<token> 填那串 10 位十六进制"
    )
    default_use_proxy: ClassVar[bool] = True
    requires: ClassVar[tuple[str, ...]] = ("aiohttp",)

    def __init__(self, options: Mapping[str, Any], settings: PluginSettings) -> None:
        super().__init__(options, settings)
        # fetch_meta 刚取过元信息，download 直接复用，省掉一次 gdata 请求
        self._meta: dict[str, GalleryMeta] = {}

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        """内置渠道默认启用：匿名可下 1280px；Cookie 留空，用户需要原图时自己填。"""
        return {
            "enable": True,
            "site_domains": DEFAULT_SITE_DOMAINS,
            "use_proxy": True,
            "request_timeout": DEFAULT_REQUEST_TIMEOUT,
            "request_interval": DEFAULT_REQUEST_INTERVAL,
            "max_concurrent_images": DEFAULT_IMAGE_CONCURRENCY,
            "retry_times": DEFAULT_IMAGE_RETRY_TIMES,
            "max_pages": DEFAULT_MAX_PAGES,
            "download_original": True,
            "cookie": "",
        }

    # ---- ID 解析 ----

    def match_link(self, text: str) -> str | None:
        """只认 e-hentai/exhentai 域名下的 `/g/<gid>/<10 位 token>/`。

        必须同时校验域名与完整路径：别的站点也有 `/g/<id>/` 形状（nhentai），
        只看路径会把链接路由错。
        """
        host, path = split_link(text)
        if not host_matches(host, _EH_HOSTS):
            return None
        matched = _GALLERY_PATH.match(path)
        return _canonical(matched.group(1), matched.group(2)) if matched else None

    def normalize_id(self, raw: str) -> str:
        """把链接、`gid:token`、`gid-token`、`gid token` 收敛成 `gid-token`。

        规范形用 `-` 而不是 `:`：任务层会把它拼进断点目录名 `ehentai-<id>`，
        `:` 在 Windows 路径里非法。裸 gid 一律拒绝——`gdata` 强制要 token，
        只给数字没法定位作品（实测 `/g/<gid>/` 直接 404）。
        """
        text = (raw or "").strip()
        if not text:
            raise ChannelError(
                f"请提供作品链接或 gid:token，例如 /{self.display_command} "
                "https://e-hentai.org/g/1234567/0123456789/"
            )

        matched = _GALLERY_IN_TEXT.search(text)
        if matched:
            return _canonical(matched.group(1), matched.group(2))
        matched = _BARE_ID.match(text)
        if matched:
            return _canonical(matched.group(1), matched.group(2))
        if text.isdigit():
            raise ChannelError(
                "E-Hentai 作品 ID 由数字 gid 和 10 位十六进制 token 两段组成，只给数字下不了；"
                "请贴完整作品页链接（/g/<gid>/<token>/），或写成 1234567:0123456789"
            )
        raise ChannelError(
            "无法识别 E-Hentai 作品 ID，请贴作品页链接"
            "（e-hentai.org/g/<gid>/<token>/）或写成 gid:token"
        )

    @classmethod
    def short_id(cls, comic_id: str) -> str:
        """文件名简写只留 gid：token 是页面访问凭证，不帮助人认出作品，还多占 11 个字符。"""
        gid, sep, _token = (comic_id or "").partition("-")
        return gid if sep and gid.isdigit() else comic_id

    # ---- 元信息 ----

    async def fetch_meta(self, comic_id: str, work_dir: Path) -> ComicMeta:
        gid, token = _split_comic_id(comic_id)
        proxy, source = self.resolve_proxy()
        async with self._client(proxy, source) as client:
            item = await fetch_gdata(client, gid, token)
        meta = parse_gallery_meta(item)
        meta.gid, meta.token = gid, token
        self._meta[comic_id] = meta
        logger.info(
            f"[{PLUGIN_NAME}] E-Hentai {comic_id} 元信息：{meta.filecount} 页 · "
            f"{format_size(meta.filesize)} · 登录：{'是' if client.logged_in else '否（匿名 1280px）'}"
        )
        return self._build_meta(comic_id, meta)

    # ---- 下载 ----

    async def download(
        self,
        comic_id: str,
        work_dir: Path,
        on_progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> DownloadedComic:
        gid, token = _split_comic_id(comic_id)
        meta = self._meta.pop(comic_id, None)
        proxy, source = self.resolve_proxy()

        async with self._client(proxy, source) as client:
            if meta is None:
                # 插件重载或任务重试会走到这里：不依赖前一次 fetch_meta 的缓存
                item = await fetch_gdata(client, gid, token)
                meta = parse_gallery_meta(item)
                meta.gid, meta.token = gid, token
            return await self._download(
                client, comic_id, gid, token, meta, work_dir, on_progress, cancel
            )

    # ---- 封面 ----
    async def fetch_cover(self, comic_id: str) -> bytes | None:
        """只取作品第 1 页的图，供面板的「从渠道拉封面」用。

        只开作品页第 0 页就够（不走 `_load_pages`：那条路会翻完整本并写断点缓存），再按下载时
        同一套「原图优先、失败回退 1280px」的规则取这一张的字节。
        """
        gid, token = _split_comic_id(comic_id)
        proxy, source = self.resolve_proxy()
        try:
            async with self._client(proxy, source) as client:
                downloader = EHentaiDownloader(
                    client,
                    interval=self._request_interval(),
                    retry_times=self.opt_int(
                        "retry_times", DEFAULT_IMAGE_RETRY_TIMES, minimum=0
                    ),
                    concurrency=1,
                    include_original=self.opt_bool("download_original", True)
                    and client.logged_in,
                )
                host, page_url = await self._first_page_url(downloader, gid, token)
                links = await downloader.image_links(page_url, host)
                payload: bytes | None = None
                if downloader.include_original and links.original:
                    try:
                        payload, _suffix = await downloader.image(links.original)
                    except EHLoginRequired as exc:
                        # Cookie 没生效时原图会撞登录墙，与下载链路一样降级到重采样图
                        logger.info(
                            f"[{PLUGIN_NAME}] E-Hentai {comic_id} 封面原图需要登录，"
                            f"改取 1280px 重采样：{exc}"
                        )
                if payload is None:
                    payload, _suffix = await downloader.image(links.resample)
        except ChannelError as exc:
            logger.warning(
                f"[{PLUGIN_NAME}] ⚠️ 取封面失败 E-Hentai {comic_id}：{exc}"
            )
            return None
        return payload

    async def _first_page_url(
        self, downloader: EHentaiDownloader, gid: str, token: str
    ) -> tuple[str, str]:
        """按站点域名清单打开作品页第 0 页，返回（站点域名，第 1 张图的图片页地址）。"""
        hosts = await self._site_domains()
        last_error: ChannelError | None = None
        for host in hosts:
            try:
                pages = await downloader.gallery_page(
                    gallery_url(host, gid, token, 0), host
                )
            except (EHQuotaExceeded, EHBanned):
                raise
            except ChannelError as exc:
                last_error = exc
                logger.warning(
                    f"[{PLUGIN_NAME}] E-Hentai 封面取作品页失败，换下一个域名：{exc}"
                )
                continue
            if pages:
                return host, pages[0]
        logger.warning(
            f"[{PLUGIN_NAME}] E-Hentai 作品页没解析到缩略图：{last_error}"
        )
        raise ChannelError(
            "作品页里没有图片：可能已删除、需要登录，或站点改版了"
        )

    # ---- 排行榜 ----
    # 站点只有一个「当前热门」页，没有日/周/月/总的分档；四档都接受，但非日榜会注明
    # 时间窗不生效——藏起来让用户以为「E-Hentai 没榜单」比说实话更糟
    ranking_boards: ClassVar[tuple[str, ...]] = ("day", "week", "month", "all")

    async def ranking(
        self, board: str, page: int = 1, limit: int = 10, offset: int = 0
    ) -> RankingPage:
        """站点热门页（`/popular`）：按近期收藏热度排，匿名可用。

        与搜索共用域名降级、代理与风控判定；页面结构同样是作品行，所以复用搜索解析器。
        站点不提供时间窗，除日榜外都注明「时间窗不生效」，并把「第几页」交给站点默认。
        """
        if board not in self.ranking_boards:
            raise RankingUnsupportedError(f"E-Hentai 没有「{board}」这一档榜单")
        note = "" if board == "day" else "E-Hentai 只提供「当前热门」一档，时间窗不生效"
        hosts = self._search_hosts()
        proxy, source = self.resolve_proxy()
        last_error: Exception | None = None
        async with self._client(proxy, source) as client:
            for host in hosts:
                try:
                    document, final_url = await client.get_text(
                        ehentai_popular_url(host)
                    )
                except (EHQuotaExceeded, EHLoginRequired, EHBanned) as exc:
                    logger.warning(f"[{PLUGIN_NAME}] E-Hentai 排行榜不可用：{exc}")
                    raise ChannelError(
                        "排行榜这会儿看不了：站点要求登录或配额用尽，过几小时再试"
                    ) from exc
                except ChannelError as exc:
                    last_error = exc
                    logger.warning(
                        f"[{PLUGIN_NAME}] E-Hentai 热门页抓取失败，换下一个域名：{exc}"
                    )
                    continue
                if is_banned(document):
                    raise ChannelError("E-Hentai 热门页被临时封禁，请过几小时再试")
                if is_login_wall(document) or "sadpanda" in final_url.lower():
                    last_error = ChannelError("热门页需要登录")
                    continue
                hits, has_more = parse_search_results(document, page=0)
                if hits:
                    candidates = [
                        RankingItem(
                            channel=self.name,
                            comic_id=_canonical(hit.gid, hit.token),
                            title=hit.title,
                            page_count=hit.page_count,
                            rank=index + 1,
                        )
                        for index, hit in enumerate(hits)
                    ]
                    begin = max(0, int(offset))
                    window = candidates[begin : begin + max(1, int(limit))]
                    exhausted = begin + len(window) >= len(candidates)
                    return RankingPage(
                        channel=self.name,
                        board=board,
                        items=window,
                        has_more=(not exhausted) or has_more,
                        note=note,
                        exhausted=exhausted,
                    )
                last_error = ChannelError("热门页没有解析到结果")

        if last_error is not None:
            logger.warning(f"[{PLUGIN_NAME}] E-Hentai 排行榜获取失败：{last_error}")
            raise ChannelError(
                "排行榜这会儿打不开：换个域名或过几小时再试"
            ) from last_error
        return RankingPage(channel=self.name, board=board, note=note)

    # ---- 搜索 ----
    async def search(
        self, query: SearchQuery, limit: int = 5, page: int = 1
    ) -> SearchPage:
        """站内搜索页；匿名可用，exhentai 需要登录 Cookie。

        E-Hentai 的搜索语法本身支持命名空间与 ``-`` 排除，所以前缀直接翻译成原生查询：
        ``author:`` → ``artist:``、``actor:`` → ``character:``，``tag:``/``work:`` 用裸词
        （站点没有只搜标签或标题的语法，只能近似）。搜索页是 HTML（站点没有搜索 API），
        解析失败按「没搜到」返回空列表，被风控/封禁时才报错——站点改版不该让整条命令炸掉。
        """
        keyword = self._build_search_text(query)
        hosts = self._search_hosts()
        last_error: Exception | None = None
        upstream_page = max(1, int(page)) - 1  # 站点分页是 0 基
        proxy, source = self.resolve_proxy()
        async with self._client(proxy, source) as client:
            for host in hosts:
                url = ehentai_search_url(host, keyword, page=upstream_page)
                try:
                    document, final_url = await client.get_text(url)
                except (EHQuotaExceeded, EHLoginRequired, EHBanned) as exc:
                    logger.warning(f"[{PLUGIN_NAME}] E-Hentai 搜索不可用：{exc}")
                    raise ChannelError(
                        "搜索这会儿用不了：站点要求登录或配额用尽，过几小时再试"
                    ) from exc
                except ChannelError as exc:
                    last_error = exc
                    logger.warning(
                        f"[{PLUGIN_NAME}] E-Hentai 搜索页抓取失败，换下一个域名：{exc}"
                    )
                    continue
                if is_banned(document):
                    raise ChannelError("E-Hentai 搜索被临时封禁，请过几小时再试")
                if is_login_wall(document) or "sadpanda" in final_url.lower():
                    last_error = ChannelError("搜索页需要登录")
                    continue

                hits, has_more = parse_search_results(document, page=upstream_page)
                if hits:
                    items = [
                        SearchItem(
                            channel=self.name,
                            comic_id=_canonical(hit.gid, hit.token),
                            title=hit.title,
                            page_count=hit.page_count,
                        )
                        for hit in hits[: max(1, limit)]
                    ]
                    return SearchPage(
                        channel=self.name,
                        keyword=query.raw,
                        items=items,
                        has_more=has_more or len(hits) > len(items),
                    )
                if "No hits found" in document:
                    return SearchPage(channel=self.name, keyword=query.raw)
                last_error = ChannelError("搜索页没有解析到结果")

        if last_error is not None:
            logger.warning(f"[{PLUGIN_NAME}] E-Hentai 搜索失败：{last_error}")
            raise ChannelError(
                "搜索页这会儿打不开：换个域名或过几小时再试"
            ) from last_error
        return SearchPage(channel=self.name, keyword=query.raw)

    @staticmethod
    def _build_search_text(query: SearchQuery) -> str:
        """把结构化条件翻译成 E-Hentai 的搜索串。"""
        terms = [
            EHentaiChannel._search_term(item.field, item.value)
            for item in query.includes
        ]
        terms.extend(
            "-" + EHentaiChannel._search_term(item.field, item.value)
            for item in query.excludes
        )
        if query.keyword:
            terms.append(query.keyword)
        return " ".join(term for term in terms if term)

    @staticmethod
    def _search_term(field: str, value: str) -> str:
        if field == "language":
            return f"language:{_language_value(value)}"
        quoted = _quote_search_value(value)
        if field in {"tag", "work"}:
            # EH 没有只搜标签/标题的语法：裸词会同时命中标签与标题，只能近似
            return quoted
        if field == "author":
            return f"artist:{quoted}"
        if field == "actor":
            return f"character:{quoted}"
        return f"{field}:{quoted}"

    def _search_hosts(self) -> list[str]:
        """搜索只认配置里的站点域名；没登录时跳过 exhentai（会返回 Sad Panda）。"""
        raw = self.opt_list("site_domains") or split_option_list(DEFAULT_SITE_DOMAINS)
        hosts: list[str] = []
        for item in raw:
            host = re.sub(r"^https?://", "", item.strip().lower())
            host = host.split("/", 1)[0].split(":", 1)[0].rstrip(".")
            if host and host not in hosts:
                hosts.append(host)
        if not self._has_credentials():
            hosts = [host for host in hosts if host != "exhentai.org"]
        if not hosts:
            raise ChannelError(
                "E-Hentai 没有可用的站点域名：exhentai 需要登录 Cookie，"
                "请在渠道配置里填 cookie，或把 e-hentai.org 加回站点域名"
            )
        return hosts

    async def _download(
        self,
        client: EHentaiClient,
        comic_id: str,
        gid: str,
        token: str,
        meta: GalleryMeta,
        work_dir: Path,
        on_progress: ProgressCallback | None,
        cancel: CancelToken | None,
    ) -> DownloadedComic:
        max_pages = self._max_pages()
        try:
            host, pages = await self._load_pages(
                client, gid, token, meta.filecount, work_dir, max_pages
            )
        except (EHQuotaExceeded, EHBanned) as exc:
            raise self._paused(work_dir, str(exc)) from exc
        except EHLoginRequired as exc:
            raise self._need_login(exc) from exc

        if max_pages > 0 and len(pages) > max_pages:
            logger.info(
                f"[{PLUGIN_NAME}] E-Hentai {comic_id} 按「最多下载页数」截取前 "
                f"{max_pages}/{meta.filecount} 页"
            )
            pages = pages[:max_pages]
        if not pages:
            raise DownloadError("没有解析到任何图片页，站点结构可能已变化")

        wants_original = self.opt_bool("download_original", True)
        if wants_original and not client.logged_in:
            logger.info(
                f"[{PLUGIN_NAME}] E-Hentai 未配置登录 Cookie，本次自动降级为 1280px 重采样图"
            )
        elif not wants_original and client.logged_in:
            logger.info(
                f"[{PLUGIN_NAME}] E-Hentai 已配置 Cookie，但关闭了「优先原图」，本次下 1280px 重采样图"
            )

        downloader = EHentaiDownloader(
            client,
            interval=self._request_interval(),
            retry_times=self.opt_int(
                "retry_times", DEFAULT_IMAGE_RETRY_TIMES, minimum=0
            ),
            concurrency=self.opt_int(
                "max_concurrent_images", DEFAULT_IMAGE_CONCURRENCY, minimum=1
            ),
            include_original=wants_original and client.logged_in,
        )

        session = DownloadSession(comic_id=comic_id, total=len(pages), cancel=cancel)
        register_session(session)
        logger.info(
            f"[{PLUGIN_NAME}] 开始下载 E-Hentai {comic_id}：{len(pages)} 页，站点 {host}，"
            f"画质 {'原图优先（失败回退 1280px）' if downloader.include_original else '1280px 重采样'}，"
            f"网络 {self.proxy_summary()}"
        )
        # 文件名宽度按整本页数定，不按本次截断后的页数：否则先整本下一半、再改小
        # max_pages 续跑时，`0001.jpg` 与 `001.jpg` 对不上，同一页会被重下
        width = max(3, len(str(meta.filecount or len(pages))))
        heartbeat = ProgressHeartbeat(session.state, on_tick=on_progress)
        heartbeat.start()
        try:
            fatal = await self._run_workers(
                downloader, host, pages, work_dir, session, on_progress, width
            )
        finally:
            await heartbeat.stop()
            unregister_session(session)

        if fatal is not None:
            if isinstance(fatal, EHQuotaExceeded):
                raise self._paused(
                    work_dir,
                    "E-Hentai 图片配额用尽（HTTP 509）",
                    saved=session.done,
                    total=session.total,
                ) from fatal
            if isinstance(fatal, EHBanned):
                raise self._paused(
                    work_dir,
                    "站点提示当前 IP 被临时封禁（翻页/下载太快）",
                    saved=session.done,
                    total=session.total,
                ) from fatal
            if isinstance(fatal, EHLoginRequired):
                raise self._need_login(fatal) from fatal
            logger.warning(f"[{PLUGIN_NAME}] E-Hentai 下载失败：{fatal}")
            raise DownloadError(
                "E-Hentai 下载失败：多半是站点风控或网络不通，开代理后重发一次命令"
            ) from fatal

        if session.done <= 0:
            raise DownloadError(
                "图片一张都没下载成功，可能是站点风控拦截或网络不通；"
                "请检查代理与渠道配置后重试"
            )
        if session.failed:
            logger.warning(
                f"[{PLUGIN_NAME}] E-Hentai {comic_id} 有 {session.failed}/{session.total} 页失败，产物将缺页"
            )
        # 整本已下完，页地址缓存不再有用；「仅本地保存」模式下别把它留在用户目录里
        try:
            (work_dir / _PAGES_CACHE).unlink(missing_ok=True)
        except OSError:
            pass
        # 目录里的实际张数才是要打包的张数：续跑时目录里可能还留着上一轮下好的、
        # 已经超出本次 max_pages 截取范围的页，按 session.done 报会少算
        image_count = _count_images(work_dir)
        extra = (
            f"，目录共 {image_count} 张" if image_count != session.done else ""
        )
        logger.info(
            f"[{PLUGIN_NAME}] 下载结束 E-Hentai {comic_id}：{session.done}/{session.total} 页"
            f"（跳过已下 {session.skipped}，降级 {session.degraded}{extra}）· "
            f"{format_size(session.bytes_downloaded)} · 耗时 {session.elapsed():.1f}s"
        )
        return DownloadedComic(
            meta=self._build_meta(comic_id, meta),
            source_dir=work_dir,
            image_count=image_count,
            failed_images=session.failed,
        )

    async def _run_workers(
        self,
        downloader: EHentaiDownloader,
        host: str,
        pages: list[str],
        work_dir: Path,
        session: DownloadSession,
        on_progress: ProgressCallback | None,
        width: int,
    ) -> BaseException | None:
        """并发跑完所有页；返回第一个致命异常（配额/封禁/登录），其余失败只计数。"""
        abort = asyncio.Event()
        # 图片页本身也是「页面」：限 2 路并发，真正的节流在 downloader 的闸门上
        page_semaphore = asyncio.Semaphore(2)
        degraded_notice = {"logged": False, "missing": False}

        async def one(index: int, page_url: str) -> None:
            if session.stopped or abort.is_set():
                return
            stem = work_dir / f"{index:0{width}d}"
            existing = find_existing_image(stem)
            if existing is not None:
                session.skipped += 1
                session.done += 1
                try:
                    session.bytes_downloaded += existing.stat().st_size
                except OSError:
                    pass
                return

            try:
                async with page_semaphore:
                    links = await downloader.image_links(page_url, host)

                payload: bytes | None = None
                suffix = ""
                if downloader.include_original and not links.original:
                    # Cookie 无效或该图没有原图时，图片页不会给 /fullimg/ 链接：
                    # 仍然按 1280px 下，但让管理员在日志里看到「原图没生效」
                    if not degraded_notice["missing"]:
                        logger.info(
                            f"[{PLUGIN_NAME}] E-Hentai 图片页没有原图链接（Cookie 可能未生效），"
                            "本次取 1280px 重采样"
                        )
                        degraded_notice["missing"] = True
                if downloader.include_original and links.original:
                    try:
                        payload, suffix = await downloader.image(links.original)
                    except (EHQuotaExceeded, EHBanned):
                        raise
                    except EHLoginRequired as exc:
                        # 原图需要登录但 Cookie 没配/失效：整本继续走重采样，不中断
                        if not degraded_notice["logged"]:
                            logger.info(
                                f"[{PLUGIN_NAME}] E-Hentai 原图需要登录，后续页自动回退 1280px：{exc}"
                            )
                            degraded_notice["logged"] = True
                    except ChannelError as exc:
                        if not degraded_notice["logged"]:
                            logger.warning(
                                f"[{PLUGIN_NAME}] E-Hentai 原图取不到，后续页自动回退 1280px：{exc}"
                            )
                            degraded_notice["logged"] = True

                degraded = payload is None and downloader.include_original
                if payload is None:
                    payload, suffix = await downloader.image(links.resample)

                target = stem.with_suffix(suffix)
                # 写盘是同步 IO，丢线程池里，别阻塞事件循环（几百 KB 一张）
                await asyncio.to_thread(write_image_atomically, target, payload)
                session.done += 1
                session.bytes_downloaded += len(payload)
                if degraded:
                    session.degraded += 1
                await emit(on_progress, session.state())
            except (EHQuotaExceeded, EHBanned, EHLoginRequired):
                abort.set()
                raise
            except ChannelError as exc:
                session.failed += 1
                logger.warning(
                    f"[{PLUGIN_NAME}] E-Hentai {session.comic_id} 第 {index} 页失败：{exc}"
                )

        tasks = [
            asyncio.create_task(one(index, url))
            for index, url in enumerate(pages, 1)
        ]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        for result in results:
            if isinstance(result, (EHQuotaExceeded, EHBanned, EHLoginRequired)):
                return result
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                return result
        return None

    # ---- 作品页枚举与断点缓存 ----

    async def _load_pages(
        self,
        client: EHentaiClient,
        gid: str,
        token: str,
        filecount: int,
        work_dir: Path,
        max_pages: int = 0,
    ) -> tuple[str, list[str]]:
        """确定可用站点域名并拿到整本图片页地址；完整翻页结果会缓存到断点目录。

        `max_pages > 0` 时只翻到够用的那一页，且不写缓存：否则「这次只下 2 页」
        留下的半截清单会被下一次「下整本」当成完整清单复用。
        """
        hosts = await self._site_domains()
        wanted = min(filecount, max_pages) if max_pages > 0 else filecount
        cached = self._read_pages_cache(work_dir, gid, token, filecount)
        if cached is not None and cached[0] in hosts and len(cached[1]) >= wanted:
            logger.info(
                f"[{PLUGIN_NAME}] E-Hentai {gid} 复用断点里的 {len(cached[1])} 页地址"
                f"（站点 {cached[0]}）"
            )
            return cached

        downloader = EHentaiDownloader(
            client,
            interval=self._request_interval(),
            retry_times=self.opt_int(
                "retry_times", DEFAULT_IMAGE_RETRY_TIMES, minimum=0
            ),
            concurrency=1,
            include_original=False,
        )
        last_error: ChannelError | None = None
        for host in hosts:
            try:
                pages = await self._enumerate_pages(
                    downloader, host, gid, token, filecount, max_pages
                )
            except (EHQuotaExceeded, EHBanned):
                raise
            except ChannelError as exc:
                last_error = exc
                logger.warning(
                    f"[{PLUGIN_NAME}] E-Hentai 站点 {host} 打不开作品页，换下一个：{exc}"
                )
                continue
            if max_pages <= 0 or len(pages) >= filecount:
                self._write_pages_cache(
                    work_dir, gid, token, host, filecount, pages
                )
            return host, pages
        logger.warning(f"[{PLUGIN_NAME}] E-Hentai 作品页打不开：{last_error}")
        raise ChannelError(
            "作品页打不开：多半是出口被风控或需要登录，换代理线路或过几小时再试"
        )

    async def _enumerate_pages(
        self,
        downloader: EHentaiDownloader,
        host: str,
        gid: str,
        token: str,
        filecount: int,
        max_pages: int = 0,
    ) -> list[str]:
        first = await downloader.gallery_page(gallery_url(host, gid, token, 0), host)
        if not first:
            raise ChannelError("作品页里没有解析到缩略图，可能已删除或需要登录")
        # 缩略图每页数量可由用户设置（20/40/100），以实际解析到的为准
        per_page = len(first)
        wanted = min(filecount, max_pages) if max_pages > 0 else filecount
        total_pages = max(1, (wanted + per_page - 1) // per_page)
        pages = list(first)
        for page in range(1, total_pages):
            if len(pages) >= wanted:
                break
            urls = await downloader.gallery_page(
                gallery_url(host, gid, token, page), host
            )
            if not urls:
                logger.warning(
                    f"[{PLUGIN_NAME}] E-Hentai 作品页第 {page} 页没有缩略图，跳过"
                )
                continue
            pages.extend(urls)
        # 去重但保序：同页重复链接会让某一页被下两次、页码错位
        unique = list(dict.fromkeys(pages))
        if len(unique) < wanted:
            logger.warning(
                f"[{PLUGIN_NAME}] E-Hentai 只解析到 {len(unique)}/{filecount} 页，"
                "站点结构可能已变化或部分页不可见"
            )
        return unique

    def _read_pages_cache(
        self, work_dir: Path, gid: str, token: str, filecount: int
    ) -> tuple[str, list[str]] | None:
        path = work_dir / _PAGES_CACHE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if str(data.get("gid")) != gid or str(data.get("token")) != token:
            return None
        if _as_int(data.get("filecount")) != filecount:
            return None
        host = str(data.get("host") or "")
        raw_pages = data.get("pages")
        pages = [
            str(item)
            for item in raw_pages
            if isinstance(item, str) and item.startswith("http")
        ] if isinstance(raw_pages, list) else []
        if not host or not pages:
            return None
        return host, pages

    def _write_pages_cache(
        self,
        work_dir: Path,
        gid: str,
        token: str,
        host: str,
        filecount: int,
        pages: list[str],
    ) -> None:
        path = work_dir / _PAGES_CACHE
        tmp = path.with_name(f"{path.name}.part")
        payload = {
            "gid": gid,
            "token": token,
            "host": host,
            "filecount": filecount,
            "pages": pages,
        }
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, path)
        except OSError as exc:
            # 缓存写不进去只影响续跑效率，不该挡住本次下载
            logger.warning(f"[{PLUGIN_NAME}] E-Hentai 页地址缓存写入失败（忽略）：{exc}")
        finally:
            tmp.unlink(missing_ok=True)

    # ---- 内部 ----

    def _client(self, proxy: str, source: str = "") -> EHentaiClient:
        return EHentaiClient(
            proxy=proxy,
            proxy_source=source,
            timeout=self.opt_int(
                "request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1
            ),
            cookie=self.opt_str("cookie"),
        )

    async def _site_domains(self) -> list[str]:
        """配置里的站点域名，按实测速度排序；没配 Cookie 时跳过 exhentai。

        两个站点内容一致，正常情况 e-hentai 排第一；填了 Cookie 时 exhentai 也可用，
        这时值得测一下——境外线路下两个域名的速度可能差出一倍。
        """
        domains = self.opt_list("site_domains") or split_option_list(
            DEFAULT_SITE_DOMAINS
        )
        hosts: list[str] = []
        for raw in domains:
            host = raw.strip().lower()
            host = re.sub(r"^https?://", "", host).split("/", 1)[0]
            host = host.split(":", 1)[0].rstrip(".")
            if host and host not in hosts:
                hosts.append(host)
        if not self._has_credentials():
            hosts = [host for host in hosts if host != "exhentai.org"]
        if not hosts:
            raise ChannelError(
                "E-Hentai 没有可用的站点域名：exhentai 需要登录 Cookie，"
                "请在渠道配置里填 cookie，或把 e-hentai.org 加回站点域名"
            )
        if len(hosts) < 2:
            return hosts
        ranked = await self.ordered_domains(
            hosts, self._site_probe(), label="站点域名"
        )
        if not ranked:
            return hosts
        # 测速只负责「谁快」：探测失败的域名也得留在后面，等前面真的打不开时兜底
        return ranked + [host for host in hosts if host not in ranked]

    def _site_probe(self) -> ProbeFn:
        """探测面用首页：无需凭据，且和真正要打开的作品页走同一条线路。"""
        return http_probe(
            lambda host: f"https://{host}/",
            proxy=self._proxy(),
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            # exhentai 的 Cookie 只填了一半时首页是 Sad Panda（HTTP 200），
            # 不标记出来会被测速排到第一，下载再回落一次
            reject_markers=(b"Sad Panda",),
        )

    def _proxy(self) -> str:
        proxy, _source = self.resolve_proxy()
        return proxy

    def _has_credentials(self) -> bool:
        return has_login_cookies(parse_cookie_string(self.opt_str("cookie")))

    def _request_interval(self) -> float:
        raw = self.options.get("request_interval")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = DEFAULT_REQUEST_INTERVAL
        return min(max(value, 0.0), 10.0)

    def _max_pages(self) -> int:
        return self.opt_int("max_pages", DEFAULT_MAX_PAGES, minimum=0)

    def _build_meta(self, comic_id: str, meta: GalleryMeta) -> ComicMeta:
        return ComicMeta(
            channel=self.name,
            comic_id=comic_id,
            title=meta.display_title,
            author=meta.author,
            page_count=meta.filecount,
            tags=meta.tags,
        )

    def _paused(
        self,
        work_dir: Path,
        reason: str,
        *,
        saved: int | None = None,
        total: int = 0,
    ) -> DownloadPaused:
        """配额/封禁统一转成「暂停」：保留断点，重发命令接着下，不当作失败重试。"""
        if saved is None:
            saved = _count_images(work_dir)
        return DownloadPaused(
            f"⏸ {reason}。已保存 {saved}{f'/{total}' if total else ''} 页，断点会保留；"
            "过几小时或换网络后发 /comic retry 会接着下",
            saved=saved,
            total=total,
        )

    def _need_login(self, exc: Exception) -> DownloadError:
        return DownloadError(
            f"E-Hentai 需要登录才能继续：{exc}。"
            "匿名只能下 1280px 重采样图；要原图或 exhentai 内容，"
            "请在渠道配置的「登录 Cookie」里填 ipb_member_id、ipb_pass_hash（exhentai 还需 igneous）"
        )

    def cancel_downloads(self) -> list[str]:
        """插件卸载时调用：让进行中的下载尽快收工并返回其状态（供上层写日志）。"""
        return cancel_active_downloads()


# 语言字段的中文写法映射到 E-Hentai 的 language 标签值；其它值按小写透传
_LANGUAGE_ALIASES = {
    "中文": "chinese",
    "汉化": "chinese",
    "漢化": "chinese",
    "简中": "chinese",
    "繁中": "chinese",
    "日文": "japanese",
    "日语": "japanese",
    "日語": "japanese",
    "英文": "english",
    "英语": "english",
    "英語": "english",
    "韩文": "korean",
    "韩语": "korean",
    "韓文": "korean",
    "韓語": "korean",
}


def _language_value(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return text
    return _LANGUAGE_ALIASES.get(text) or _LANGUAGE_ALIASES.get(text.lower()) or text.lower()


def _quote_search_value(value: str) -> str:
    """含空格的值用引号包起来，避免 E-Hentai 把它拆成多个 AND 词。"""
    text = (value or "").strip()
    if not text:
        return text
    if any(char.isspace() for char in text) and '"' not in text:
        return f'"{text}"'
    return text


def _split_comic_id(comic_id: str) -> tuple[str, str]:
    gid, sep, token = (comic_id or "").partition("-")
    if not sep or not gid.isdigit() or not re.fullmatch(r"[0-9a-fA-F]{10}", token):
        # 只说「格式不对」没用：把该长什么样、从哪儿抄写在同一句里
        raise ChannelError(
            "E-Hentai 要的是 <gid>-<token> 形式的 ID（如 1234567-1a2b3c4d5e）："
            "贴作品页链接最省事，打开作品页地址栏里就有"
        )
    return gid, token.lower()


def _canonical(gid: str, token: str) -> str:
    return f"{gid}-{token.lower()}"


def _count_images(work_dir: Path) -> int:
    try:
        return sum(
            1
            for path in work_dir.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        )
    except OSError:
        return 0


def _as_int(raw: object) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return -1

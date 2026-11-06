"""哔咔（PicACG）渠道的 `BaseChannel` 实现：拿 ID → 取元信息 → 下载图片。

打包与加密完全交给上层，所以这里看不到任何 zip/pdf/密码相关逻辑；
协议细节（签名、翻页、图片直链）都在同目录的 `api.py` 里。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar

from astrbot.api import logger

from ....domain import (
    ChannelError,
    ComicMeta,
    DownloadError,
    DownloadedComic,
    PluginSettings,
    SearchItem,
    SearchPage,
)
from ....domain.constants import PLUGIN_NAME
from ....utils import host_matches, sanitize_filename, split_link
from ...channel import BaseChannel, CancelToken, ProgressCallback
from ...progress import ProgressHeartbeat, ProgressState, emit
from .api import PicaClient, image_url
from .constants import (
    DEFAULT_MAX_CONCURRENT_IMAGES,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_RETRY_TIMES,
    DEFAULT_SECRET_KEY,
    SHORT_ID_LENGTH,
    WEB_COMIC_DOMAINS,
)

# 哔咔的漫画 ID 是 Mongo ObjectId：24 位十六进制
_COMIC_ID = re.compile(r"[0-9a-fA-F]{24}")
_UNSAFE_SUFFIX = re.compile(r"[^a-z0-9.]")


def _as_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _image_suffix(url: str) -> str:
    """从图片直链取后缀；取不到或不像后缀时按 jpg 落盘，上层只按后缀识别图片。"""
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if not suffix or len(suffix) > 5 or _UNSAFE_SUFFIX.search(suffix):
        return ".jpg"
    return suffix


class PicacgChannel(BaseChannel):
    name: ClassVar[str] = "picacg"
    command: ClassVar[str] = "picacg"
    command_aliases: ClassVar[tuple[str, ...]] = ("pica",)
    display_name: ClassVar[str] = "哔咔漫画"
    # 哔咔以 App 为主，网页版作品页在这个域名下；没有网页版时用户可从 App 分享内容里取 ID
    website: ClassVar[str] = "https://manhuapica.com"
    link_template: ClassVar[str] = "https://manhuapica.com/comic/<ID>"
    default_use_proxy: ClassVar[bool] = True
    requires: ClassVar[tuple[str, ...]] = ("httpx",)

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        """内置渠道默认启用；账号密码留空，等用户自己填（哔咔没有匿名访问）。"""
        return {
            "enable": True,
            "account": "",
            "password": "",
            "secret_key": DEFAULT_SECRET_KEY,
            "request_timeout": DEFAULT_REQUEST_TIMEOUT,
            "retry_times": DEFAULT_RETRY_TIMES,
            "max_concurrent_images": DEFAULT_MAX_CONCURRENT_IMAGES,
            "use_proxy": True,
        }

    def __init__(self, options: Mapping[str, Any], settings: PluginSettings) -> None:
        super().__init__(options, settings)
        # 登录态在插件生命周期内复用，避免每次下载都重新登录一次
        self._token = ""

    # ---- 客户端 ----
    def _new_client(self) -> PicaClient:
        return PicaClient(
            secret_key=self.opt_str("secret_key", DEFAULT_SECRET_KEY),
            proxy=self.resolve_proxy()[0],
            timeout=self.opt_int("request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=5),
            retry_times=self.opt_int("retry_times", DEFAULT_RETRY_TIMES, minimum=0),
        )

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[PicaClient]:
        """借出一个已登录的客户端，退出时关闭连接并把 token 状态同步回渠道实例。"""
        client = self._new_client()
        try:
            if self._token:
                client.token = self._token
            else:
                self._token = await client.login(
                    self.opt_str("account"), self.opt_str("password")
                )
            yield client
        finally:
            # 接口返回 401 时客户端会自行清空 token，同步回来让下次自动重新登录
            self._token = client.token
            await client.aclose()

    # ---- ID 解析 ----
    def match_link(self, text: str) -> str | None:
        # 哔咔的网页作品页也是 `/comic/<id>`，与 Hanime1 同形，只能靠域名区分
        host, path = split_link(text)
        if not host_matches(host, WEB_COMIC_DOMAINS):
            return None
        matched = _COMIC_ID.search(path)
        return matched.group(0).lower() if matched else None

    def normalize_id(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            raise ChannelError(
                f"请提供漫画 ID 或链接，例如 /{self.display_command} 5f1e2d3c4b5a69788796a5b4"
            )
        matched = _COMIC_ID.search(text)
        if matched:
            return matched.group(0).lower()
        raise ChannelError(
            "无法从输入中识别出哔咔漫画 ID：请给出作品链接里的 24 位 ID"
        )

    @classmethod
    def short_id(cls, comic_id: str) -> str:
        """文件名简写取 24 位 ObjectId 的后 8 位：前 8 位是秒级时间戳，同批作品会撞。"""
        text = (comic_id or "").lower()
        return text[-SHORT_ID_LENGTH:] if _COMIC_ID.fullmatch(text) else comic_id

    # ---- 元信息 ----
    async def fetch_meta(self, comic_id: str, work_dir: Path) -> ComicMeta:
        async with self._session() as client:
            comic = await client.fetch_comic(comic_id)
        return self._meta(comic_id, comic)

    # ---- 搜索 ----
    async def search(
        self, keyword: str, limit: int = 5, page: int = 1
    ) -> SearchPage:
        """哔咔搜索必须登录；没填账号密码时给明确提示，而不是让用户以为「搜不到」。"""
        if not self.opt_str("account") or not self.opt_str("password"):
            raise ChannelError(
                "哔咔搜索需要账号：请先在渠道配置里填写哔咔账号与密码"
            )

        async with self._session() as client:
            payload = await client.search(keyword, page=page)
        comics = (payload.get("data") or {}).get("comics") or {}
        docs = comics.get("docs") if isinstance(comics, dict) else None
        items: list[SearchItem] = []
        if isinstance(docs, list):
            for entry in docs[: max(1, limit)]:
                if not isinstance(entry, dict):
                    continue
                comic_id = str(entry.get("_id") or "").strip()
                if not comic_id:
                    continue
                items.append(
                    SearchItem(
                        channel=self.name,
                        comic_id=comic_id,
                        title=str(entry.get("title") or ""),
                        author=_author_name(entry.get("author")),
                        page_count=_as_int(
                            entry.get("epsCount") or entry.get("pagesCount"), 0
                        ),
                        cover_url=_cover_url(entry.get("thumb")),
                    )
                )
        total = _as_int(comics.get("total"), 0) if isinstance(comics, dict) else 0
        pages = _as_int(comics.get("pages"), 1) if isinstance(comics, dict) else 1
        return SearchPage(
            channel=self.name,
            keyword=keyword,
            items=items,
            total=total or None,
            has_more=pages > max(1, int(page)),
        )

    def _meta(self, comic_id: str, comic: Mapping[str, Any]) -> ComicMeta:
        creator = comic.get("_creator") or {}
        author = str(comic.get("author") or creator.get("name") or "")
        return ComicMeta(
            channel=self.name,
            comic_id=str(comic.get("_id") or comic_id),
            title=str(comic.get("title") or ""),
            author=author,
            page_count=_as_int(comic.get("epsCount"), 0),
        )

    # ---- 下载 ----
    async def download(
        self,
        comic_id: str,
        work_dir: Path,
        on_progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> DownloadedComic:
        started = time.monotonic()
        async with self._session() as client:
            comic = await client.fetch_comic(comic_id)
            episodes = await client.fetch_episodes(comic_id)
            if not episodes:
                raise DownloadError("哔咔没有返回任何章节，可能这本漫画已下架或需要付费解锁")

            plans = await self._plan_episodes(client, comic_id, episodes)
            total = sum(len(urls) for _, urls in plans)
            if total <= 0:
                raise DownloadError("哔咔返回的章节里没有任何图片，请确认这本漫画是否需要付费解锁")

            logger.info(
                f"[{PLUGIN_NAME}] 哔咔开始下载 {comic_id}："
                f"{len(plans)} 章节 / {total} 张图片，"
                f"网络 {self.proxy_summary()}"
            )
            root = work_dir / comic_id
            done, failed, bytes_total = await self._fetch_all(
                client, root, plans, total, on_progress, cancel
            )

        elapsed = max(time.monotonic() - started, 0.001)
        logger.info(
            f"[{PLUGIN_NAME}] 哔咔下载结束 {comic_id}：成功 {done} 张 / 失败 {failed} 张，"
            f"{bytes_total / 1048576:.1f} MB，耗时 {elapsed:.0f}s"
            f"（均 {done / elapsed * 60:.1f} 张/分）"
        )
        if done <= 0:
            raise DownloadError(
                "图片全部下载失败，请稍后重试；若一直失败，请在渠道配置里改用代理"
            )
        return DownloadedComic(
            meta=self._meta(comic_id, comic),
            source_dir=root,
            image_count=done,
            failed_images=failed,
        )

    async def _plan_episodes(
        self, client: PicaClient, comic_id: str, episodes: list[dict[str, Any]]
    ) -> list[tuple[str, list[str]]]:
        """先把每章的图片清单取齐：总数已知，进度才有意义。

        目录名带三位 order 前缀，打包时按数字排序不会把章节顺序搞乱。
        """
        plans: list[tuple[str, list[str]]] = []
        for index, episode in enumerate(episodes, start=1):
            order = _as_int(episode.get("order"), index)
            pages = await client.fetch_pages(comic_id, order)
            urls = [image_url(page.get("media") or {}) for page in pages]
            urls = [url for url in urls if url]
            if not urls:
                continue
            title = str(episode.get("title") or f"第{order}章")
            plans.append((f"{order:03d}_{sanitize_filename(title)}", urls))
        return plans

    async def _fetch_all(
        self,
        client: PicaClient,
        root: Path,
        plans: list[tuple[str, list[str]]],
        total: int,
        on_progress: ProgressCallback | None,
        cancel: CancelToken | None = None,
    ) -> tuple[int, int, int]:
        """按章节逐批并发下载图片，返回（成功张数、失败张数、落盘字节数）。"""
        semaphore = asyncio.Semaphore(
            self.opt_int("max_concurrent_images", DEFAULT_MAX_CONCURRENT_IMAGES, minimum=1)
        )
        progress = _Progress(total, on_progress)

        async def fetch_one(url: str, dest: Path) -> None:
            try:
                async with semaphore:
                    if dest.is_file() and dest.stat().st_size > 0:
                        # 断点续跑：这一页上次已经下好，直接算完成
                        await progress.skip(dest.stat().st_size)
                        return
                    size = await client.download_image(url, dest)
            except (ChannelError, OSError) as exc:
                logger.warning(f"[{PLUGIN_NAME}] 哔咔图片下载失败: {url} · {exc}")
                await progress.fail()
                return
            await progress.ok(size)

        heartbeat = ProgressHeartbeat(progress.state, on_tick=on_progress)
        heartbeat.start()
        try:
            for dir_name, urls in plans:
                if cancel is not None and cancel.cancelled:
                    logger.info(f"[{PLUGIN_NAME}] 哔咔下载被要求停手，提前收工")
                    break
                episode_dir = root / dir_name
                tasks = [
                    asyncio.create_task(
                        fetch_one(url, episode_dir / f"{index:04d}{_image_suffix(url)}")
                    )
                    for index, url in enumerate(urls, start=1)
                ]
                try:
                    await asyncio.gather(*tasks)
                except asyncio.CancelledError:
                    for task in tasks:
                        task.cancel()
                    raise
        finally:
            await heartbeat.stop()
        return progress.done, progress.failed, progress.bytes_total


class _Progress:
    """下载进度计数：并发任务各自累加，回调只在计数变更时触发一次。"""

    def __init__(self, total: int, on_progress: ProgressCallback | None) -> None:
        self.total = total
        self.on_progress = on_progress
        self.done = 0
        self.failed = 0
        self.bytes_total = 0
        self.started = time.monotonic()
        self._lock = asyncio.Lock()

    def state(self) -> ProgressState:
        """进度快照：心跳与聊天进度共用，缺了它「一分钟没动静」就没人汇报。"""
        return ProgressState(
            comic_id="",  # 命令层不靠这个字段认本子，排队提示用的是自己的登记表
            unit="图片",
            done=self.done,
            total=self.total,
            bytes_downloaded=self.bytes_total,
            elapsed=max(time.monotonic() - self.started, 0.001),
            failed=self.failed,
        )

    async def ok(self, size: int) -> None:
        async with self._lock:
            self.done += 1
            self.bytes_total += size
        await self._emit()

    async def skip(self, size: int) -> None:
        """断点续跑时已经落盘的那一页：计入完成，不再重下。"""
        async with self._lock:
            self.done += 1
            self.bytes_total += size
        await self._emit()

    async def fail(self) -> None:
        async with self._lock:
            self.failed += 1
        await self._emit()

    async def _emit(self) -> None:
        await emit(self.on_progress, self.state())


def _author_name(raw: object) -> str:
    if isinstance(raw, Mapping):
        return str(raw.get("name") or "").strip()
    if isinstance(raw, (list, tuple)):
        return "、".join(str(item).strip() for item in raw if str(item).strip())
    return str(raw or "").strip()


def _cover_url(raw: object) -> str:
    if isinstance(raw, Mapping):
        return image_url(raw)
    return str(raw or "").strip()

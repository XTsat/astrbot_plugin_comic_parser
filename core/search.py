"""搜索业务：单渠道超时、全渠道聚合、短时缓存与结果渲染。

搜索**不进入** `DownloadTask`：它只需要向上游要一页结果，没有 work_dir、元信息与打包。
聚合搜索的关键是「不能被一个慢渠道拖死」：每个渠道单独 `wait_for`，到点记为跳过，
其余渠道的结果照常返回；渠道之间并发执行，因此总耗时接近单个超时，而不是各渠道相加。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass

from astrbot.api import logger

from ..domain import (
    PLUGIN_NAME,
    PluginError,
    PluginSettings,
    SearchItem,
    SearchPage,
    SearchUnsupportedError,
)
from ..domain.constants import (
    SEARCH_CACHE_MAX_ENTRIES,
    SEARCH_MAX_TEXT_CHARS,
)
from .channel import BaseChannel

_WHITESPACE = re.compile(r"\s+")
# 标题/作者的展示上限：搜索结果里常有整段宣传语，原样发出去会刷屏
_TITLE_LIMIT = 42
_AUTHOR_LIMIT = 16


@dataclass(slots=True)
class SearchOutcome:
    """一个渠道的搜索结果：成功、空结果或失败都收在这一层，聚合层不抛异常。"""

    channel: BaseChannel
    page: SearchPage | None = None
    error: str = ""
    timed_out: bool = False
    elapsed: float = 0.0


@dataclass(slots=True)
class _CacheEntry:
    stored_at: float
    page: SearchPage


class SearchService:
    """搜索编排：超时、缓存、冷却与文本渲染。"""

    def __init__(
        self,
        channels: dict[str, BaseChannel],
        settings: PluginSettings,
    ) -> None:
        self.channels = channels
        self.settings = settings
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_search: dict[str, float] = {}

    # ---- 能力与冷却 ----
    def searchable(self) -> list[BaseChannel]:
        """启用且实现了搜索的渠道，保持注册顺序（结果展示顺序也与之一致）。"""
        return [
            channel
            for channel in self.channels.values()
            if channel.enabled and channel.supports_search
        ]

    def begin(self, session_id: str) -> str | None:
        """记录一次搜索请求；冷却期内返回面向用户的拒绝文案。

        冷却比下载防抖短得多，且只拦「同一会话连续搜索」——搜索结果会被缓存，
        正常翻看/换关键词不受影响。
        """
        interval = max(0, int(getattr(self.settings, "search_cooldown", 0)))
        if interval <= 0:
            return None

        now = time.monotonic()
        last = self._last_search.get(session_id)
        if last is not None:
            remain = interval - (now - last)
            if remain > 0:
                return f"⏳ 搜索太快了，请 {math.ceil(remain)} 秒后再试"
        self._last_search[session_id] = now
        if len(self._last_search) > 512:
            # 会话数量不可控，超限时清掉已过冷却期的记录；搜索冷却只是防刷，不必精确
            cutoff = now - interval
            for key, value in list(self._last_search.items()):
                if value < cutoff:
                    self._last_search.pop(key, None)
        return None

    # ---- 单渠道搜索 ----
    async def search_one(self, channel: BaseChannel, keyword: str) -> SearchOutcome:
        """搜一个渠道；超时/失败都收成 outcome，绝不向聚合层抛异常。"""
        started = time.monotonic()
        cache_key = (channel.name, keyword.casefold())
        cached = self._cache_get(cache_key)
        if cached is not None:
            return SearchOutcome(
                channel=channel, page=cached, elapsed=time.monotonic() - started
            )

        timeout = max(1, int(getattr(self.settings, "search_timeout", 10)))
        limit = max(1, int(getattr(self.settings, "search_page_size", 5)))
        lock = self._locks.setdefault(channel.name, asyncio.Lock())

        async def execute() -> SearchPage:
            # 锁也包在超时里：上一个同渠道搜索卡住时，后来者不会额外再等一个完整超时
            async with lock:
                # 等锁期间可能已有人搜过同一关键词，拿到锁后先复查缓存
                cached = self._cache_get(cache_key)
                if cached is not None:
                    return cached
                return await channel.search(keyword, limit=limit)

        try:
            page = await asyncio.wait_for(execute(), timeout=float(timeout))
            self._cache_put(cache_key, page)
            return SearchOutcome(
                channel=channel, page=page, elapsed=time.monotonic() - started
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[{PLUGIN_NAME}] {channel.display_name} 搜索超时（{timeout}s）：{keyword}"
            )
            return SearchOutcome(
                channel=channel,
                timed_out=True,
                error=f"超过 {timeout} 秒没有响应",
                elapsed=time.monotonic() - started,
            )
        except SearchUnsupportedError as exc:
            return SearchOutcome(
                channel=channel,
                error=str(exc),
                elapsed=time.monotonic() - started,
            )
        except PluginError as exc:
            return SearchOutcome(
                channel=channel,
                error=str(exc),
                elapsed=time.monotonic() - started,
            )
        except Exception as exc:  # 兜底：单个渠道的未预期异常不能拖垮整次搜索
            logger.error(
                f"[{PLUGIN_NAME}] {channel.display_name} 搜索失败：{exc}"
            )
            return SearchOutcome(
                channel=channel,
                error="搜索出现未预期错误，详情见日志",
                elapsed=time.monotonic() - started,
            )

    async def search_all(self, keyword: str) -> list[SearchOutcome]:
        """并发搜所有可用渠道；每个渠道各自超时，互不阻塞。"""
        channels = self.searchable()
        if not channels:
            raise SearchUnsupportedError(
                "当前没有支持搜索的渠道；可以先用 /漫画网站 到站内搜作品、找 ID"
            )
        return list(
            await asyncio.gather(
                *(self.search_one(channel, keyword) for channel in channels)
            )
        )

    # ---- 缓存 ----
    def _cache_get(self, key: tuple[str, str]) -> SearchPage | None:
        ttl = max(0, int(getattr(self.settings, "search_cache_ttl", 0)))
        entry = self._cache.get(key)
        if entry is None or ttl <= 0:
            return None
        if time.monotonic() - entry.stored_at > ttl:
            self._cache.pop(key, None)
            return None
        return entry.page

    def _cache_put(self, key: tuple[str, str], page: SearchPage) -> None:
        ttl = max(0, int(getattr(self.settings, "search_cache_ttl", 0)))
        if ttl <= 0:
            return
        self._cache[key] = _CacheEntry(stored_at=time.monotonic(), page=page)
        if len(self._cache) > SEARCH_CACHE_MAX_ENTRIES:
            # 简单淘汰：字典按写入顺序，丢掉最早的四分之一
            drop = len(self._cache) - SEARCH_CACHE_MAX_ENTRIES
            for stale in list(self._cache)[: max(drop, 1)]:
                self._cache.pop(stale, None)

    # ---- 渲染 ----
    def render_one(self, outcome: SearchOutcome, keyword: str) -> str:
        """只搜一个渠道时的结果文案；失败也直接给出原因，不吞掉。"""
        channel = outcome.channel
        if outcome.error:
            if outcome.timed_out:
                return f"❌ {channel.display_name} 搜索超时：{outcome.error}"
            return f"❌ {channel.display_name} 搜索失败：{outcome.error}"
        page = outcome.page
        if page is None or page.empty:
            return (
                f"🔎 {channel.display_name} 没有找到「{keyword}」的结果\n"
                "换个关键词，或 /漫画网站 到站内搜作品、找 ID"
            )

        lines = [f"🔎 {channel.display_name} 搜索「{keyword}」", ""]
        for index, item in enumerate(page.items, start=1):
            lines.extend(self._format_item(index, item, channel))
        lines.append("")
        lines.append(self._footer(page, outcome.elapsed))
        return self._bounded("\n".join(lines))

    def render_all(
        self, outcomes: list[SearchOutcome], keyword: str
    ) -> str:
        """全渠道搜索结果：按渠道轮流取，避免第一个渠道占满整条消息。"""
        limit = max(1, int(getattr(self.settings, "search_aggregate_limit", 8)))
        buckets = [
            outcome
            for outcome in outcomes
            if outcome.page is not None and outcome.page.items
        ]
        lines = [f"🔎 全渠道搜索「{keyword}」", ""]

        if not buckets:
            lines.append("没有渠道返回结果；换个关键词，或 /漫画网站 到站内搜作品、找 ID")
            lines.extend(self._failure_lines(outcomes))
            return self._bounded("\n".join(lines))

        selected: dict[str, list[SearchItem]] = {
            outcome.channel.name: [] for outcome in buckets
        }
        shown = 0
        index = 0
        while shown < limit:
            progressed = False
            for outcome in buckets:
                items = outcome.page.items
                if index < len(items):
                    selected[outcome.channel.name].append(items[index])
                    shown += 1
                    progressed = True
                    if shown >= limit:
                        break
            if not progressed:
                break
            index += 1

        for outcome in buckets:
            picked = selected[outcome.channel.name]
            if not picked:
                continue
            channel = outcome.channel
            lines.append(f"【{channel.display_name}】")
            for position, item in enumerate(picked, start=1):
                lines.extend(self._format_item(position, item, channel))
            lines.append("")

        lines.append(self._failure_lines(outcomes))
        slowest = max((outcome.elapsed for outcome in outcomes), default=0.0)
        lines.append(f"共 {shown} 条 · 用时 {slowest:.1f}s")
        lines.append(
            "只想搜一个渠道："
            + "、".join(f"/{o.channel.display_command} sou 关键词" for o in buckets)
        )
        return self._bounded("\n".join(lines))

    def _format_item(
        self, index: int, item: SearchItem, channel: BaseChannel
    ) -> list[str]:
        title = self._clip(getattr(item, "title", ""), _TITLE_LIMIT) or getattr(
            item, "display_title", ""
        )
        if not title:
            title = f"{channel.display_name} 作品"
        meta_parts: list[str] = []
        author = self._clip(getattr(item, "author", ""), _AUTHOR_LIMIT)
        if author:
            meta_parts.append(author)
        page_count = int(getattr(item, "page_count", 0) or 0)
        if page_count > 0:
            meta_parts.append(f"{page_count}P")
        meta = f"（{' · '.join(meta_parts)}）" if meta_parts else ""
        comic_id = str(getattr(item, "comic_id", ""))
        return [
            f"{index}. {title}{meta}",
            f"   ID：{comic_id}　下载：/{channel.display_command} {comic_id}",
        ]

    @staticmethod
    def _failure_lines(outcomes: list[SearchOutcome]) -> str:
        failures = [outcome for outcome in outcomes if outcome.error]
        if not failures:
            return ""
        return "\n".join(
            (
                f"⚠️ {outcome.channel.display_name} 搜索超时：{outcome.error}"
                if outcome.timed_out
                else f"⚠️ {outcome.channel.display_name}：{outcome.error}"
            )
            for outcome in failures
        )

    @staticmethod
    def _footer(page: SearchPage, elapsed: float) -> str:
        parts: list[str] = []
        if page.total is not None and page.total > 0:
            parts.append(f"共约 {page.total} 条")
        if page.has_more:
            parts.append("只展示了前几条，换个更精确的关键词可以缩小范围")
        parts.append(f"用时 {elapsed:.1f}s")
        return " · ".join(parts)

    @staticmethod
    def _clip(raw: str, limit: int) -> str:
        """压平换行/控制字符并截断：上游标题里常有整段换行与多余空白。"""
        text = _WHITESPACE.sub(" ", (raw or "").strip())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _bounded(text: str) -> str:
        """聊天消息过长会被平台截断；超限时截掉尾部并补一句说明。"""
        if len(text) <= SEARCH_MAX_TEXT_CHARS:
            return text
        return text[: SEARCH_MAX_TEXT_CHARS - 20].rstrip() + "\n…（结果过长，已截断）"

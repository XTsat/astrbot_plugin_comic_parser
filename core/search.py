"""搜索业务：单渠道超时、全渠道聚合、短时缓存、本地翻页与结果渲染。

搜索**不进入** `DownloadTask`：它只需要向上游要一页结果，没有 work_dir、元信息与打包。
聚合搜索的关键是「不能被一个慢渠道拖死」：每个渠道单独 `wait_for`，到点记为跳过，
其余渠道的结果照常返回；渠道之间并发执行，因此总耗时接近单个超时，而不是各渠道相加。

翻页是**本地翻页**：上游一次请求取回整页（通常 20–50 条）放进会话缓冲，展示层再按
「单渠道结果条数 / 聚合结果条数」切片。缓冲用完才带上游页码取下一页——展示页与上游页
不是一一对应，翻几页才需要打一次上游。条目只追加不移动，已经展示过的编号不会变，
按序号下载才不会下错。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field

from astrbot.api import logger

from ..domain import (
    PLUGIN_NAME,
    PluginError,
    PluginSettings,
    SearchItem,
    SearchPage,
    SearchQuery,
    SearchUnsupportedError,
)
from ..domain.constants import (
    SEARCH_CACHE_MAX_ENTRIES,
    SEARCH_MAX_TEXT_CHARS,
    SEARCH_RESULT_MAX_SESSIONS,
    SEARCH_RESULT_TTL,
    SEARCH_UPSTREAM_FETCH_LIMIT,
)
from .channel import BaseChannel
from .search_query import parse_search_query

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


@dataclass(slots=True)
class _SearchSession:
    """一次搜索的翻页状态与已展示结果，按会话保存。

    条目只在末尾追加：先按渠道轮流取，再按展示页切片。上游数据晚到时只往后加，
    已经展示过的编号不会变，按序号下载才不会下错。
    """

    stored_at: float
    query: SearchQuery
    aggregate: bool
    order: list[str]  # 渠道名，决定轮流取与展示的顺序
    page: int = 1
    elapsed: float = 0.0
    buffers: dict[str, list[SearchItem]] = field(default_factory=dict)
    consumed: dict[str, int] = field(default_factory=dict)
    upstream_pages: dict[str, int] = field(default_factory=dict)
    has_more: dict[str, bool] = field(default_factory=dict)
    totals: dict[str, int | None] = field(default_factory=dict)
    entries: list[tuple[str, SearchItem]] = field(default_factory=list)


class SearchService:
    """搜索编排：超时、缓存、冷却、翻页与文本渲染。"""

    def __init__(
        self,
        channels: dict[str, BaseChannel],
        settings: PluginSettings,
    ) -> None:
        self.channels = channels
        self.settings = settings
        self._cache: dict[tuple[str, str, int], _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_search: dict[str, float] = {}
        self._sessions: dict[str, _SearchSession] = {}

    # ---- 能力与冷却 ----
    def parse_query(self, raw: str) -> SearchQuery:
        """解析用户输入；语法错误由调用方按 `SearchQueryError` 展示。"""
        return parse_search_query(raw)

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
    async def search_one(
        self, channel: BaseChannel, query: SearchQuery, page: int = 1
    ) -> SearchOutcome:
        """搜一个渠道的上游第 ``page`` 页；超时/失败都收成 outcome，绝不向聚合层抛异常。"""
        started = time.monotonic()
        page = max(1, int(page))
        cache_key = (channel.name, query.cache_key, page)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return SearchOutcome(
                channel=channel, page=cached, elapsed=time.monotonic() - started
            )

        timeout = max(1, int(getattr(self.settings, "search_timeout", 10)))
        limit = SEARCH_UPSTREAM_FETCH_LIMIT
        lock = self._locks.setdefault(channel.name, asyncio.Lock())

        async def execute() -> SearchPage:
            # 锁也包在超时里：上一个同渠道搜索卡住时，后来者不会额外再等一个完整超时
            async with lock:
                # 等锁期间可能已有人搜过同一关键词，拿到锁后先复查缓存
                cached = self._cache_get(cache_key)
                if cached is not None:
                    return cached
                return await channel.search(query, limit=limit, page=page)

        try:
            result = await asyncio.wait_for(execute(), timeout=float(timeout))
            self._cache_put(cache_key, result)
            return SearchOutcome(
                channel=channel, page=result, elapsed=time.monotonic() - started
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[{PLUGIN_NAME}] {channel.display_name} 搜索超时（{timeout}s）：{query.raw}"
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

    async def search_all(self, query: SearchQuery) -> list[SearchOutcome]:
        """并发搜所有可用渠道的上游第一页；每个渠道各自超时，互不阻塞。"""
        channels = self.searchable()
        if not channels:
            raise SearchUnsupportedError(
                "当前没有支持搜索的渠道；可以先用 /漫画网站 到站内搜作品、找 ID"
            )
        return list(
            await asyncio.gather(
                *(self.search_one(channel, query) for channel in channels)
            )
        )

    # ---- 缓存 ----
    def _cache_get(self, key: tuple[str, str, int]) -> SearchPage | None:
        ttl = max(0, int(getattr(self.settings, "search_cache_ttl", 0)))
        entry = self._cache.get(key)
        if entry is None or ttl <= 0:
            return None
        if time.monotonic() - entry.stored_at > ttl:
            self._cache.pop(key, None)
            return None
        return entry.page

    def _cache_put(self, key: tuple[str, str, int], page: SearchPage) -> None:
        ttl = max(0, int(getattr(self.settings, "search_cache_ttl", 0)))
        if ttl <= 0:
            return
        self._cache[key] = _CacheEntry(stored_at=time.monotonic(), page=page)
        if len(self._cache) > SEARCH_CACHE_MAX_ENTRIES:
            # 简单淘汰：字典按写入顺序，丢掉最早的四分之一
            drop = len(self._cache) - SEARCH_CACHE_MAX_ENTRIES
            for stale in list(self._cache)[: max(drop, 1)]:
                self._cache.pop(stale, None)

    # ---- 翻页与序号下载 ----
    def _page_size(self, session: _SearchSession) -> int:
        """当前会话每页展示多少条：单渠道与全渠道用各自的配置。"""
        if session.aggregate:
            return max(1, int(getattr(self.settings, "search_aggregate_limit", 8)))
        return max(1, int(getattr(self.settings, "search_page_size", 5)))

    def _build_session(
        self,
        query: SearchQuery,
        aggregate: bool,
        pages: list[tuple[BaseChannel, SearchPage]],
    ) -> _SearchSession | None:
        usable = [
            (channel, page)
            for channel, page in pages
            if page is not None and page.items
        ]
        if not usable:
            return None
        order = [channel.name for channel, _ in usable]
        return _SearchSession(
            stored_at=time.monotonic(),
            query=query,
            aggregate=aggregate,
            order=order,
            buffers={channel.name: list(page.items) for channel, page in usable},
            consumed={channel.name: 0 for channel, _ in usable},
            upstream_pages={channel.name: 1 for channel, _ in usable},
            has_more={channel.name: bool(page.has_more) for channel, page in usable},
            totals={channel.name: page.total for channel, page in usable},
        )

    def _store_session(self, session_id: str, session: _SearchSession) -> None:
        if not session_id:
            return
        self._sessions[session_id] = session
        self._prune_sessions()

    def _drop_session(self, session_id: str) -> None:
        if session_id:
            self._sessions.pop(session_id, None)

    def _prune_sessions(self) -> None:
        """会话数量不可控：超限时先清过期记录，仍超限就丢最早的那批。"""
        if len(self._sessions) <= SEARCH_RESULT_MAX_SESSIONS:
            return
        cutoff = time.monotonic() - SEARCH_RESULT_TTL
        for key, session in list(self._sessions.items()):
            if session.stored_at < cutoff:
                self._sessions.pop(key, None)
        overflow = len(self._sessions) - SEARCH_RESULT_MAX_SESSIONS
        if overflow > 0:
            oldest = sorted(
                self._sessions, key=lambda key: self._sessions[key].stored_at
            )[:overflow]
            for key in oldest:
                self._sessions.pop(key, None)

    def _emit_available(self, session: _SearchSession, target: int) -> None:
        """只从已缓冲的结果里按渠道轮流补条目，不发请求。

        一次补一整轮（每个还有未消费条目的渠道各取一条），保证轮流展示；轮次可能
        略微超过 ``target``，多出来的条目留给下一页，不会重复。
        """
        while len(session.entries) < target:
            progressed = False
            for name in session.order:
                items = session.buffers.get(name, [])
                position = session.consumed.get(name, 0)
                if position < len(items):
                    session.entries.append((name, items[position]))
                    session.consumed[name] = position + 1
                    progressed = True
            if not progressed:
                return

    async def _fetch_more(self, session: _SearchSession) -> None:
        """给缓冲已消费完且上游还有下一页的渠道各取下一条上游页。"""
        targets = [
            self.channels[name]
            for name in session.order
            if name in self.channels
            and session.has_more.get(name)
            and session.consumed.get(name, 0) >= len(session.buffers.get(name, []))
        ]
        if not targets:
            return
        next_pages = {
            channel.name: session.upstream_pages.get(channel.name, 1) + 1
            for channel in targets
        }
        outcomes = await asyncio.gather(
            *(
                self.search_one(
                    channel, session.query, page=next_pages[channel.name]
                )
                for channel in targets
            )
        )
        for channel, outcome in zip(targets, outcomes):
            name = channel.name
            if outcome.page is None or not outcome.page.items:
                session.has_more[name] = False
                if outcome.error:
                    logger.warning(
                        f"[{PLUGIN_NAME}] {channel.display_name} 翻页失败，"
                        f"已停止该渠道的后续页：{outcome.error}"
                    )
                continue
            session.buffers[name].extend(outcome.page.items)
            session.upstream_pages[name] = next_pages[name]
            session.has_more[name] = bool(outcome.page.has_more)

    async def _ensure_entries(self, session: _SearchSession, target: int) -> None:
        """补到 ``target`` 条：先吃本地缓冲，不够再按需取上游下一页。"""
        self._emit_available(session, target)
        while len(session.entries) < target:
            if not any(
                session.has_more.get(name)
                and session.consumed.get(name, 0)
                >= len(session.buffers.get(name, []))
                for name in session.order
            ):
                return
            await self._fetch_more(session)
            self._emit_available(session, target)

    def _has_next(self, session: _SearchSession) -> bool:
        if any(session.has_more.get(name) for name in session.order):
            return True
        return any(
            session.consumed.get(name, 0) < len(session.buffers.get(name, []))
            for name in session.order
        )

    def _nav_command(self, session: _SearchSession) -> str:
        if not session.aggregate and session.order:
            channel = self.channels.get(session.order[0])
            if channel is not None:
                return f"/{channel.display_command}"
        return "/comic"

    async def turn_page(self, session_id: str, step: int) -> str | None:
        """翻页：返回新的结果文案；没有可翻的搜索时返回 None，边界单独给文案。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.monotonic() - session.stored_at > SEARCH_RESULT_TTL:
            self._sessions.pop(session_id, None)
            return None

        target = session.page + step
        if target < 1:
            return "📄 已经是第一页了"
        size = self._page_size(session)
        if step > 0:
            await self._ensure_entries(session, target * size)
        if (target - 1) * size >= len(session.entries):
            return f"📄 没有更多结果了（第 {session.page} 页已是最后一页）"

        session.page = target
        session.stored_at = time.monotonic()
        return self._render_session(session)

    def resolve_index(
        self, session_id: str, token: str
    ) -> tuple[BaseChannel, str] | None:
        """把序号还原成当前搜索会话里的（渠道, 规范 ID）；过期/越界/渠道停用时返回 None。"""
        if not session_id or not token.isdigit():
            return None
        index = int(token)
        if index < 1:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.monotonic() - session.stored_at > SEARCH_RESULT_TTL:
            self._sessions.pop(session_id, None)
            return None
        self._emit_available(session, index)
        if index > len(session.entries):
            return None
        channel_name, item = session.entries[index - 1]
        channel = self.channels.get(channel_name)
        if channel is None or not channel.enabled:
            return None
        return channel, str(item.comic_id)

    # ---- 渲染 ----
    async def render_one(
        self, outcome: SearchOutcome, query: SearchQuery, session_id: str = ""
    ) -> str:
        """只搜一个渠道时的结果文案；失败也直接给出原因，不吞掉。"""
        channel = outcome.channel
        if outcome.error:
            self._drop_session(session_id)
            if outcome.timed_out:
                return f"❌ {channel.display_name} 搜索超时：{outcome.error}"
            return f"❌ {channel.display_name} 搜索失败：{outcome.error}"
        page = outcome.page
        if page is None or page.empty:
            self._drop_session(session_id)
            lines = [
                f"🔎 {channel.display_name} 没有找到「{query.raw}」的结果",
                "换个关键词，或 /漫画网站 到站内搜作品、找 ID",
            ]
            if page is not None and page.note:
                lines.append(f"ℹ️ {page.note}")
            return "\n".join(lines)

        session = self._build_session(query, False, [(channel, page)])
        if session is None:
            self._drop_session(session_id)
            return f"🔎 {channel.display_name} 没有找到「{query.raw}」的结果"
        session.elapsed = outcome.elapsed
        self._store_session(session_id, session)
        # 第一页也要补满：上游一页可能比展示页小，不补会把后面的条目挤到翻页之外
        await self._ensure_entries(session, self._page_size(session))
        return self._render_session(session, extra=self._note_lines([outcome]))

    async def render_all(
        self, outcomes: list[SearchOutcome], query: SearchQuery, session_id: str = ""
    ) -> str:
        """全渠道搜索结果：按渠道轮流取，避免第一个渠道占满整条消息。"""
        buckets = [
            (outcome.channel, outcome.page)
            for outcome in outcomes
            if outcome.page is not None and outcome.page.items
        ]
        if not buckets:
            self._drop_session(session_id)
            lines = [f"🔎 全渠道搜索「{query.raw}」", ""]
            lines.append("没有渠道返回结果；换个关键词，或 /漫画网站 到站内搜作品、找 ID")
            extra = "\n".join(
                part
                for part in (self._failure_lines(outcomes), self._note_lines(outcomes))
                if part
            )
            if extra:
                lines.append(extra)
            return self._bounded("\n".join(lines))

        session = self._build_session(query, True, buckets)
        if session is None:
            self._drop_session(session_id)
            lines = [f"🔎 全渠道搜索「{query.raw}」", ""]
            lines.append("没有渠道返回结果；换个关键词，或 /漫画网站 到站内搜作品、找 ID")
            return self._bounded("\n".join(lines))
        session.elapsed = max((outcome.elapsed for outcome in outcomes), default=0.0)
        self._store_session(session_id, session)
        await self._ensure_entries(session, self._page_size(session))
        extra = "\n".join(
            part
            for part in (self._failure_lines(outcomes), self._note_lines(outcomes))
            if part
        )
        return self._render_session(session, extra=extra)

    def _render_session(self, session: _SearchSession, extra: str = "") -> str:
        size = self._page_size(session)
        target = session.page * size
        self._emit_available(session, target)
        start = (session.page - 1) * size
        visible = session.entries[start:target]

        title = self._session_title(session)
        if session.page > 1:
            title += f"（第 {session.page} 页）"
        lines = [title, ""]
        for offset, (channel_name, item) in enumerate(visible):
            channel = self.channels.get(channel_name)
            lines.extend(
                self._format_item(
                    start + offset + 1,
                    item,
                    channel,
                    show_channel=session.aggregate,
                )
            )
        if extra:
            lines.append("")
            lines.append(extra)
        lines.append("")
        lines.append(self._session_footer(session))
        lines.append(self._download_hint(session, start + 1))
        nav = self._nav_hint(session)
        if nav:
            lines.append(nav)
        return self._bounded("\n".join(lines))

    def _session_title(self, session: _SearchSession) -> str:
        if session.aggregate:
            return f"🔎 全渠道搜索「{session.query.raw}」"
        channel = self.channels.get(session.order[0]) if session.order else None
        name = channel.display_name if channel else (session.order[0] if session.order else "")
        return f"🔎 {name} 搜索「{session.query.raw}」"

    def _session_footer(self, session: _SearchSession) -> str:
        parts: list[str] = []
        if session.page > 1:
            parts.append(f"第 {session.page} 页")
        elif not session.aggregate and session.order:
            total = session.totals.get(session.order[0])
            if total:
                parts.append(f"共约 {total} 条")
        if session.page == 1 and session.elapsed:
            parts.append(f"用时 {session.elapsed:.1f}s")
        if not parts:
            parts.append(f"第 {session.page} 页")
        return " · ".join(parts)

    def _download_hint(self, session: _SearchSession, first_index: int) -> str:
        return (
            f"下载：{self._nav_command(session)} {first_index}"
            f"（把 {first_index} 换成上面的序号）"
        )

    def _nav_hint(self, session: _SearchSession) -> str:
        command = self._nav_command(session)
        parts: list[str] = []
        if session.page > 1:
            parts.append(f"上一页：{command} 上一页")
        if self._has_next(session):
            parts.append(f"下一页：{command} 下一页")
        return "　".join(parts)

    def _format_item(
        self,
        index: int,
        item: SearchItem,
        channel: BaseChannel | None,
        show_channel: bool,
    ) -> list[str]:
        title = self._clip(getattr(item, "title", ""), _TITLE_LIMIT) or getattr(
            item, "display_title", ""
        )
        if not title:
            title = f"{channel.display_name if channel else item.channel} 作品"
        meta_parts: list[str] = []
        author = self._clip(getattr(item, "author", ""), _AUTHOR_LIMIT)
        if author:
            meta_parts.append(author)
        page_count = int(getattr(item, "page_count", 0) or 0)
        if page_count > 0:
            meta_parts.append(f"{page_count}P")
        meta = f"（{' · '.join(meta_parts)}）" if meta_parts else ""
        comic_id = str(getattr(item, "comic_id", ""))
        tag = ""
        if show_channel:
            name = channel.display_name if channel else item.channel
            tag = f"[{name}] "
        return [f"{index}. {tag}{title}{meta}　ID：{comic_id}"]

    @staticmethod
    def _note_lines(outcomes: list[SearchOutcome]) -> str:
        """把各渠道的降级说明按文案分组，避免同一句话重复四遍。"""
        grouped: dict[str, list[str]] = {}
        for outcome in outcomes:
            page = outcome.page
            if page is None or not page.note:
                continue
            grouped.setdefault(page.note, []).append(outcome.channel.display_name)
        return "\n".join(
            f"ℹ️ {'、'.join(names)}：{note}" for note, names in grouped.items()
        )

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

"""排行榜业务：单渠道超时、全渠道聚合、短时缓存、翻页、序号记忆与文字渲染。

榜单与搜索最大的差别是「上游页就是展示页」：没有关键词、没有筛选降级，也不需要本地
缓冲——所以这里没有 `search.py` 那套缓冲/切片状态机，链路只有「按窗口取一页 → 展示 →
翻下一页再取下一页」。序号记忆与搜索同一套语义：同一个数字还原成（渠道, 规范 ID），
但编号只属于**当前这一页**（翻页后重新从 1 开始），因为榜单的名次本身就是页内概念。

聚合是「每渠道各取头部并列展示」，不做跨站混排：不同站点的热度口径不一样，
混在一起排出来的名次没有意义。缺失的时间窗由渠道自己声明（`ranking_boards`），
这里只把「谁没有这一档」写进说明，不报错、也不悄悄换一档。
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
    RankingItem,
    RankingPage,
)
from ..domain.constants import (
    RANKING_BOARD_ALIASES,
    RANKING_BOARD_LABELS,
    RANKING_CACHE_MAX_ENTRIES,
    RANKING_INDEX_HINT_MAX,
    RANKING_MAX_TEXT_CHARS,
    RANKING_RESULT_TTL,
    RANKING_RESULT_MAX_SESSIONS,
)
from ..utils.title import AI_MARK_BADGE, has_ai_mark
from .channel import BaseChannel, search_proxy_override

_WHITESPACE = re.compile(r"\s+")
# 标题/作者的展示上限：榜单里的标题常带整段宣传语，原样发出去会刷屏。
# 60 字是折中：Hanime1 这类「[社团] 日文原名 | 中文译名」的标题在 40 字时几乎只剩社团名
_TITLE_LIMIT = 60
_AUTHOR_LIMIT = 16


def parse_board(raw: str) -> str | None:
    """把用户给的时间窗词收敛成规范窗口名；认不出时返回 None。"""
    token = (raw or "").strip().lower()
    if not token:
        return None
    return RANKING_BOARD_ALIASES.get(token)


def board_label(board: str) -> str:
    return RANKING_BOARD_LABELS.get(board, board)


@dataclass(slots=True)
class RankingOutcome:
    """一个渠道的榜单结果：成功、空榜或失败都收在这一层，聚合层不抛异常。

    ``stream`` 是这个渠道这次的本地缓冲：取完页要交回会话，下一页接着用。
    """

    channel: BaseChannel
    page: RankingPage | None = None
    error: str = ""
    timed_out: bool = False
    elapsed: float = 0.0
    stream: "_RankStream | None" = None


@dataclass(slots=True)
class RankingResult:
    """一次榜单请求的成品：渲染好的文案 + 这次翻到第几页。"""

    board: str
    page: int
    text: str
    aggregate: bool
    count: int = 0  # 这一页实际拿到了多少条；0 表示这页是空的
    start_index: int = 1  # 这一页第一条的序号（翻页后接续，与搜索一致）
    elapsed: float = 0.0
    # 图片版用：每个渠道这一页的原始结果（文字版不需要，渲染卡片时才用得上）
    pages: list[tuple[BaseChannel, RankingPage]] = field(default_factory=list)
    # 失败与降级说明；文字版已写进 text，只出图时要靠它单独发一句
    warnings: str = ""


@dataclass(slots=True)
class RankingTurn:
    """翻页结果：``result`` 是成品，``message`` 是边界提示（二选一）。"""

    result: RankingResult | None = None
    message: str = ""


@dataclass(slots=True)
class _CacheEntry:
    stored_at: float
    page: RankingPage


@dataclass(slots=True)
class _RankingSession:
    """本会话最近一次榜单：翻页参数与序号表。

    ``entries`` 只存**当前这一页**的条目，但编号与搜索一致、跨页连续：``counts``
    记住每一页展示了几条，第 N 页的起始序号就是前面各页之和 + 1。这样「翻页后回序号」
    与搜索是同一套心智，也不会出现「第 2 页又从 1 开始、回 3 下到第 1 页那本」。
    """

    stored_at: float
    board: str
    aggregate: bool
    order: list[str]  # 渠道名，决定展示顺序
    page: int = 1
    entries: list[tuple[str, str]] = field(default_factory=list)  # (渠道名, 作品 ID)
    start_index: int = 1  # 当前这一页第一条的序号
    counts: dict[int, int] = field(default_factory=dict)  # 页码 → 该页展示了几条
    # 渠道名 → 本地榜单缓冲（上游候选按名次攒着，取页时才过滤，零头不会被丢）
    streams: dict[str, "_RankStream"] = field(default_factory=dict)


@dataclass
class _RankStream:
    """一个渠道某档的本地榜单缓冲。

    ``items`` 是**按上游名次顺序**攒下来的候选（含超上限的，先不动——过滤只发生在
    取页的时候，所以改上限、翻页都不会把「零头」丢掉）；``page`` / ``offset`` 是下一次
    要往上游哪儿要数据（本页第几条 / 下一页）；``starts`` 记住每个逻辑页从 ``items``
    的哪一条开始，翻回上一页靠它。
    """

    items: list[RankingItem] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)  # 已收下的作品 ID，用来挡重复块
    total: int | None = None
    page: int = 1  # 下一个要拉的上游页
    offset: int = 0  # 该上游页里下一个要拉的候选序号
    exhausted: bool = False  # 上游已经没有更多候选
    cursor: int = 0  # 下一段从这里开始取（= 已经分给前面各页的条数）
    starts: dict[int, int] = field(default_factory=dict)  # 逻辑页 → items 起始下标
    notes: list[str] = field(default_factory=list)  # 渠道给的说明（去重后一起展示）


class RankingService:
    """榜单编排：超时、缓存、冷却、翻页、序号记忆与文本渲染。"""

    # 档位从窄到宽：缺档时按这个顺序找最近的一档（同样远时优先更窄的那档）
    _BOARD_ORDER = ("day", "week", "month", "all")

    def __init__(
        self,
        channels: dict[str, BaseChannel],
        settings: PluginSettings,
    ) -> None:
        self.channels = channels
        self.settings = settings
        self._cache: dict[tuple[str, str, int, int, int], _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_view: dict[str, float] = {}
        self._sessions: dict[str, _RankingSession] = {}

    # ---- 能力与冷却 ----
    def rankable(self) -> list[BaseChannel]:
        """启用且实现了榜单的渠道，保持注册顺序（展示顺序也与之一致）。"""
        return [
            channel
            for channel in self.channels.values()
            if channel.enabled and channel.supports_ranking
        ]

    def supports_board(self, channel: BaseChannel, board: str) -> bool:
        return board in channel.ranking_boards

    def begin(self, session_id: str) -> str | None:
        """记录一次榜单请求；冷却期内返回面向用户的拒绝文案。"""
        now = time.monotonic()
        interval = max(0, int(getattr(self.settings, "ranking_cooldown", 0)))
        if interval > 0:
            last = self._last_view.get(session_id)
            if last is not None:
                remain = interval - (now - last)
                if remain > 0:
                    return f"⏳ 看榜太快了，请 {math.ceil(remain)} 秒后再试"
            self._last_view[session_id] = now
        if len(self._last_view) > RANKING_RESULT_MAX_SESSIONS:
            cutoff = now - max(interval, 60)
            for key, value in list(self._last_view.items()):
                if value < cutoff:
                    self._last_view.pop(key, None)
        return None

    def session_active(self, session_id: str) -> bool:
        """本会话是否有仍然有效的榜单（序号下载/翻页据此决定问哪个服务）。"""
        session = self._sessions.get(session_id)
        if session is None:
            return False
        if time.monotonic() - session.stored_at > RANKING_RESULT_TTL:
            self._sessions.pop(session_id, None)
            return False
        return True

    # ---- 取数 ----
    def _fetch_limit(self, aggregate: bool) -> int:
        """每个渠道取多少条：聚合时只取头部，单渠道时按展示条数取。"""
        if aggregate:
            return max(1, int(self.settings.ranking_aggregate_limit))
        limit = max(1, int(self.settings.ranking_page_size))
        if self.settings.ranking_output in ("image", "both"):
            # 一页图要的封面数可能比文字版一页的条数多，取大的那个才够画
            limit = max(limit, int(self.settings.ranking_image_page_size))
        return limit

    # 一页最多往上游要几块：被上限挡掉的条目可能连着好几块都没货，
    # 但补位不值得无限要——4 块已经是四倍的候选量
    _MAX_CHUNKS = 4

    async def ranking_one(
        self,
        channel: BaseChannel,
        board: str,
        page: int = 1,
        limit: int = 10,
        session_id: str = "",
        dry_run: bool = False,
    ) -> RankingOutcome:
        """取一个渠道榜单的第 ``page`` 个逻辑页；超时/失败都收成 outcome，不向聚合层抛异常。

        取数走本会话里这个渠道的**本地缓冲**（:class:`_RankStream`）：缓冲里已经有够用的
        候选就只用本地的，不够再按 ``(上游页, 页内序号)`` 往下游要一块，攒进同一个缓冲里
        接着挑。因为过滤只发生在取页这一刻，被挡掉的条目和没展示的零头都还在缓冲里，
        翻下一页时接着用，既不重复也不漏。
        """
        started = time.monotonic()
        page = max(1, int(page))
        limit = max(1, int(limit))
        if not channel.supports_ranking:
            return RankingOutcome(
                channel=channel,
                error=f"{channel.display_name} 暂不支持排行榜",
                elapsed=time.monotonic() - started,
            )
        # 缺档不再跳过：按最近的一档取，并在说明里写清「你要的是哪档、实际看的是哪档」。
        # 榜单这件事「有内容」比「档位严格」重要，尤其 E-Hentai 只有一档「当前热门」。
        requested = board
        fallback_note = ""
        if not self.supports_board(channel, board):
            fallback = self._fallback_board(channel, board)
            if fallback is None:
                return RankingOutcome(
                    channel=channel,
                    error=f"{channel.display_name} 暂不支持排行榜",
                    elapsed=time.monotonic() - started,
                )
            board = fallback
            fallback_note = f"没有{board_label(requested)}，已改看{board_label(fallback)}"

        timeout = max(1, int(getattr(self.settings, "ranking_timeout", 15)))
        # 缓冲按「用户要的那一档」归类：渠道缺档改看别的档时，同一个会话仍用同一份缓冲
        stream = self._stream_for(session_id, channel.name, requested)
        start = stream.starts.get(page, stream.cursor)
        picked, end, skipped, has_more, error = await self._assemble(
            channel, board, stream, start, limit, timeout
        )
        if error is not None and not picked:
            elapsed = time.monotonic() - started
            if isinstance(error, asyncio.TimeoutError):
                return RankingOutcome(
                    channel=channel,
                    error=f"超时（{timeout} 秒）",
                    timed_out=True,
                    elapsed=elapsed,
                )
            if isinstance(error, PluginError):
                return RankingOutcome(channel=channel, error=str(error), elapsed=elapsed)
            logger.warning(
                f"[{PLUGIN_NAME}] {channel.display_name} 榜单出现未预期异常: {error}"
            )
            return RankingOutcome(
                channel=channel, error=f"未预期错误：{error}", elapsed=elapsed
            )

        if not dry_run:
            stream.starts[page] = start
            stream.cursor = end

        note_parts = [fallback_note, *stream.notes]
        cap_label = self.settings.ranking_cap_label
        if skipped and cap_label:
            note_parts.append(f"已跳过 {skipped} 本超上限的长篇（{cap_label}）")
        result = RankingPage(
            channel=channel.name,
            board=requested,
            items=picked,
            total=stream.total,
            has_more=has_more,
            note="；".join(part for part in dict.fromkeys(note_parts) if part),
        )
        return RankingOutcome(
            channel=channel,
            page=result,
            elapsed=time.monotonic() - started,
            stream=stream if not dry_run else None,
        )

    def _stream_for(
        self, session_id: str, channel_name: str, board: str
    ) -> _RankStream:
        """本会话里该渠道的缓冲；没有（或换了档位）就给一个空的。"""
        session = self._sessions.get(session_id) if session_id else None
        if session is not None and session.board == board:
            stream = session.streams.get(channel_name)
            if stream is not None:
                return stream
        return _RankStream()

    async def _assemble(
        self,
        channel: BaseChannel,
        board: str,
        stream: _RankStream,
        start: int,
        limit: int,
        timeout: int,
    ) -> tuple[list[RankingItem], int, int, bool, Exception | None]:
        """从缓冲里攒出一页：返回（条目, 结束下标, 跳过几本, 上游还有没有, 出错时的异常）。

        只有缓冲里凑不满一页时才回上游要下一块；要多少块有上限，也受单渠道超时约束。
        """
        idx = start
        picked: list[RankingItem] = []
        skipped = 0
        chunks = 0
        error: Exception | None = None
        started = time.monotonic()
        while len(picked) < limit:
            while idx < len(stream.items) and len(picked) < limit:
                item = stream.items[idx]
                idx += 1
                if self.settings.ranking_size_ok(item.page_count, item.chapter_count):
                    picked.append(item)
                elif item.page_count > 0 or item.chapter_count > 0:
                    skipped += 1
            if len(picked) >= limit or stream.exhausted:
                break
            if chunks >= self._MAX_CHUNKS or time.monotonic() - started > timeout:
                break
            chunks += 1
            try:
                raw = await self._fetch_upstream(
                    channel, board, stream.page, stream.offset, limit, timeout
                )
            except Exception as exc:  # noqa: BLE001 - 交给调用方决定怎么报
                error = exc
                break
            if raw.note and raw.note not in stream.notes:
                stream.notes.append(raw.note)
            if stream.total is None:
                stream.total = raw.total
            fresh = 0
            for item in raw.items:
                if item.comic_id in stream.seen:
                    continue
                stream.seen.add(item.comic_id)
                stream.items.append(item)
                fresh += 1
            if raw.exhausted:
                stream.page += 1
                stream.offset = 0
            else:
                stream.offset += len(raw.items)
            # 上游到底了、这一块没货、或整块都是重复（站点不认页码）就收手
            if not raw.has_more or not raw.items or (raw.items and not fresh):
                stream.exhausted = True
        has_more = not stream.exhausted or idx < len(stream.items)
        return picked, idx, skipped, has_more, error

    async def _fetch_upstream(
        self,
        channel: BaseChannel,
        board: str,
        page: int,
        offset: int,
        limit: int,
        timeout: int,
    ) -> RankingPage:
        """取上游第 ``page`` 页、从第 ``offset`` 条起的一块候选（带缓存 + 超时 + 代理重试）。"""
        cache_key = (channel.name, board, page, offset, limit)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        proxy = (getattr(self.settings, "proxy", "") or "").strip()
        auto_proxy = bool(getattr(self.settings, "search_auto_proxy", True)) and bool(
            proxy
        )
        retry_timeout = max(
            1, int(getattr(self.settings, "search_auto_proxy_timeout", 15))
        )
        lock = self._locks.setdefault(channel.name, asyncio.Lock())

        async def fetch(budget: int) -> RankingPage:
            # 锁也包在超时里：同渠道上一个请求卡住时，后来者不会再多等一个完整超时
            async with lock:
                cached_page = self._cache_get(cache_key)
                if cached_page is not None:
                    return cached_page
                return await asyncio.wait_for(
                    channel.ranking(board, page=page, limit=limit, offset=offset),
                    timeout=budget,
                )

        try:
            result = await fetch(timeout)
        except (asyncio.TimeoutError, PluginError, OSError) as exc:
            if not auto_proxy:
                raise
            # 直连慢/被挡时用插件代理再给一次机会，预算独立于正常超时
            logger.info(
                f"[{PLUGIN_NAME}] {channel.display_name} 榜单直连不理想（{exc}），"
                f"改用代理重试"
            )
            with search_proxy_override(proxy):
                result = await fetch(retry_timeout)
        if result is not None and not result.empty:
            self._cache_put(cache_key, result)
        return result

    def _fallback_board(self, channel: BaseChannel, board: str) -> str | None:
        """渠道没有这一档时用哪一档顶：取距离最近的一档，等距时用更窄的。"""
        available = [item for item in self._BOARD_ORDER if item in channel.ranking_boards]
        if not available:
            return None
        if board not in self._BOARD_ORDER:
            return available[0]
        index = self._BOARD_ORDER.index(board)
        return min(
            available,
            key=lambda item: (
                abs(self._BOARD_ORDER.index(item) - index),
                self._BOARD_ORDER.index(item),
            ),
        )

    async def ranking_all(
        self,
        board: str,
        page: int = 1,
        limit: int = 5,
        session_id: str = "",
        dry_run: bool = False,
    ) -> list[RankingOutcome]:
        """并发取所有支持榜单的渠道；彼此独立，一个失败不影响其它。"""
        targets = self.rankable()
        if not targets:
            return []
        return list(
            await asyncio.gather(
                *(
                    self.ranking_one(
                        channel,
                        board,
                        page=page,
                        limit=limit,
                        session_id=session_id,
                        dry_run=dry_run,
                    )
                    for channel in targets
                )
            )
        )

    # ---- 缓存 ----
    def _cache_get(self, key: tuple[str, str, int, int, int]) -> RankingPage | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        ttl = max(0, int(getattr(self.settings, "ranking_cache_ttl", 300)))
        if ttl <= 0 or time.monotonic() - entry.stored_at > ttl:
            self._cache.pop(key, None)
            return None
        return entry.page

    def _cache_put(
        self, key: tuple[str, str, int, int, int], page: RankingPage
    ) -> None:
        ttl = max(0, int(getattr(self.settings, "ranking_cache_ttl", 300)))
        if ttl <= 0:
            return
        if len(self._cache) >= RANKING_CACHE_MAX_ENTRIES:
            oldest = min(self._cache, key=lambda item: self._cache[item].stored_at)
            self._cache.pop(oldest, None)
        self._cache[key] = _CacheEntry(stored_at=time.monotonic(), page=page)

    # ---- 编排 ----
    async def run(
        self,
        session_id: str,
        channel: BaseChannel | None,
        board: str,
        page: int = 1,
        store: bool = True,
    ) -> RankingResult:
        """取一页榜单并渲染成成品；``channel`` 为 None 时聚合所有渠道。

        ``store=False`` 是翻页前的「探一探」：照样从会话的本地缓冲里取数（缓冲可以顺手
        多拉一块回来，下次真翻页就不用再等），但不推进「当前页」的位置。
        """
        page = max(1, int(page))
        if channel is not None:
            limit = self._fetch_limit(False)
            outcome = await self.ranking_one(
                channel,
                board,
                page=page,
                limit=limit,
                session_id=session_id,
                dry_run=not store,
            )
            outcomes = [outcome]
            aggregate = False
        else:
            limit = self._fetch_limit(True)
            outcomes = await self.ranking_all(
                board,
                page=page,
                limit=limit,
                session_id=session_id,
                dry_run=not store,
            )
            aggregate = True
        return self._build_result(
            session_id if store else "", board, page, outcomes, aggregate
        )

    async def turn_page(self, session_id: str, step: int) -> RankingTurn:
        """翻上一页/下一页；返回成品或边界提示。"""
        session = self._sessions.get(session_id)
        if session is None or not self.session_active(session_id):
            return RankingTurn()
        target = session.page + step
        if target < 1:
            return RankingTurn(message="📄 已经是第一页了")
        channel = (
            None
            if session.aggregate
            else self.channels.get(session.order[0] if session.order else "")
        )
        if not session.aggregate and channel is None:
            return RankingTurn()
        if step > 0:
            # 先探一下有没有下一页：上游这页是空的就没得翻
            probe = await self._probe_next(session_id, session, target)
            if not probe:
                return RankingTurn(
                    message=f"📄 没有更多名次了（第 {session.page} 页已是最后一页）"
                )
        result = await self.run(session_id, channel, session.board, page=target)
        return RankingTurn(result=result)

    async def _probe_next(
        self, session_id: str, session: _RankingSession, target: int
    ) -> bool:
        """下一页是否还有内容：取一次上游，有结果就算能翻（结果本身会被缓存复用）。"""
        result = await self.run(
            session_id,
            None
            if session.aggregate
            else self.channels.get(session.order[0] if session.order else ""),
            session.board,
            page=target,
            store=False,  # 探测不写会话，避免把「当前页」状态覆盖成下一页
        )
        return result.count > 0

    # ---- 序号 ----
    def resolve_index(
        self, session_id: str, token: str
    ) -> tuple[BaseChannel, str] | None:
        """把序号还原成榜单里的（渠道, 规范 ID）；过期/越界/渠道停用时返回 None。"""
        if not session_id or not token.isdigit():
            return None
        index = int(token)
        if index < 1 or not self.session_active(session_id):
            return None
        session = self._sessions[session_id]
        local = index - session.start_index + 1
        if local < 1 or local > len(session.entries):
            return None
        channel_name, comic_id = session.entries[local - 1]
        channel = self.channels.get(channel_name)
        if channel is None or not channel.enabled:
            return None
        return channel, comic_id

    def index_hint(
        self, session_id: str, token: str, command: str = "comic"
    ) -> str | None:
        """纯数字没能当成序号时给一句人话解释；不属于「序号语境」时返回 None。"""
        if not session_id or not token.isdigit():
            return None
        index = int(token)
        if not 1 <= index <= RANKING_INDEX_HINT_MAX:
            return None
        if self.session_active(session_id):
            session = self._sessions[session_id]
            last = session.start_index + len(session.entries) - 1
            if session.start_index <= index <= last:
                return None
            return (
                f"❌ 这一页的序号是 {session.start_index}–{last}："
                "翻页看后面的名次，或按作品 ID 直接下载"
            )
        if session_id in self._sessions:
            self._sessions.pop(session_id, None)
            minutes = max(1, round(RANKING_RESULT_TTL / 60))
            return (
                f"❌ 榜单已过期（{minutes} 分钟内有效）："
                f"重新发 /{command} 排行 再看一次"
            )
        return None

    # ---- 渲染 ----
    def _build_result(
        self,
        session_id: str,
        board: str,
        page: int,
        outcomes: list[RankingOutcome],
        aggregate: bool,
    ) -> RankingResult:
        pages: list[tuple[BaseChannel, RankingPage]] = []
        entries: list[tuple[str, str]] = []
        order: list[str] = []
        for outcome in outcomes:
            channel_page = outcome.page
            if channel_page is None or channel_page.empty:
                continue
            pages.append((outcome.channel, channel_page))
            order.append(outcome.channel.name)
            entries.extend(
                (outcome.channel.name, str(item.comic_id))
                for item in channel_page.items
            )

        elapsed = max((outcome.elapsed for outcome in outcomes), default=0.0)
        shown = {channel.name for channel, _ in pages} if aggregate else set()
        extra = "\n".join(
            part
            for part in (
                self._failure_lines(outcomes),
                self._note_lines(outcomes, skip=shown),
            )
            if part
        )
        # 超长时按「每个渠道都留几条」来缩，而不是把最后几个渠道整段砍掉：
        # 砍尾会让用户以为后几家没上榜，缩成「每渠道前 2 名」至少每家都在
        pages, per_channel = self._fit_pages(
            pages,
            board=board,
            page=page,
            aggregate=aggregate,
            elapsed=elapsed,
            extra=extra,
            start_index=self._page_start(session_id, page),
        )
        if per_channel is not None:
            entries = [
                (channel.name, str(item.comic_id))
                for channel, channel_page in pages
                for item in channel_page.items
            ]
            order = [channel.name for channel, _ in pages]
        # 先按原样渲染（不截断）→ 超长时缩条数 → 最后才用 _bounded 兜底
        start_index = self._page_start(session_id, page)
        text = self._bounded(
            self._render_text(
                board=board,
                page=page,
                aggregate=aggregate,
                pages=pages,
                elapsed=elapsed,
                extra=extra,
                per_channel=per_channel,
                start_index=start_index,
            )
        )
        result = RankingResult(
            board=board,
            page=page,
            text=text,
            aggregate=aggregate,
            count=len(entries),
            elapsed=elapsed,
            start_index=start_index,
            pages=pages,
            warnings=extra,
        )
        if session_id and pages:
            self._store_session(
                session_id,
                board,
                page,
                aggregate,
                order,
                entries,
                start_index,
                # 每个渠道的缓冲都要交回会话（哪怕这一页它没内容）：
                # 漏一个下次就会从第 1 页重新读，翻页会重复
                streams={
                    outcome.channel.name: outcome.stream
                    for outcome in outcomes
                    if outcome.stream is not None
                },
            )
        return result

    def _fit_pages(
        self,
        pages: list[tuple[BaseChannel, RankingPage]],
        *,
        board: str,
        page: int,
        aggregate: bool,
        elapsed: float,
        extra: str,
        start_index: int = 1,
    ) -> tuple[list[tuple[BaseChannel, RankingPage]], int | None]:
        """把一页结果缩到单条消息放得下；返回（缩过的页, 每渠道保留几条或 None）。

        只缩聚合结果：单渠道一页就是配置的条数，本来就放得下。缩的时候整渠道一起缩，
        避免「前几家全显示、后几家一条不剩」——那种截断看起来像平台漏消息。
        """
        if not aggregate or len(pages) <= 1:
            return pages, None

        widest = max(len(channel_page.items) for _, channel_page in pages)
        for per in range(widest - 1, 0, -1):
            trial = [
                (
                    channel,
                    RankingPage(
                        channel=channel_page.channel,
                        board=channel_page.board,
                        items=channel_page.items[:per],
                        total=channel_page.total,
                        has_more=channel_page.has_more,
                        note=channel_page.note,
                    ),
                )
                for channel, channel_page in pages
            ]
            trial_text = self._render_text(
                board=board,
                page=page,
                aggregate=aggregate,
                pages=trial,
                elapsed=elapsed,
                extra=extra,
                per_channel=per,
                start_index=start_index,
            )
            if len(trial_text) <= RANKING_MAX_TEXT_CHARS:
                return trial, per
        return pages, None

    def _page_start(self, session_id: str, page: int) -> int:
        """第 ``page`` 页第一条的序号：前面各页展示条数之和 + 1（与搜索一致）。"""
        session = self._sessions.get(session_id) if session_id else None
        if session is None:
            return 1
        return 1 + sum(count for no, count in session.counts.items() if no < page)

    def _store_session(
        self,
        session_id: str,
        board: str,
        page: int,
        aggregate: bool,
        order: list[str],
        entries: list[tuple[str, str]],
        start_index: int = 1,
        streams: dict[str, "_RankStream"] | None = None,
    ) -> None:
        if len(self._sessions) > RANKING_RESULT_MAX_SESSIONS:
            now = time.monotonic()
            for key, value in list(self._sessions.items()):
                if now - value.stored_at > RANKING_RESULT_TTL:
                    self._sessions.pop(key, None)
            overflow = len(self._sessions) - RANKING_RESULT_MAX_SESSIONS
            if overflow > 0:
                oldest = sorted(
                    self._sessions, key=lambda key: self._sessions[key].stored_at
                )[:overflow]
                for key in oldest:
                    self._sessions.pop(key, None)
        previous = self._sessions.get(session_id)
        same_board = previous is not None and previous.board == board
        counts = dict(previous.counts) if same_board and previous is not None else {}
        counts[page] = len(entries)
        # 各渠道的本地缓冲只在同一档位内沿用：换档位等于重新看一场榜
        buffers: dict[str, _RankStream] = {}
        if same_board and previous is not None:
            buffers = dict(previous.streams)
        buffers.update(
            {
                name: stream
                for name, stream in (streams or {}).items()
                if stream is not None
            }
        )
        self._sessions[session_id] = _RankingSession(
            stored_at=time.monotonic(),
            board=board,
            aggregate=aggregate,
            order=order,
            page=page,
            entries=entries,
            start_index=start_index,
            counts=counts,
            streams=buffers,
        )

    def _render_text(
        self,
        *,
        board: str,
        page: int,
        aggregate: bool,
        pages: list[tuple[BaseChannel, RankingPage]],
        elapsed: float,
        extra: str,
        per_channel: int | None = None,
        start_index: int = 1,
    ) -> str:
        title = (
            f"🏆 全渠道{board_label(board)}"
            if aggregate
            else f"🏆 {pages[0][0].display_name if pages else ''}{board_label(board)}"
        )
        if page > 1:
            title += f"（第 {page} 页）"

        # 条目行与「渠道小标题」分开收集：整条消息超长时要先丢条目、保住脚注与下载提示，
        # 否则用户拿到的是一段被截断的榜单——连怎么下载、能不能翻页都被截掉了
        body: list[str] = []
        total = 0
        for channel, channel_page in pages:
            if aggregate:
                body.append(f"【{channel.display_name}】")
            for item in channel_page.items:
                total += 1
                body.append(self._format_item(start_index + total - 1, item))
            if aggregate and channel_page.note:
                body.append(f"ℹ️ {channel_page.note}")
            if aggregate:
                body.append("")

        footer = f"第 {page} 页"
        if elapsed and page == 1:
            footer += f" · 用时 {elapsed:.1f}s"
        tail: list[str] = []
        if extra:
            tail.extend([extra, ""])
        tail.append(footer)
        if total:
            # 与搜索同一句式：序号要带命令发，插件不监听裸数字
            command = self._nav_command(aggregate, pages)
            tail.append(
                f"下载：{command} {start_index}（把 {start_index} 换成上面的序号）"
            )
        else:
            tail.append("这次没有拿到榜单内容，换个时间窗或稍后再试")
        nav = self._nav_hint(page, any(channel_page.has_more for _, channel_page in pages))
        if nav:
            tail.append(nav)

        head = [title, ""]
        if per_channel is not None:
            body.append(
                f"（受单条消息长度限制，每家只列前 {per_channel} 名；"
                "想看完整名次就只查一个渠道）"
            )
            body.append("")
        lines = head + body + tail
        return "\n".join(lines)

    @staticmethod
    def _nav_command(
        aggregate: bool, pages: list[tuple[BaseChannel, RankingPage]]
    ) -> str:
        """下载提示里用哪个命令：聚合看 `/comic`，单渠道用它自己的命令。"""
        if not aggregate and pages:
            return f"/{pages[0][0].display_command}"
        return "/comic"

    def _nav_hint(self, page: int, has_more: bool) -> str:
        """翻页提示：没有下一页时不提「下一页」，免得用户白翻一次。"""
        parts: list[str] = []
        if page > 1:
            parts.append("上一页：/comic 上一页")
        if has_more:
            parts.append("下一页：/comic 下一页")
        if parts:
            return "　".join(parts)
        return "最后一页了：想看别的档位可以换时间窗" if page > 1 else ""

    def _format_item(self, index: int, item: RankingItem) -> str:
        raw_title = item.title or item.display_title
        title = self._clip(raw_title, _TITLE_LIMIT) or f"{item.channel} 作品"
        meta_parts: list[str] = []
        author = self._clip(item.author, _AUTHOR_LIMIT)
        if author:
            meta_parts.append(author)
        if item.page_count > 0:
            meta_parts.append(f"{item.page_count}P")
        meta = f"（{' · '.join(meta_parts)}）" if meta_parts else ""
        mark = f"　{AI_MARK_BADGE}" if has_ai_mark(raw_title) else ""
        return f"{index}. {title}{meta}　ID：{item.comic_id}{mark}"

    @staticmethod
    def _note_lines(outcomes: list[RankingOutcome], skip: set[str] | None = None) -> str:
        """把各渠道的降级说明按文案分组，避免同一句话重复四遍。"""
        grouped: dict[str, list[str]] = {}
        for outcome in outcomes:
            page = outcome.page
            if page is None or not page.note:
                continue
            if skip and outcome.channel.name in skip:
                continue
            grouped.setdefault(page.note, []).append(outcome.channel.display_name)
        return "\n".join(
            f"ℹ️ {'、'.join(names)}：{note}" for note, names in grouped.items()
        )

    @staticmethod
    def _failure_lines(outcomes: list[RankingOutcome]) -> str:
        failures = [outcome for outcome in outcomes if outcome.error]
        if not failures:
            return ""
        return "\n".join(
            (
                f"⚠️ {outcome.channel.display_name} 榜单超时：{outcome.error}"
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
        if len(text) <= RANKING_MAX_TEXT_CHARS:
            return text
        return text[: RANKING_MAX_TEXT_CHARS - 20].rstrip() + "\n…（榜单过长，已截断）"

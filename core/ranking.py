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
from .channel import BaseChannel, search_proxy_override

_WHITESPACE = re.compile(r"\s+")
# 标题/作者的展示上限：榜单里的标题常带整段宣传语，原样发出去会刷屏
_TITLE_LIMIT = 40
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
    """一个渠道的榜单结果：成功、空榜或失败都收在这一层，聚合层不抛异常。"""

    channel: BaseChannel
    page: RankingPage | None = None
    error: str = ""
    timed_out: bool = False
    elapsed: float = 0.0


@dataclass(slots=True)
class RankingResult:
    """一次榜单请求的成品：渲染好的文案 + 这次翻到第几页。"""

    board: str
    page: int
    text: str
    aggregate: bool
    count: int = 0  # 这一页实际拿到了多少条；0 表示这页是空的
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

    ``entries`` 只存**当前这一页**的条目，编号从 1 开始：榜单名次是页内概念，
    跨页累计编号反而会让用户按序号下载到与画面对不上的一本。
    """

    stored_at: float
    board: str
    aggregate: bool
    order: list[str]  # 渠道名，决定展示顺序
    page: int = 1
    entries: list[tuple[str, str]] = field(default_factory=list)  # (渠道名, 作品 ID)


class RankingService:
    """榜单编排：超时、缓存、冷却、翻页、序号记忆与文本渲染。"""

    def __init__(
        self,
        channels: dict[str, BaseChannel],
        settings: PluginSettings,
    ) -> None:
        self.channels = channels
        self.settings = settings
        self._cache: dict[tuple[str, str, int, int], _CacheEntry] = {}
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

    async def ranking_one(
        self,
        channel: BaseChannel,
        board: str,
        page: int = 1,
        limit: int = 10,
    ) -> RankingOutcome:
        """取一个渠道的榜单；超时/失败都收成 outcome，绝不向聚合层抛异常。"""
        started = time.monotonic()
        page = max(1, int(page))
        if not channel.supports_ranking:
            return RankingOutcome(
                channel=channel,
                error=f"{channel.display_name} 暂不支持排行榜",
                elapsed=time.monotonic() - started,
            )
        if not self.supports_board(channel, board):
            return RankingOutcome(
                channel=channel,
                page=RankingPage(
                    channel=channel.name,
                    board=board,
                    # 说明只写「缺哪一档」：渠道名由渲染层按渠道分组统一加，拼在这里会重复
                    note=f"没有{board_label(board)}这一档，已跳过",
                ),
                elapsed=time.monotonic() - started,
            )

        cache_key = (channel.name, board, page, limit)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return RankingOutcome(
                channel=channel, page=cached, elapsed=time.monotonic() - started
            )

        timeout = max(1, int(getattr(self.settings, "ranking_timeout", 15)))
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
                    channel.ranking(board, page=page, limit=limit), timeout=budget
                )

        try:
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
        except asyncio.TimeoutError:
            return RankingOutcome(
                channel=channel,
                error=f"超时（{timeout} 秒）",
                timed_out=True,
                elapsed=time.monotonic() - started,
            )
        except PluginError as exc:
            return RankingOutcome(
                channel=channel,
                error=str(exc),
                elapsed=time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - 渠道实现里的意外异常不该拖垮整条命令
            logger.warning(
                f"[{PLUGIN_NAME}] {channel.display_name} 榜单出现未预期异常: {exc}"
            )
            return RankingOutcome(
                channel=channel,
                error=f"未预期错误：{exc}",
                elapsed=time.monotonic() - started,
            )

        if result is not None and not result.empty:
            self._cache_put(cache_key, result)
        return RankingOutcome(
            channel=channel, page=result, elapsed=time.monotonic() - started
        )

    async def ranking_all(
        self, board: str, page: int = 1, limit: int = 5
    ) -> list[RankingOutcome]:
        """并发取所有支持榜单的渠道；彼此独立，一个失败不影响其它。"""
        targets = self.rankable()
        if not targets:
            return []
        return list(
            await asyncio.gather(
                *(
                    self.ranking_one(channel, board, page=page, limit=limit)
                    for channel in targets
                )
            )
        )

    # ---- 缓存 ----
    def _cache_get(self, key: tuple[str, str, int, int]) -> RankingPage | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        ttl = max(0, int(getattr(self.settings, "ranking_cache_ttl", 300)))
        if ttl <= 0 or time.monotonic() - entry.stored_at > ttl:
            self._cache.pop(key, None)
            return None
        return entry.page

    def _cache_put(self, key: tuple[str, str, int, int], page: RankingPage) -> None:
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
    ) -> RankingResult:
        """取一页榜单并渲染成成品；``channel`` 为 None 时聚合所有渠道。"""
        page = max(1, int(page))
        if channel is not None:
            limit = self._fetch_limit(False)
            outcome = await self.ranking_one(channel, board, page=page, limit=limit)
            outcomes = [outcome]
            aggregate = False
        else:
            limit = self._fetch_limit(True)
            outcomes = await self.ranking_all(board, page=page, limit=limit)
            aggregate = True
        return self._build_result(session_id, board, page, outcomes, aggregate)

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
            probe = await self._probe_next(session, target)
            if not probe:
                return RankingTurn(
                    message=f"📄 没有更多名次了（第 {session.page} 页已是最后一页）"
                )
        result = await self.run(session_id, channel, session.board, page=target)
        return RankingTurn(result=result)

    async def _probe_next(self, session: _RankingSession, target: int) -> bool:
        """下一页是否还有内容：取一次上游，有结果就算能翻（结果本身会被缓存复用）。"""
        result = await self.run(
            "",  # 探测不写会话，避免把「当前页」状态覆盖成下一页
            None
            if session.aggregate
            else self.channels.get(session.order[0] if session.order else ""),
            session.board,
            page=target,
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
        if index > len(session.entries):
            return None
        channel_name, comic_id = session.entries[index - 1]
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
            if index <= len(session.entries):
                return None
            return (
                f"❌ 这次的榜单只到第 {len(session.entries)} 名："
                f"发「/comic 下一页」看后面的名次"
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
        extra = "\n".join(
            part
            for part in (
                self._failure_lines(outcomes),
                self._note_lines(outcomes),
            )
            if part
        )
        text = self._render_text(
            board=board,
            page=page,
            aggregate=aggregate,
            pages=pages,
            elapsed=elapsed,
            extra=extra,
        )
        result = RankingResult(
            board=board,
            page=page,
            text=text,
            aggregate=aggregate,
            count=len(entries),
            elapsed=elapsed,
            pages=pages,
            warnings=extra,
        )
        if session_id and pages:
            self._store_session(session_id, board, page, aggregate, order, entries)
        return result

    def _store_session(
        self,
        session_id: str,
        board: str,
        page: int,
        aggregate: bool,
        order: list[str],
        entries: list[tuple[str, str]],
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
        self._sessions[session_id] = _RankingSession(
            stored_at=time.monotonic(),
            board=board,
            aggregate=aggregate,
            order=order,
            page=page,
            entries=entries,
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
    ) -> str:
        title = (
            f"🏆 全渠道{board_label(board)}"
            if aggregate
            else f"🏆 {pages[0][0].display_name if pages else ''}{board_label(board)}"
        )
        if page > 1:
            title += f"（第 {page} 页）"
        lines = [title, ""]

        index = 0
        for channel, channel_page in pages:
            if aggregate:
                lines.append(f"【{channel.display_name}】")
            for item in channel_page.items:
                index += 1
                lines.append(self._format_item(index, item))
            if aggregate:
                lines.append("")

        if extra:
            lines.append(extra)

        lines.append("")
        footer = f"第 {page} 页"
        if elapsed and page == 1:
            footer += f" · 用时 {elapsed:.1f}s"
        lines.append(footer)
        if index:
            lines.append("下载：直接回序号（如 3），或用对应渠道命令贴 ID")
        else:
            lines.append("这次没有拿到榜单内容，换个时间窗或稍后再试")
        nav = self._nav_hint(page)
        if nav:
            lines.append(nav)
        return self._bounded("\n".join(lines))

    def _nav_hint(self, page: int) -> str:
        if page > 1:
            return "上一页：/comic 上一页　下一页：/comic 下一页"
        return "下一页：/comic 下一页"

    def _format_item(self, index: int, item: RankingItem) -> str:
        title = self._clip(item.title, _TITLE_LIMIT) or item.display_title
        meta_parts: list[str] = []
        author = self._clip(item.author, _AUTHOR_LIMIT)
        if author:
            meta_parts.append(author)
        if item.page_count > 0:
            meta_parts.append(f"{item.page_count}P")
        meta = f"（{' · '.join(meta_parts)}）" if meta_parts else ""
        return f"{index}. {title}{meta}　ID：{item.comic_id}"

    @staticmethod
    def _note_lines(outcomes: list[RankingOutcome]) -> str:
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

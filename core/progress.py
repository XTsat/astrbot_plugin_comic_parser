"""跨渠道的进度上报：统一的状态快照 + 独立心跳。

各渠道的下载形态不同（有的跑在事件循环里，有的像 JM 一样先丢进线程池），但它们能提供的
事实是同一组：完成了多少、一共多少、已下多少字节、已用多久。这里把这组事实收成
`ProgressState`，再给出与「下完一个单位」无关的定时心跳——渠道只在自己方便的时候上报，
慢网络下一章能拖好几分钟，心跳负责把这段空白补上。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from astrbot.api import logger

from ..domain.constants import DOWNLOAD_LOG_INTERVAL, PLUGIN_NAME
from ..utils import format_size

# 读取当前进度快照：由渠道实现，必须无副作用，可被事件循环在任何时刻调用
SnapshotFn = Callable[[], "ProgressState"]


@dataclass(slots=True)
class ProgressState:
    """一次下载在某一刻的进度快照。"""

    comic_id: str
    unit: str = "图片"
    done: int = 0
    total: int = 0
    # 整本一共多少张图，0 表示渠道没给。多章节本的主计数是章节，`total` 就不是张数，
    # 需要按「张」说话的地方（暂停/取消提示）只能用它
    image_total: int = 0
    # 第二个计数的人话描述（如 JM 多章节本的「12/50 页」），与主计数一起展示
    detail: str = ""
    bytes_downloaded: int = 0
    elapsed: float = 0.0
    failed: int = 0

    @property
    def counter(self) -> str:
        if self.total:
            return f"{self.done}/{self.total} {self.unit}"
        return f"{self.done} {self.unit}"

    @property
    def headline(self) -> str:
        """主计数 + 次计数：一眼看清「做到第几章、这一本到第几页」。"""
        return f"{self.counter} · {self.detail}" if self.detail else self.counter

    @property
    def percent(self) -> int | None:
        """按主计数算的百分比；总数未知时返回 None（调用方只用绝对计数）。"""
        if self.total <= 0:
            return None
        return min(100, int(self.done * 100 / self.total))

    def log_line(self) -> str:
        """进度日志的完整一行：主计数 + 次计数 + 体积 + 速率。"""
        elapsed = max(self.elapsed, 0.001)
        failed = f"，失败 {self.failed}" if self.failed else ""
        # 少数渠道（如哔咔）在下载器内部才知道作品 ID，此时只写进度，不留空位
        label = f"{self.comic_id} " if self.comic_id else ""
        return (
            f"下载进度 {label}{self.headline}"
            f" · {format_size(self.bytes_downloaded)}"
            f"（已用 {self.elapsed:.0f}s，均 {self.done / elapsed * 60:.1f} {self.unit}/分、"
            f"{format_size(int(self.bytes_downloaded / elapsed * 60))}/分{failed}）"
        )


class ProgressHeartbeat:
    """定时汇报进度：不依赖下载器是否刚好下完一个单位。

    每 ``interval`` 秒取一次快照，写一条进度日志，并把同一份快照转给命令层
    （聊天进度的节流由命令层负责，这里不做判断）。心跳自身的失败只记 debug：
    它只是让人看见进度，坏掉不该影响下载本身。
    """

    def __init__(
        self,
        snapshot: SnapshotFn,
        *,
        on_tick: Callable[[ProgressState], Awaitable[None]] | None = None,
        interval: float = DOWNLOAD_LOG_INTERVAL,
    ) -> None:
        self._snapshot = snapshot
        self._on_tick = on_tick
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="comic-progress-heartbeat"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def __aenter__(self) -> ProgressHeartbeat:
        self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                state = self._snapshot()
            except Exception as exc:  # 取快照只是汇报，坏掉不能影响下载
                logger.debug(f"[{PLUGIN_NAME}] 读取进度快照失败（忽略）: {exc}")
                continue
            logger.info(f"[{PLUGIN_NAME}] {state.log_line()}")
            if self._on_tick is None:
                continue
            try:
                await self._on_tick(state)
            except Exception as exc:  # 进度只是锦上添花，不能拖垮下载
                logger.debug(f"[{PLUGIN_NAME}] 心跳进度上报失败（忽略）: {exc}")


async def emit(
    on_progress: Callable[[ProgressState], Awaitable[None]] | None,
    state: ProgressState,
) -> None:
    """把一份快照转给命令层。

    下载器只管在合适的时机（下完一张图/一章）推一次；上报失败只记 debug，
    进度本来就是锦上添花，不能因此拖垮下载。
    """
    if on_progress is None:
        return
    try:
        await on_progress(state)
    except Exception as exc:  # 进度只是锦上添花，不能拖垮下载
        logger.debug(f"[{PLUGIN_NAME}] 进度上报失败（忽略）: {exc}")

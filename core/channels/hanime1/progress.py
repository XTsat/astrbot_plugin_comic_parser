"""下载会话状态与进度日志。

Hanime1 渠道全程跑在事件循环里（抓页在线程池、下载是 aiohttp），所以这里用一个普通
标记位就够：插件重载时 terminate() 在同一个循环上置位，下一次落图前的检查就能收工。

进度日志由共享心跳（`core/progress.py`）按固定间隔产出——不依赖「下完一张图」才写，
所以慢网络下也不会出现几分钟一条日志都没有的空白；这里只提供快照与「是否该停手」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ....utils import format_size
from ...channel import CancelToken
from ...progress import ProgressState


@dataclass(slots=True)
class DownloadSession:
    """一次下载的实时状态：进度快照、暂停/重载中断判定与中断汇报共用。"""

    comic_id: str
    total: int = 0
    unit: str = "张"
    done: int = 0
    bytes_downloaded: int = 0
    # 插件重载时 terminate() 置位；cancel 是本次任务自己的取消信号（到点暂停/放弃）
    stop: bool = False
    cancel: CancelToken | None = None
    started: float = field(default_factory=time.monotonic)

    @property
    def stopped(self) -> bool:
        """该收工了吗：插件重载与「本次任务到点/放弃」任一成立都要停。"""
        return self.stop or (self.cancel is not None and self.cancel.cancelled)

    def elapsed(self) -> float:
        return max(time.monotonic() - self.started, 0.001)

    def state(self) -> ProgressState:
        return ProgressState(
            comic_id=self.comic_id,
            unit=self.unit,
            done=self.done,
            total=self.total,
            bytes_downloaded=self.bytes_downloaded,
            elapsed=self.elapsed(),
        )

    def summary(self) -> str:
        return (
            f"{self.comic_id}：{self.state().counter} · "
            f"{format_size(self.bytes_downloaded)} · 已用 {self.elapsed():.0f}s"
        )


# 活跃下载登记表：插件卸载时用 cancel_active_downloads() 让下载尽快收工
_SESSIONS: dict[int, DownloadSession] = {}


def register_session(session: DownloadSession) -> None:
    _SESSIONS[id(session)] = session


def unregister_session(session: DownloadSession) -> None:
    _SESSIONS.pop(id(session), None)


def cancel_active_downloads() -> list[str]:
    """让正在跑的下载尽快停下，返回各自的实时状态（调用方负责写日志）。"""
    summaries: list[str] = []
    for session in list(_SESSIONS.values()):
        session.stop = True
        summaries.append(session.summary())
    return summaries

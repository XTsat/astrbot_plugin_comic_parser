"""下载会话状态与进度日志。

E-Hentai 的下载全程跑在事件循环里（aiohttp），所以这里用一个普通标记位就够：
插件重载时 terminate() 在同一个循环上置位，下一次发请求前检查就能收工。

进度日志由共享心跳（`core/progress.py`）按固定间隔产出，这里只提供快照与「是否该停手」。
`degraded` 单独计数：配置了原图却回退到 1280px 的页数，结果摘要里要如实告诉用户。
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
    unit: str = "页"
    done: int = 0
    failed: int = 0
    # 请求了原图、实际回退成 1280px 重采样的页数
    degraded: int = 0
    # 断点里已存在、本次直接跳过的页数（续跑时用）
    skipped: int = 0
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
        detail = f"降级 {self.degraded} 页" if self.degraded else ""
        return ProgressState(
            comic_id=self.comic_id,
            unit=self.unit,
            done=self.done,
            total=self.total,
            detail=detail,
            bytes_downloaded=self.bytes_downloaded,
            elapsed=self.elapsed(),
            failed=self.failed,
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

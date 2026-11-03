"""下载会话状态与进度日志。

nhentai 渠道全程跑在事件循环里（抓页在线程池、下载是 aiohttp），所以这里用一个普通标记位
就够：插件重载时 terminate() 在同一个循环上置位，下一次落图前的检查就能收工。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from astrbot.api import logger

from ....domain.constants import DOWNLOAD_LOG_INTERVAL, PLUGIN_NAME


def format_size(num_bytes: int) -> str:
    """体积展示：不足 1 MB 用 KB，避免小本子一律显示 0.0 MB。"""
    if num_bytes < 1048576:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / 1048576:.1f} MB"


@dataclass(slots=True)
class DownloadSession:
    """一次下载的实时状态：定时进度日志与「重载中断」时都靠它汇报。"""

    comic_id: str
    total: int = 0
    unit: str = "图片"
    done: int = 0
    bytes_downloaded: int = 0
    stop: bool = False
    started: float = field(default_factory=time.monotonic)
    last_log_at: float = 0.0  # 0 表示还没打过进度日志

    def elapsed(self) -> float:
        return max(time.monotonic() - self.started, 0.001)

    def summary(self) -> str:
        total = f"/{self.total}" if self.total else ""
        return (
            f"{self.comic_id}：{self.done}{total} {self.unit} · "
            f"{format_size(self.bytes_downloaded)} · 已用 {self.elapsed():.0f}s"
        )

    def log_progress(self) -> None:
        """按固定间隔写一条进度日志：张数 + 已下载体积 + 速度，慢下载一眼能看出来。"""
        now = time.monotonic()
        if now - self.last_log_at < DOWNLOAD_LOG_INTERVAL:
            return
        self.last_log_at = now
        elapsed = self.elapsed()
        total = f"/{self.total}" if self.total else ""
        logger.info(
            f"[{PLUGIN_NAME}] 下载进度 {self.comic_id} {self.done}{total} {self.unit} · "
            f"{format_size(self.bytes_downloaded)}（已用 {elapsed:.0f}s，"
            f"均 {self.done / elapsed * 60:.1f} {self.unit}/分）"
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

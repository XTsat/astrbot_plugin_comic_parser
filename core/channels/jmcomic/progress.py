"""下载会话状态与进度上报。

jmcomic 的下载回调是同步的、且跑在线程池里，所以进度先落在本模块的会话对象上，
再由回调桥接回事件循环；插件重载时也靠同一批会话让线程尽快收工。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ....domain.constants import DOWNLOAD_LOG_INTERVAL, PLUGIN_NAME

# jmcomic 的下载器子类只能在其可用后创建，这里按库对象缓存，避免重复定义
_PROGRESS_DOWNLOADER_CACHE: dict[str, Any] = {}


def format_size(num_bytes: int) -> str:
    """体积展示：不足 1 MB 用 KB，避免小本子一律显示 0.0 MB。"""
    if num_bytes < 1048576:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / 1048576:.1f} MB"


@dataclass(slots=True)
class DownloadSession:
    """一次下载的实时状态：定时进度日志与「重载中断」时都靠它汇报。"""

    album_id: str
    started: float = field(default_factory=time.monotonic)
    unit: str = "图片"
    done: int = 0
    total: int = 0
    bytes_downloaded: int = 0
    stop: threading.Event = field(default_factory=threading.Event)

    def summary(self) -> str:
        elapsed = max(time.monotonic() - self.started, 0.001)
        total = f"/{self.total}" if self.total else ""
        return (
            f"{self.album_id}：{self.done}{total} {self.unit}"
            f" · {format_size(self.bytes_downloaded)} · 已用 {elapsed:.0f}s"
        )


# 活跃下载登记表：插件卸载时用 cancel_active_downloads() 让线程尽快收工
_SESSIONS: dict[int, DownloadSession] = {}
_SESSIONS_LOCK = threading.Lock()


def register_session(session: DownloadSession) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS[id(session)] = session


def unregister_session(session: DownloadSession) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.pop(id(session), None)


def cancel_active_downloads() -> list[str]:
    """让正在跑的下载尽快停下，返回各自的实时状态（调用方负责写日志）。

    下载线程跑在线程池里，插件卸载并不会让它停；结果也无处投递，继续跑只是白占带宽
    与磁盘，所以重载时主动取消，并由任务自己清理临时目录。
    """
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS.values())
    summaries: list[str] = []
    for session in sessions:
        session.stop.set()
        summaries.append(session.summary())
    return summaries


def progress_downloader_class(jmcomic: Any) -> type:
    """构造带进度上报的下载器子类。

    jmcomic 的回调是同步的（跑在线程里），上报时通过 run_coroutine_threadsafe
    回到事件循环，命令层才能安全地发消息。
    """
    cached = _PROGRESS_DOWNLOADER_CACHE.get("cls")
    if cached is not None:
        return cached

    class _ProgressDownloader(jmcomic.JmDownloader):
        def __init__(self, option: Any, report: Any, session: DownloadSession) -> None:
            super().__init__(option)
            self._report = report
            self._session = session
            self._last_log_at = session.started

        def _check_stop(self) -> None:
            if self._session.stop.is_set():
                raise jmcomic.DownloadCancelledException("插件重载，取消下载")

        def before_album(self, album: Any) -> None:
            self._check_stop()
            super().before_album(album)
            self._session.started = time.monotonic()
            self._last_log_at = self._session.started
            # 多章节本子按「章节」计进度更贴近用户直觉；单章本子按图片计
            try:
                chapter_count = len(album)
            except Exception:  # 上游对象异常不应影响下载
                chapter_count = 0
            if chapter_count > 1:
                self._session.unit = "章节"
                self._session.total = chapter_count
            logger.info(
                f"[{PLUGIN_NAME}] 开始下载 {getattr(album, 'id', '?')}："
                f"{chapter_count} 章节 / {getattr(album, 'page_count', 0)} 页"
            )

        def before_photo(self, photo: Any) -> None:
            self._check_stop()
            super().before_photo(photo)
            if self._session.unit != "图片" or self._session.total:
                return
            try:
                self._session.total = len(photo)
            except Exception:
                self._session.total = 0

        def after_photo(self, photo: Any) -> None:
            super().after_photo(photo)
            if self._session.unit == "章节":
                self._session.done += 1
                self._emit()

        def before_image(self, image: Any, img_save_path: Any) -> None:
            self._check_stop()
            super().before_image(image, img_save_path)

        def after_image(self, image: Any, img_save_path: Any) -> None:
            self._check_stop()
            super().after_image(image, img_save_path)
            try:
                self._session.bytes_downloaded += (
                    Path(str(img_save_path)).stat().st_size
                )
            except OSError:  # 文件统计失败不影响下载本身
                pass
            if self._session.unit == "图片":
                self._session.done += 1
                self._emit()

        def _emit(self) -> None:
            self._log_progress()
            try:
                self._report(
                    self._session.done, self._session.total, self._session.unit
                )
            except Exception as exc:  # 进度只是锦上添花，不能拖垮下载
                logger.debug(f"[{PLUGIN_NAME}] 进度上报失败（忽略）: {exc}")

        def _log_progress(self) -> None:
            """按固定间隔写一条进度日志：张数 + 已下载 MB + 速度，慢下载一眼能看出来。"""
            session = self._session
            now = time.monotonic()
            if now - self._last_log_at < DOWNLOAD_LOG_INTERVAL:
                return
            self._last_log_at = now
            elapsed = max(now - session.started, 0.001)
            rate_size = format_size(int(session.bytes_downloaded / elapsed * 60))
            failed = len(self.download_failed_image) + len(self.download_failed_photo)
            total = f"/{session.total}" if session.total else ""
            logger.info(
                f"[{PLUGIN_NAME}] 下载进度 {session.album_id} "
                f"{session.done}{total} {session.unit} · {format_size(session.bytes_downloaded)}"
                f"（已用 {elapsed:.0f}s，均 {session.done / elapsed * 60:.1f} {session.unit}/分、"
                f"{rate_size}/分，失败 {failed}）"
            )

    _PROGRESS_DOWNLOADER_CACHE["cls"] = _ProgressDownloader
    return _ProgressDownloader

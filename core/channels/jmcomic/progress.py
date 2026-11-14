"""下载会话状态与进度上报。

jmcomic 的下载回调是同步的、且跑在线程池里，所以进度先落在本模块的会话对象上，
再由回调桥接回事件循环；插件重载与「到点暂停」也靠同一批会话让线程尽快收工。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ....domain.constants import PLUGIN_NAME
from ....utils import format_size
from ...progress import ProgressState

# jmcomic 的下载器子类只能在其可用后创建，这里按库对象缓存，避免重复定义
_PROGRESS_DOWNLOADER_CACHE: dict[str, Any] = {}


@dataclass(slots=True)
class DownloadSession:
    """一次下载的实时状态：章节与页两个计数都记，日志、聊天进度与中断汇报共用。"""

    album_id: str
    started: float = field(default_factory=time.monotonic)
    chapters_total: int = 0
    chapters_done: int = 0
    pages_total: int = 0
    pages_done: int = 0
    bytes_downloaded: int = 0
    failed: int = 0
    stop: threading.Event = field(default_factory=threading.Event)

    def state(self) -> ProgressState:
        """两个计数一起给：多章节本主计数是章节、次计数是页；单章本主计数就是页。"""
        elapsed = max(time.monotonic() - self.started, 0.001)
        if self.chapters_total > 1:
            return ProgressState(
                comic_id=self.album_id,
                unit="章",
                done=self.chapters_done,
                total=self.chapters_total,
                image_total=self.pages_total,
                detail=(
                    f"{self.pages_done}/{self.pages_total} 页"
                    if self.pages_total
                    else ""
                ),
                bytes_downloaded=self.bytes_downloaded,
                elapsed=elapsed,
                failed=self.failed,
            )
        return ProgressState(
            comic_id=self.album_id,
            unit="张",
            done=self.pages_done,
            total=self.pages_total,
            image_total=self.pages_total,
            bytes_downloaded=self.bytes_downloaded,
            elapsed=elapsed,
            failed=self.failed,
        )

    def summary(self) -> str:
        """中断（重载/到点暂停）时写进日志的一句话状态。"""
        elapsed = max(time.monotonic() - self.started, 0.001)
        return (
            f"{self.album_id}：{self.state().headline} · "
            f"{format_size(self.bytes_downloaded)} · 已用 {elapsed:.0f}s"
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
    回到事件循环，命令层才能安全地发消息。**日志不在回调里写**：进度日志由心跳按
    固定间隔产出，回调只把计数推到会话上，慢网络下才不会有几分钟的空白。
    """
    cached = _PROGRESS_DOWNLOADER_CACHE.get("cls")
    if cached is not None:
        return cached

    class _ProgressDownloader(jmcomic.JmDownloader):
        def __init__(self, option: Any, report: Any, session: DownloadSession) -> None:
            super().__init__(option)
            self._report = report
            self._session = session

        def _check_stop(self) -> None:
            if self._session.stop.is_set():
                raise jmcomic.DownloadCancelledException("插件重载或任务到点，取消下载")

        def before_album(self, album: Any) -> None:
            self._check_stop()
            super().before_album(album)
            self._session.started = time.monotonic()
            # 多章节本子按「章节」当主计数更贴近用户直觉，页数并行展示
            try:
                chapter_count = len(album)
            except Exception:  # 上游对象异常不应影响下载
                chapter_count = 0
            self._session.chapters_total = chapter_count
            self._session.pages_total = int(getattr(album, "page_count", 0) or 0)
            logger.info(
                f"[{PLUGIN_NAME}] 开始下载 {getattr(album, 'id', '?')}："
                f"{chapter_count} 章节 / {self._session.pages_total} 页"
            )

        def before_photo(self, photo: Any) -> None:
            self._check_stop()
            super().before_photo(photo)
            if self._session.pages_total:
                return
            # 个别本子上游没给总页数：退化成逐章累加，次计数仍然有意义
            try:
                self._session.pages_total += len(photo)
            except Exception:
                pass

        def after_photo(self, photo: Any) -> None:
            super().after_photo(photo)
            self._session.chapters_done += 1
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
            self._session.pages_done += 1
            self._session.failed = len(self.download_failed_image) + len(
                self.download_failed_photo
            )
            self._emit()

        def _emit(self) -> None:
            try:
                self._report(self._session.state())
            except Exception as exc:  # 进度只是锦上添花，不能拖垮下载
                logger.debug(f"[{PLUGIN_NAME}] 进度上报失败（忽略）: {exc}")

    _PROGRESS_DOWNLOADER_CACHE["cls"] = _ProgressDownloader
    return _ProgressDownloader

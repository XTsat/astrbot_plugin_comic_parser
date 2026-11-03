"""JM 漫画渠道。

只做「拿 ID → 取元信息 → 下载图片」，打包与加密完全交给上层，
所以这里看不到任何 zip/pdf/密码相关逻辑。

jmcomic 是可选依赖：插件启动时不 import，只有真正用到本渠道时才懒加载，
缺失时给出可执行的提示而不是崩溃。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from astrbot.api import logger

from ...domain import ChannelError, ComicMeta, DownloadError, DownloadedComic
from ...domain.constants import (
    DEFAULT_API_DOMAINS,
    DEFAULT_REQUEST_RETRY_TIMES,
    DEFAULT_HTML_DOMAINS,
    DEFAULT_REQUEST_TIMEOUT,
    DOWNLOAD_LOG_INTERVAL,
    IMAGE_SUFFIXES,
    PLUGIN_NAME,
)
from ..channel import BaseChannel, ProgressCallback, split_option_list

_ALBUM_IN_TEXT = re.compile(r"/(?:album|photo)/(\d+)")
_FIRST_DIGITS = re.compile(r"\d+")

def _format_size(num_bytes: int) -> str:
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
            f" · {_format_size(self.bytes_downloaded)} · 已用 {elapsed:.0f}s"
        )


# 活跃下载登记表：插件卸载时用 cancel_active_downloads() 让线程尽快收工
_SESSIONS: dict[int, DownloadSession] = {}
_SESSIONS_LOCK = threading.Lock()


def _register_session(session: DownloadSession) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS[id(session)] = session


def _unregister_session(session: DownloadSession) -> None:
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


# jmcomic 的下载器子类只能在其可用后创建，这里按库对象缓存，避免重复定义
_PROGRESS_DOWNLOADER_CACHE: dict[str, Any] = {}


def import_jmcomic() -> Any | None:
    """懒加载 jmcomic；未安装时返回 None，由调用方决定提示。"""
    try:
        import jmcomic
    except ImportError:
        return None
    return jmcomic


def _progress_downloader_class(jmcomic: Any) -> type:
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
                self._session.bytes_downloaded += Path(str(img_save_path)).stat().st_size
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
            rate_size = _format_size(int(session.bytes_downloaded / elapsed * 60))
            failed = len(self.download_failed_image) + len(self.download_failed_photo)
            total = f"/{session.total}" if session.total else ""
            logger.info(
                f"[{PLUGIN_NAME}] 下载进度 {session.album_id} "
                f"{session.done}{total} {session.unit} · {_format_size(session.bytes_downloaded)}"
                f"（已用 {elapsed:.0f}s，均 {session.done / elapsed * 60:.1f} {session.unit}/分、"
                f"{rate_size}/分，失败 {failed}）"
            )

    _PROGRESS_DOWNLOADER_CACHE["cls"] = _ProgressDownloader
    return _ProgressDownloader


def _count_downloaded_images(downloader: Any, source_dir: Path) -> int:
    """已成功下载的图片张数。

    以 jmcomic 下载器自己的成功记录为准：它只随版本增删属性（2.7.10 里是
    download_success_dict），没有稳定的「计数器」字段，硬读某个名字会在升级后
    把「下载成功」误判成 0 张。记录缺失时退回数落盘文件，仍然给出可信结果。
    """
    success = getattr(downloader, "download_success_dict", None)
    if isinstance(success, dict):
        recorded = sum(
            len(images) for photos in success.values() for images in photos.values()
        )
        if recorded:
            return recorded
    if not source_dir.exists():
        return 0
    return sum(
        1 for path in source_dir.rglob("*") if path.suffix.lower() in IMAGE_SUFFIXES
    )


class JMComicChannel(BaseChannel):
    name: ClassVar[str] = "jmcomic"
    command: ClassVar[str] = "jm"
    display_name: ClassVar[str] = "JM 漫画"
    requires: ClassVar[tuple[str, ...]] = ("jmcomic",)

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        """内置渠道默认启用：域名交给 jmcomic 自动获取，其余取推荐值。"""
        return {
            "enable": True,
            "client_type": "api",
            "image_suffix": ".jpg",
            "retry_times": DEFAULT_REQUEST_RETRY_TIMES,
            "request_timeout": DEFAULT_REQUEST_TIMEOUT,
            "max_concurrent_photos": 3,
            "max_concurrent_images": 5,
            "use_proxy": False,
        }

    # ---- ID 解析 ----
    def normalize_id(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            raise ChannelError(f"请提供漫画 ID 或链接，例如 /{self.command} 123456")

        matched = _ALBUM_IN_TEXT.search(text)
        if matched:
            return matched.group(1)
        matched = _FIRST_DIGITS.search(text)
        if matched:
            return matched.group(0)
        raise ChannelError("无法从输入中识别出 JM 漫画 ID，请直接给出数字 ID")

    # ---- 元信息 ----
    async def fetch_meta(self, comic_id: str, work_dir: Path) -> ComicMeta:
        jmcomic = import_jmcomic()
        if jmcomic is None:
            raise ChannelError("jmcomic 未安装，请先安装依赖后重载插件")

        option = await asyncio.to_thread(self._build_option, jmcomic, work_dir)
        return await asyncio.to_thread(self._fetch_meta_sync, jmcomic, option, comic_id)

    def _fetch_meta_sync(self, jmcomic: Any, option: Any, comic_id: str) -> ComicMeta:
        client = option.new_jm_client()
        album = client.get_album_detail(jmcomic.JmcomicText.parse_to_jm_id(comic_id))
        return ComicMeta(
            channel=self.name,
            comic_id=str(album.id),
            title=str(getattr(album, "title", "") or ""),
            author=str(getattr(album, "author", "") or ""),
            page_count=len(album) if hasattr(album, "__len__") else 0,
        )

    # ---- 下载 ----
    async def download(
        self,
        comic_id: str,
        work_dir: Path,
        on_progress: ProgressCallback | None = None,
    ) -> DownloadedComic:
        jmcomic = import_jmcomic()
        if jmcomic is None:
            raise ChannelError("jmcomic 未安装，请先安装依赖后重载插件")

        option = await asyncio.to_thread(self._build_option, jmcomic, work_dir)
        loop = asyncio.get_running_loop()

        def report(done: int, total: int, unit: str) -> None:
            if on_progress is None:
                return
            try:
                future = asyncio.run_coroutine_threadsafe(
                    on_progress(done, total, unit), loop
                )
                # 结果无人等待，挂个回调吞掉异常，避免 "Future exception was never retrieved"
                future.add_done_callback(lambda f: f.cancelled() or f.exception())
            except RuntimeError:
                # 事件循环已关闭（插件重载中），丢弃这次进度即可
                pass

        session = DownloadSession(album_id=comic_id)
        _register_session(session)
        try:
            return await asyncio.to_thread(
                self._download_sync, jmcomic, option, comic_id, report, session
            )
        finally:
            _unregister_session(session)

    def cancel_downloads(self) -> list[str]:
        """插件卸载时调用：取消本渠道正在跑的下载并返回其状态（供上层写日志）。"""
        return cancel_active_downloads()

    def _download_sync(
        self,
        jmcomic: Any,
        option: Any,
        comic_id: str,
        report: Any,
        session: DownloadSession,
    ) -> DownloadedComic:
        album_id = jmcomic.JmcomicText.parse_to_jm_id(comic_id)
        downloader = _progress_downloader_class(jmcomic)(option, report, session)
        started = time.monotonic()
        photo_workers = self.opt_int("max_concurrent_photos", 3, minimum=1)
        image_workers = self.opt_int("max_concurrent_images", 5, minimum=1)
        logger.info(
            f"[{PLUGIN_NAME}] 请求下载 {comic_id}"
            f"（并发 章节×{photo_workers} / 图片×{image_workers}）"
        )

        try:
            with downloader:
                album = downloader.download_album(album_id)
        except jmcomic.DownloadCancelledException as exc:
            logger.info(f"[{PLUGIN_NAME}] 下载被取消：{session.summary()}")
            raise DownloadError("下载已被插件重载中断，请重新请求") from exc

        source_dir = Path(option.dir_rule.decide_album_root_dir(album))
        failed_images = len(getattr(downloader, "download_failed_image", []) or [])
        failed_photos = len(getattr(downloader, "download_failed_photo", []) or [])
        image_count = _count_downloaded_images(downloader, source_dir)
        elapsed = max(time.monotonic() - started, 0.001)
        rate_size = _format_size(int(session.bytes_downloaded / elapsed * 60))
        logger.info(
            f"[{PLUGIN_NAME}] 下载结束 {album_id}：成功 {image_count} 张 · "
            f"{_format_size(session.bytes_downloaded)}，"
            f"失败 {failed_images} 张图片 / {failed_photos} 章节，"
            f"耗时 {elapsed:.1f}s（均 {image_count / elapsed * 60:.1f} 张/分、"
            f"{rate_size}/分）"
        )

        if image_count <= 0:
            if failed_images or failed_photos:
                raise DownloadError(
                    f"图片全部下载失败（{failed_images} 张图片、{failed_photos} 个章节失败），"
                    "请稍后重试，或在渠道配置里改用代理/自定义域名"
                )
            raise DownloadError("没有下载到任何图片，可能本子 ID 不存在或已被下架")

        meta = ComicMeta(
            channel=self.name,
            comic_id=str(album.id),
            title=str(getattr(album, "title", "") or ""),
            author=str(getattr(album, "author", "") or ""),
            page_count=len(album) if hasattr(album, "__len__") else 0,
        )
        return DownloadedComic(
            meta=meta,
            source_dir=source_dir,
            image_count=image_count,
            # 章节级失败意味着该章图片一张都没拿到，同样计入「失败」让用户知道产物不完整
            failed_images=failed_images + failed_photos,
        )

    # ---- JmOption 构建 ----
    def _build_option(self, jmcomic: Any, base_dir: Path) -> Any:
        """把渠道配置翻译成 jmcomic 的 option。

        目录规则固定 Bd/Aid/Pindex：用 Aid 作目录名避开标题里的非法字符，
        再带章节维度 Pindex，否则多章节本子的各章会用相同页码互相覆盖。
        """
        client_type = self.opt_str("client_type", "api")
        impl = "html" if client_type.startswith("html") else "api"
        option_dict: dict[str, Any] = {
            "dir_rule": {"base_dir": str(base_dir), "rule": "Bd/Aid/Pindex"},
            "download": {
                "image": {"suffix": self.opt_str("image_suffix", ".jpg")},
                "threading": {
                    "photo": self.opt_int("max_concurrent_photos", 3, minimum=1),
                    "image": self.opt_int("max_concurrent_images", 5, minimum=1),
                },
            },
            "client": {"impl": impl},
        }

        # 只有「自定义域名」的客户端类型才用下面输入的清单；其它交给 jmcomic 自动更新。
        # 清单顺序就是 jmcomic 的重试顺序，前一个失败自动换下一个。
        if client_type.endswith("_custom"):
            key = "html_domains" if impl == "html" else "api_domains"
            default_domains = DEFAULT_HTML_DOMAINS if impl == "html" else DEFAULT_API_DOMAINS
            domains = self.opt_list(key) or split_option_list(default_domains)
            option_dict["client"]["domain"] = {impl: domains}

        option_dict["client"]["retry_times"] = self.opt_int(
            "retry_times", DEFAULT_REQUEST_RETRY_TIMES, minimum=1
        )

        # 单请求超时对 api / html 两种客户端都生效；配小了坏节点能更快放弃
        meta_data: dict[str, Any] = {
            "timeout": self.opt_int(
                "request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1
            )
        }
        proxy = self.settings.proxy if self.opt_bool("use_proxy", False) else ""
        if proxy:
            meta_data["proxies"] = proxy
        else:
            # 显式置空：容器里常带系统代理变量，不覆盖会让请求绕到不存在的代理上
            meta_data["proxies"] = {}
        option_dict["client"]["postman"] = {"meta_data": meta_data}

        return jmcomic.JmModuleConfig.option_class().construct(option_dict)

"""单次下载任务的编排。

固定顺序：解析 ID → 取元信息 → 下载图片 → **统一打包加密**（上层，不归渠道）。
渠道只填「下载」这一格，产物形态、密码、清理都在这里统一。

下载目录按「渠道 + 作品 ID」固定，不随请求时间变：到点暂停时把它留下，重发一次命令
就能接着下（已落盘的页会被渠道跳过）；只有打包成功并交付后才真正清掉。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from ..domain import (
    ChannelError,
    ComicMeta,
    DownloadCancelled,
    DownloadError,
    DownloadedComic,
    DownloadPaused,
    PackError,
    PackResult,
    PluginError,
    PluginSettings,
)
from ..domain.constants import CANCEL_POLL_INTERVAL, IMAGE_SUFFIXES, PLUGIN_NAME
from ..utils import build_package_name, format_size
from .channel import BaseChannel, CancelToken, ProgressCallback
from .packer import Packer

# 断点目录名：放 temp 下的独立子目录，启动清理与手动清理都能一把带走
RESUME_DIRNAME = "resume"


@dataclass(slots=True)
class DownloadOutcome:
    """一次成功任务的完整结果，供命令层拼消息与决定清理。"""

    meta: ComicMeta
    pack: PackResult
    source_dir: Path
    work_dir: Path
    image_count: int
    failed_images: int
    elapsed: float

    @property
    def sendable_path(self) -> Path | None:
        """可以发给用户的文件路径；仅本地保存（fmt=none）时为 None。"""
        if not self.pack.success or self.pack.fmt == "none":
            return None
        return self.pack.output_path


def _human_span(seconds: int) -> str:
    """把秒数说成人话：断点保留时长要写在给用户看的提示里。"""
    if seconds <= 0:
        return "不保留"
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} 分钟"
    return f"{seconds / 3600:.1f} 小时".replace(".0", "")


class DownloadTask:
    """把一次用户请求跑完，失败时抛 PluginError 子类（消息可直接给用户）。"""

    def __init__(
        self,
        channel: BaseChannel,
        settings: PluginSettings,
        temp_root: Path,
        save_root: Path,
        pack_format: str | None = None,
    ) -> None:
        self.channel = channel
        self.settings = settings
        self.temp_root = temp_root
        self.save_root = save_root
        # 单次下载可以指定产物形态（命令末尾加 pdf / 长图 等），不写就跟随全局配置
        self.pack_format = pack_format or settings.pack_format

    async def run(
        self,
        raw_id: str,
        on_progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> DownloadOutcome:
        started = time.monotonic()
        comic_id = self.channel.normalize_id(raw_id)
        work_dir = self._prepare_work_dir(comic_id)

        try:
            meta = await self._fetch_meta(comic_id, work_dir)
            downloaded = await self._download(
                comic_id, work_dir, on_progress, meta, cancel
            )
            pack = await asyncio.to_thread(self._pack, downloaded, meta)
        except (DownloadPaused, DownloadCancelled):
            # 到点暂停 / 手动取消：断点要留着给下一次接着下，绝不能清目录
            raise
        except Exception:
            Packer.cleanup(work_dir)
            raise

        if not pack.success:
            Packer.cleanup(work_dir)
            raise PackError(pack.error or "打包失败")

        return DownloadOutcome(
            meta=meta,
            pack=pack,
            source_dir=downloaded.source_dir,
            work_dir=work_dir,
            image_count=downloaded.image_count,
            failed_images=downloaded.failed_images,
            elapsed=time.monotonic() - started,
        )

    # ---- 断点目录 ----

    def _prepare_work_dir(self, comic_id: str) -> Path:
        """准备下载目录，顺手清掉别本的过期断点。

        「断点保留时长」配 0 时连本作旧图一起扔掉，等价于关掉续跑。
        """
        work_dir = self.temp_root / RESUME_DIRNAME / f"{self.channel.name}-{comic_id}"
        root = work_dir.parent
        root.mkdir(parents=True, exist_ok=True)
        ttl = max(0, self.settings.resume_ttl)
        now = time.time()
        for entry in root.iterdir():
            if entry == work_dir:
                continue
            try:
                expired = ttl <= 0 or now - entry.stat().st_mtime > ttl
            except OSError:
                continue
            if expired:
                logger.info(f"[{PLUGIN_NAME}] 清理过期断点：{entry.name}")
                Packer.cleanup(entry)
        if ttl <= 0:
            Packer.cleanup(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir

    def _scan_images(self, work_dir: Path) -> tuple[int, int]:
        """数一下断点里已经有多少张图、共多大：暂停提示要用。"""
        count = 0
        size = 0
        for path in work_dir.rglob("*"):
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                if not path.is_file():
                    continue
                size += path.stat().st_size
            except OSError:
                continue
            count += 1
        return count, size

    def _halted_text(
        self, work_dir: Path, meta: ComicMeta, *, prefix: str
    ) -> tuple[str, int, int]:
        """暂停/取消共用的「停在哪儿」文案：已存多少张、多大、怎么接着下。"""
        saved, size = self._scan_images(work_dir)
        saved_text = (
            f"{saved}/{meta.page_count} 张" if meta.page_count else f"{saved} 张"
        )
        text = (
            f"{prefix}：已保存 {saved_text}（{format_size(size)}）。\n"
            f"再发一次同样的命令会从断点接着下，断点保留 "
            f"{_human_span(self.settings.resume_ttl)}"
        )
        return text, saved, size

    def _cancelled(
        self, work_dir: Path, meta: ComicMeta, comic_id: str
    ) -> DownloadCancelled:
        text, saved, _ = self._halted_text(
            work_dir, meta, prefix="⏹ 已取消下载"
        )
        logger.info(f"[{PLUGIN_NAME}] {comic_id} 被用户取消，断点保留：{saved} 张")
        return DownloadCancelled(text)

    def _paused(self, work_dir: Path, meta: ComicMeta, comic_id: str) -> DownloadPaused:
        saved, size = self._scan_images(work_dir)
        saved_text = (
            f"{saved}/{meta.page_count} 张" if meta.page_count else f"{saved} 张"
        )
        minutes = max(1, round(self.settings.segment_timeout / 60))
        logger.info(
            f"[{PLUGIN_NAME}] {comic_id} 连续下载 {minutes} 分钟，先停下保留断点："
            f"{saved_text} · {format_size(size)}"
        )
        return DownloadPaused(
            f"⏸ 已连续下载 {minutes} 分钟，先停在这里：已保存 {saved_text}"
            f"（{format_size(size)}）。\n"
            f"再发一次同样的命令会从断点接着下，断点保留 "
            f"{_human_span(self.settings.resume_ttl)}",
            saved=saved,
            total=meta.page_count,
        )

    # ---- 元信息与下载 ----

    async def _fetch_meta(self, comic_id: str, work_dir: Path) -> ComicMeta:
        try:
            return await asyncio.wait_for(
                self.channel.fetch_meta(comic_id, work_dir),
                timeout=min(self.settings.download_timeout, 120),
            )
        except asyncio.TimeoutError as exc:
            raise DownloadError("获取漫画信息超时，请稍后重试") from exc
        except ChannelError:
            raise
        except Exception as exc:  # 渠道内部异常统一翻译
            logger.error(f"[{PLUGIN_NAME}] 获取元信息失败: {exc}")
            raise self.channel.wrap_failure(exc) from exc

    def _time_budget(self) -> tuple[float, bool]:
        """一次请求最多下多久，以及「到点」是暂停留断点还是判失败。

        分段时长不大于单本超时才按分段处理；配得比单本超时还长就照旧走硬超时，
        免得两个值打架时行为不可预期。
        """
        hard = float(self.settings.download_timeout)
        segment = self.settings.segment_timeout
        if segment <= 0 or segment > hard:
            return hard, False
        return float(segment), True

    async def _download(
        self,
        comic_id: str,
        work_dir: Path,
        on_progress: ProgressCallback | None,
        meta: ComicMeta,
        cancel: CancelToken | None = None,
    ) -> DownloadedComic:
        """按配置重试。整本超时是硬上限，分段到期则是「抓个暂停」，两者都不突破对方。"""
        attempts = max(0, self.settings.download_retry_times) + 1
        last_error: Exception | None = None
        timeout, pause_on_deadline = self._time_budget()
        token = cancel or CancelToken()

        for attempt in range(1, attempts + 1):
            download = asyncio.create_task(
                self.channel.download(comic_id, work_dir, on_progress, cancel=token)
            )
            outcome, error = await self._watch(download, token, timeout)

            if outcome == "cancelled":
                # 用户手动取消：和到点暂停一样留断点，不重试
                raise self._cancelled(work_dir, meta, comic_id)
            if outcome == "timeout":
                if pause_on_deadline:
                    raise self._paused(work_dir, meta, comic_id)
                last_error = DownloadError("下载超时，请稍后重试")
            elif error is None:
                return download.result()
            elif isinstance(error, asyncio.CancelledError):
                # 下载在非「到点」的情况下被叫停（外部取消）：同样留断点
                raise self._cancelled(work_dir, meta, comic_id)
            elif isinstance(error, (DownloadPaused, DownloadCancelled)):
                # 暂停/取消不是失败：配额或封禁状态下重试只会再撞一次，
                # 直接交给上层展示「断点已保留」
                raise error
            elif isinstance(error, PluginError):
                last_error = error
            else:
                logger.error(f"[{PLUGIN_NAME}] 下载异常(第 {attempt} 次): {error}")
                last_error = self.channel.wrap_failure(
                    error if isinstance(error, Exception) else Exception(str(error))
                )

            if attempt < attempts:
                logger.warning(
                    f"[{PLUGIN_NAME}] 下载失败，{attempt}/{attempts} 次，准备重试"
                )
                await asyncio.sleep(min(3 * attempt, 10))

        assert last_error is not None
        raise last_error

    async def _watch(
        self, download: asyncio.Task[DownloadedComic], token: CancelToken, timeout: float
    ) -> tuple[str, BaseException | None]:
        """盯住一次下载尝试，返回（结局, 异常）。

        结局是 ``done`` / ``cancelled``（用户手动取消）/ ``timeout``（到期）。
        这里用轮询而不是单靠 ``wait_for``：取消令牌是线程间共享的，线程池里的下载
        （JM）只认它，所以「被叫停」必须先让令牌生效，再用 ``task.cancel()`` 收掉
        跑在事件循环上的那一类渠道——两件事写在同一个循环里才不会各漏一边。
        """
        deadline = time.monotonic() + timeout
        outcome = "done"
        while True:
            done, _ = await asyncio.wait({download}, timeout=CANCEL_POLL_INTERVAL)
            if done:
                break
            if token.cancelled:
                outcome = "cancelled"
                break
            if time.monotonic() >= deadline:
                outcome = "timeout"
                break
        if outcome != "done":
            token.cancel()
            download.cancel()
        results = await asyncio.gather(download, return_exceptions=True)
        error = results[0]
        if isinstance(error, BaseException):
            return outcome, error
        return outcome, None

    # ---- 打包与清理 ----

    def _pack(self, downloaded: DownloadedComic, meta: ComicMeta) -> PackResult:
        base_name = build_package_name(
            title=meta.display_title,
            comic_id=meta.comic_id,
            template=self.settings.filename_template,
            custom_template=self.settings.filename_custom,
            password=self.settings.pack_password,
            author=meta.author,
            short_id=self.channel.short_id(meta.comic_id),
        )
        if self.pack_format == "none":
            return self._keep_local(downloaded, base_name)

        packer = Packer(self.pack_format, self.settings.pack_password)
        return packer.pack(downloaded.source_dir, base_name, self.temp_root)

    def _keep_local(self, downloaded: DownloadedComic, base_name: str) -> PackResult:
        """仅本地保存：把图片目录搬到长期保存目录，而不是留在临时目录里被清掉。"""
        target = self.save_root / base_name
        suffix = 1
        while target.exists():
            suffix += 1
            target = self.save_root / f"{base_name}_{suffix}"
        try:
            shutil.move(str(downloaded.source_dir), str(target))
        except OSError as exc:
            logger.error(f"[{PLUGIN_NAME}] 移动到保存目录失败: {exc}")
            raise PackError(f"保存到本地失败：{exc}") from exc
        return PackResult(success=True, output_path=target, fmt="none", encrypted=False)

    def cleanup(self, outcome: DownloadOutcome) -> None:
        """任务收尾：产物与断点目录都清掉；仅本地保存的目录属于用户资产，保留。

        断点目录必须一起清：否则下一本同名请求还会把这里的旧图当成「已经下过」。
        """
        if outcome.pack.fmt != "none":
            Packer.cleanup(outcome.pack.output_path)
        Packer.cleanup(outcome.source_dir)
        Packer.cleanup(outcome.work_dir)

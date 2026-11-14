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
from ..domain.constants import (
    CANCEL_POLL_INTERVAL,
    DRAIN_TIMEOUT,
    IMAGE_SUFFIXES,
    PLUGIN_NAME,
    RESUME_DIRNAME,
)
from ..utils import build_package_name, format_size
from .channel import (
    INTERRUPT_RELOAD,
    BaseChannel,
    CancelToken,
    ProgressCallback,
    ProgressState,
)
from .packer import Packer


# 正在使用中的工作目录：清理过期断点时要跳过它们。
# 目录自身的 mtime 只在直接子项增删时更新，光看时间会把还在下的那本当成过期断点删掉。
_ACTIVE_WORK_DIRS: set[Path] = set()


def _newest_mtime(path: Path) -> float:
    """目录里最新一次写入的时间（含子目录里的文件），用来判断断点是不是真过期了。"""
    newest = path.stat().st_mtime
    for child in path.rglob("*"):
        try:
            if child.is_file():
                newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
    return newest


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


def _count_text(saved: int, total: int) -> str:
    """「已存多少张」文案：分母只认真实总页数，不知道就不写分母。

    ``ComicMeta.page_count`` 在 JM、哔咔上是**章节数**（单章本 = 1），拿它当总张数会
    写出「已保存 85/1 张」这种自相矛盾的提示，所以暂停/取消文案改用进度快照里的总数。
    """
    return f"{saved}/{total} 张" if total > 0 else f"{saved} 张"


@dataclass(slots=True)
class _ProgressTap:
    """接住渠道的进度回调，顺手留下最后一份快照。

    暂停/取消发生时要写「已保存 N/总数 张」，而真正的总页数只出现在进度快照里；
    渠道到点被叫停后不会再报进度，只能由这一层把最后一份留下来。
    """

    callback: ProgressCallback | None = None
    state: ProgressState | None = None

    @property
    def total(self) -> int:
        """整本多少张图；渠道没给就返回 0（文案里干脆不写分母）。"""
        state = self.state
        if state is None:
            return 0
        if state.image_total > 0:
            return state.image_total
        # 章节型主计数的 total 是章节数，拿来当「张」的分母就会写出「85/1 张」
        return 0 if state.unit == "章节" else state.total

    async def __call__(self, state: ProgressState) -> None:
        self.state = state
        if self.callback is not None:
            await self.callback(state)


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
        tap = _ProgressTap(on_progress)
        # 登记「这个目录正在用」：别的任务清理过期断点时要跳过它，否则会把还在下的那本删掉
        _ACTIVE_WORK_DIRS.add(work_dir)
        try:
            return await self._download_and_pack(
                comic_id, work_dir, tap, cancel, started
            )
        finally:
            # 暂停/取消也要注销：目录留在磁盘上，但已经没人写了
            _ACTIVE_WORK_DIRS.discard(work_dir)

    async def _download_and_pack(
        self,
        comic_id: str,
        work_dir: Path,
        tap: _ProgressTap,
        cancel: CancelToken | None,
        started: float,
    ) -> DownloadOutcome:
        """取元信息 → 下载 → 打包；工作目录的登记与注销由 run 负责。"""
        try:
            meta = await self._fetch_meta(comic_id, work_dir)
            downloaded = await self._download(comic_id, work_dir, tap, meta, cancel)
        except (DownloadPaused, DownloadCancelled):
            # 到点暂停 / 手动取消：断点要留着给下一次接着下，绝不能清目录
            raise
        except Exception:
            Packer.cleanup(work_dir)
            raise

        # 打包放在下载的 try 之外：打包失败不该把已经下好的图一起清掉（见下方 PackError）
        pack = await asyncio.to_thread(self._pack, downloaded, meta)
        if not pack.success:
            # 图片留着，重发命令只需重新打包，不必整本重下；断点关了就没得留，别乱许诺
            hint = (
                "\n已下好的图片已保留，发 /comic retry 会直接重新打包"
                if self.settings.resume_ttl > 0
                else ""
            )
            raise PackError((pack.error or "打包失败") + hint)

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
            # 正在下的那本自己不能删；别人正在用的目录也不能碰
            if entry == work_dir or entry in _ACTIVE_WORK_DIRS:
                continue
            try:
                # 先按目录时间粗筛，可疑再用「目录里最新的文件」确认：图片写进章节目录时
                # 父目录 mtime 不动，只看它会把活着的下载误判成过期断点
                expired = ttl <= 0 or now - entry.stat().st_mtime > ttl
                if expired and ttl > 0:
                    expired = now - _newest_mtime(entry) > ttl
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

    def _resume_tail(self) -> str:
        """「怎么接着下」这半句：只说断点在不在、怎么继续。

        配置项名字不进这句话：群里没人能改配置，读起来只会变成一句需要解读的括注；
        「重载会清掉断点」这种不确定的前提留给 ``_reload_interrupted`` 那一条去说。
        """
        if self.settings.resume_ttl <= 0:
            return "发 /comic retry 会从头下载"
        return (
            "发 /comic retry 接着下，断点最多留 "
            f"{_human_span(self.settings.resume_ttl)}"
        )

    def _halted_text(
        self, work_dir: Path, tap: _ProgressTap, *, prefix: str
    ) -> tuple[str, int, int]:
        """暂停/取消共用的「停在哪儿」文案：已存多少张、多大、怎么接着下。"""
        saved, size = self._scan_images(work_dir)
        text = (
            f"{prefix}：已保存 {_count_text(saved, tap.total)}"
            f"（{format_size(size)}）。\n{self._resume_tail()}"
        )
        return text, saved, size

    def _cancelled(
        self,
        work_dir: Path,
        comic_id: str,
        token: CancelToken,
        tap: _ProgressTap,
    ) -> DownloadCancelled:
        """取消：用户按停就照暂停文案说；插件重载另说，因为图可能真的没了。"""
        if token.reason == INTERRUPT_RELOAD:
            return self._reload_interrupted(work_dir, comic_id, token, tap)
        text, saved, _ = self._halted_text(work_dir, tap, prefix="⏹ 已取消下载")
        logger.info(f"[{PLUGIN_NAME}] {comic_id} 被用户取消，断点保留：{saved} 张")
        return DownloadCancelled(text)

    def _reload_interrupted(
        self,
        work_dir: Path,
        comic_id: str,
        token: CancelToken,
        tap: _ProgressTap,
    ) -> DownloadCancelled:
        """插件重载/卸载打断下载：如实说停在哪儿，以及这些图到底还在不在。

        不能照抄暂停文案里的「断点保留 X」：重载会按「启动时清理临时目录」清空 temp（含
        断点），也可能因为断点保留时长被配成 0 而扔掉，此时让用户以为能接着下就是撒谎。
        """
        saved, size = self._scan_images(work_dir)
        state = token.state
        if state is not None:
            # 目录可能已经清空，优先用取消那一刻记下的进度，别在这里数出一个 0
            done_text, done_size = state.headline, state.bytes_downloaded
        else:
            done_text, done_size = _count_text(saved, tap.total), size

        kept = not self.settings.clean_temp_on_start and self.settings.resume_ttl > 0
        if kept:
            tail = (
                "发 /comic retry 接着下，断点最多留 "
                f"{_human_span(self.settings.resume_ttl)}"
            )
        else:
            # 为什么没留住是配置问题，写进日志给管理员；群成员只需要知道结果
            logger.info(
                f"[{PLUGIN_NAME}] 断点未保留（启动清理={self.settings.clean_temp_on_start}，"
                f"保留时长={self.settings.resume_ttl}）"
            )
            tail = "这些图片没能保留，发 /comic retry 会从头下载"
        logger.info(
            f"[{PLUGIN_NAME}] {comic_id} 因插件重载中断：{done_text} · "
            f"{format_size(done_size)}，断点{'保留' if kept else '未保留'}"
        )
        return DownloadCancelled(
            f"⏹ 插件重载，下载中断：停下时已下好 {done_text}"
            f"（{format_size(done_size)}）。\n{tail}"
        )

    def _paused(
        self, work_dir: Path, comic_id: str, tap: _ProgressTap
    ) -> DownloadPaused:
        saved, size = self._scan_images(work_dir)
        saved_text = _count_text(saved, tap.total)
        minutes = max(1, round(self.settings.segment_timeout / 60))
        logger.info(
            f"[{PLUGIN_NAME}] {comic_id} 连续下载 {minutes} 分钟，先停下保留断点："
            f"{saved_text} · {format_size(size)}"
        )
        return DownloadPaused(
            f"⏸ 已连续下载 {minutes} 分钟，先停在这里：已保存 {saved_text}"
            f"（{format_size(size)}）。\n{self._resume_tail()}",
            saved=saved,
            total=tap.total,
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
        tap: _ProgressTap,
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
                self.channel.download(comic_id, work_dir, tap, cancel=token)
            )
            outcome, error = await self._watch(download, token, timeout)

            if outcome == "cancelled":
                # 手动取消 / 插件重载：都留断点逻辑由 _cancelled 按原因分流，且不重试
                raise self._cancelled(work_dir, comic_id, token, tap)
            if outcome == "timeout":
                if pause_on_deadline:
                    raise self._paused(work_dir, comic_id, tap)
                last_error = DownloadError("下载超时，请稍后重试")
            elif error is None:
                return download.result()
            elif isinstance(error, asyncio.CancelledError):
                # 下载在非「到点」的情况下被叫停（外部取消 / 插件卸载）：同样留断点
                raise self._cancelled(work_dir, comic_id, token, tap)
            elif (
                isinstance(error, DownloadCancelled)
                and token.reason == INTERRUPT_RELOAD
            ):
                # 渠道自己先发现「插件在重载」时只报了半句话，进度与去留由任务层统一说清
                raise self._cancelled(work_dir, comic_id, token, tap)
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
        if outcome != "done" and not await self.channel.drain(token, DRAIN_TIMEOUT):
            # 线程没停稳时下游照样会去数目录，只能如实记一笔，别让它看起来像已经停好了
            logger.warning(
                f"[{PLUGIN_NAME}] {self.channel.display_name} 的下载线程未在 "
                f"{DRAIN_TIMEOUT:.0f}s 内退出"
            )
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
            name_limit=self.settings.filename_max_length,
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

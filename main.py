"""漫画下载器 —— 多渠道按 ID 下载整本漫画，统一打包并加密发送。

命令层只做四件事：准入判断、渠道选择、进度转发、结果发送；
真正的下载在渠道里，打包与加密在 `core/packer.py`（上层统一）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import (
    AstrMessageEvent,
    MessageChain,
    MessageEventResult,
    filter,
)
from astrbot.api.star import Context, Star, StarTools, register

from .core import (
    AccessGuard,
    DownloadOutcome,
    DownloadTask,
    build_channels,
    command_map,
    describe_channels,
)
from .core.channel import ProgressCallback
from .core.packer import Packer
from .domain import PluginError, PluginSettings
from .domain.constants import (
    PACK_FORMATS,
    PLUGIN_AUTHOR,
    PLUGIN_DESC,
    PLUGIN_DISPLAY_NAME,
    PLUGIN_NAME,
    PLUGIN_REPO,
    PLUGIN_VERSION,
    PROGRESS_MIN_INTERVAL,
    PROGRESS_STEP_PERCENT,
)


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class ComicParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.settings = PluginSettings.from_config(self.config)
        self.data_dir = self._resolve_data_dir()
        self.save_root = self._resolve_save_dir()
        self.temp_root = self._resolve_temp_dir()
        self.guard = AccessGuard(self.settings)
        self.channels = build_channels(self.settings)
        self._commands = command_map(self.channels)
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent_tasks)

        logger.info(
            f"[{PLUGIN_NAME}] 已加载渠道: {', '.join(self.channels) or '无'}；"
            f"保存目录: {self.save_root}"
        )

    async def terminate(self) -> None:
        """卸载/重载时清掉临时目录里的残留（只动临时目录，不碰用户的保存目录）。"""
        if not self.temp_root.exists():
            return
        for entry in self.temp_root.iterdir():
            Packer.cleanup(entry)
        logger.debug(f"[{PLUGIN_NAME}] 已清理临时目录: {self.temp_root}")

    # ==================== 命令 ====================

    @filter.command("jm")
    async def cmd_jm(
        self, event: AstrMessageEvent, comic_id: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载指定 ID 的 JM 漫画。用法：/jm <ID 或链接>"""
        async for result in self._handle_download(event, "jm", comic_id):
            yield result

    @filter.command("漫画帮助", alias={"comichelp"})
    async def cmd_help(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查看可用渠道与用法。"""
        packs = "、".join(PACK_FORMATS)
        text = (
            f"📚 {PLUGIN_DISPLAY_NAME}\n"
            f"{describe_channels(self.channels)}\n\n"
            f"打包格式：{self.settings.pack_format}（可选 {packs}）\n"
            f"打包密码：{'已设置' if self.settings.pack_password else '未设置'}\n"
            f"用法：/<命令> <ID 或链接>，例如 /jm 123456"
        )
        yield event.plain_result(text)

    # ==================== 下载流程 ====================

    async def _handle_download(
        self, event: AstrMessageEvent, command: str, raw_id: str
    ) -> AsyncGenerator[MessageEventResult, None]:
        channel = self._commands.get(command)
        if channel is None:
            yield event.plain_result("❌ 该下载渠道未启用，请先在插件配置里添加")
            return

        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        denied = self.guard.check_session(session_id)
        if denied:
            yield event.plain_result(denied)
            return

        if not raw_id.strip():
            yield event.plain_result(
                f"用法：/{command} <ID 或链接>\n示例：/{command} 123456"
            )
            return

        try:
            channel.ensure_available()
            comic_id = channel.normalize_id(raw_id)
        except PluginError as exc:
            yield event.plain_result(f"❌ {exc}")
            return

        debounce_key = f"{channel.name}:{comic_id}"
        if self.guard.is_duplicate(session_id, debounce_key):
            yield event.plain_result("⏳ 这个本子刚刚请求过，请稍后再试")
            return

        if self._semaphore.locked():
            yield event.plain_result("⏳ 前面的下载还没结束，你的请求已排队")

        async with self._semaphore:
            yield event.plain_result(
                f"⏳ 开始下载 {channel.display_name} {comic_id}…"
            )
            task = DownloadTask(
                channel, self.settings, self.temp_root, self.save_root
            )
            try:
                outcome = await task.run(raw_id, self._progress_callback(event))
            except PluginError as exc:
                self.guard.forget(session_id, debounce_key)
                yield event.plain_result(f"❌ {exc}")
                return
            except Exception as exc:  # 兜底：任何未预期异常都不该让插件静默失联
                self.guard.forget(session_id, debounce_key)
                logger.error(f"[{PLUGIN_NAME}] 下载失败: {exc}")
                yield event.plain_result("❌ 下载出现未预期错误，详情见日志")
                return

            try:
                async for result in self._deliver(event, outcome):
                    yield result
            finally:
                if self.settings.auto_delete_after_send:
                    task.cleanup(outcome)

    async def _deliver(
        self, event: AstrMessageEvent, outcome: DownloadOutcome
    ) -> AsyncGenerator[MessageEventResult, None]:
        summary = self._format_outcome(outcome)
        sendable = outcome.sendable_path

        if sendable is None:
            # 仅本地保存模式：不发送文件，但告诉用户落在了哪里
            yield event.plain_result(summary)
            return

        chain = MessageChain(
            [
                Comp.Plain(summary),
                Comp.File(name=sendable.name, file=str(sendable)),
            ]
        )
        yield event.chain_result(chain.chain)

    # ==================== 内部工具 ====================

    def _progress_callback(self, event: AstrMessageEvent) -> ProgressCallback | None:
        """按百分比步进与最小间隔双重节流，避免长任务刷屏。"""
        if not self.settings.show_progress:
            return None

        state = {"last_pct": -1, "last_ts": 0.0}

        async def on_progress(done: int, total: int, unit: str) -> None:
            if total > 0:
                percent = int(done * 100 / total)
                if percent - state["last_pct"] < PROGRESS_STEP_PERCENT:
                    return
                state["last_pct"] = percent
                text = f"📥 下载中 {percent}%（{done}/{total} {unit}）"
            else:
                now = time.monotonic()
                if now - state["last_ts"] < PROGRESS_MIN_INTERVAL:
                    return
                state["last_ts"] = now
                text = f"📥 下载中，已完成 {done} {unit}"

            try:
                await event.send(MessageChain([Comp.Plain(text)]))
            except Exception as exc:  # 进度发不出去不影响下载本身
                logger.debug(f"[{PLUGIN_NAME}] 发送进度失败（忽略）: {exc}")

        return on_progress

    def _format_outcome(self, outcome: DownloadOutcome) -> str:
        lines = [f"✅ {outcome.meta.display_title}"]
        if outcome.meta.author:
            lines.append(f"作者：{outcome.meta.author}")
        count_line = f"图片：{outcome.image_count} 张"
        if outcome.failed_images:
            count_line += f"（{outcome.failed_images} 张失败）"
        lines.append(count_line)

        if outcome.pack.fmt == "none":
            lines.append(f"已保存到本地：{outcome.source_dir}")
        elif outcome.pack.encrypted:
            lines.append("已加密打包，密码见插件配置")
        lines.append(f"耗时 {outcome.elapsed:.1f}s")
        return "\n".join(lines)

    def _resolve_data_dir(self) -> Path:
        try:
            path = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as exc:  # 拿不到平台数据目录时退回插件目录，插件仍可用
            logger.error(f"[{PLUGIN_NAME}] 获取数据目录失败，回退到插件目录: {exc}")
            path = Path(__file__).parent / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_save_dir(self) -> Path:
        """长期保存目录：只有 pack_format=none（仅本地保存）时会往这里写。"""
        raw = Path(self.settings.download_dir)
        path = raw if raw.is_absolute() else self.data_dir / raw
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_temp_dir(self) -> Path:
        """临时目录：下载图片与待发送的压缩包都放这里，发送完即删。"""
        path = self.data_dir / "temp"
        path.mkdir(parents=True, exist_ok=True)
        return path

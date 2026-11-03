"""漫画下载器 —— 多渠道按 ID 下载整本漫画，统一打包发送，支持密码加密。

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
    PROGRESS_REACTIONS,
    PROGRESS_STEP_PERCENT,
)

# 只有 OneBot(CQHTTP) 协议端提供 set_msg_emoji_like（QQ 的「贴表情」）
_REACTION_PLATFORMS = frozenset({"aiocqhttp"})
_REACTION_TIMEOUT = 5.0


async def set_progress_reaction(event: AstrMessageEvent, status: str) -> bool:
    """在触发命令的那条消息上贴表情，把「开始/完成/失败」压成一次轻量反馈。

    协议端不支持表情回应时返回 False，由调用方退回文本进度，不能因为提示失败影响下载。
    """
    if event.get_platform_name() not in _REACTION_PLATFORMS:
        return False
    # 私聊一律不支持表情回应（协议端会直接拒绝），省掉这次注定失败的请求
    if getattr(event, "is_private_chat", lambda: False)():
        logger.debug(f"[{PLUGIN_NAME}] 私聊不支持表情回应，改用文本进度")
        return False

    reaction = PROGRESS_REACTIONS.get(status)
    message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
    if reaction is None or message_id is None:
        return False
    emoji_id, emoji_type = reaction

    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None) or getattr(
        getattr(bot, "api", None), "call_action", None
    )
    if not callable(call_action):
        logger.debug(f"[{PLUGIN_NAME}] 协议端不支持 call_action，跳过进度贴表情")
        return False

    try:
        resp = await asyncio.wait_for(
            call_action(
                "set_msg_emoji_like",
                message_id=int(message_id),
                emoji_id=emoji_id,
                emoji_type=emoji_type,
                set=True,
            ),
            timeout=_REACTION_TIMEOUT,
        )
    except Exception as exc:  # 贴表情只是锦上添花
        logger.warning(f"[{PLUGIN_NAME}] 贴表情失败，改用文本进度: {exc}")
        return False

    # 有的协议端不抛异常，只在响应里标 failed
    if isinstance(resp, dict) and resp.get("status") == "failed":
        wording = resp.get("wording") or resp.get("retcode")
        logger.warning(f"[{PLUGIN_NAME}] 贴表情失败，改用文本进度: {wording}")
        return False
    return True


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
        self._clean_temp_on_start()

        logger.info(
            f"[{PLUGIN_NAME}] 已加载渠道: {', '.join(self.channels) or '无'}；"
            f"保存目录: {self.save_root}"
        )

    def _clean_temp_on_start(self) -> None:
        """按配置在加载时清空临时目录。

        下载线程不随插件卸载停下，异常退出（崩溃、强杀）时 terminate 也不会跑，半成品
        会一直占着磁盘；加载这一刻不可能有下载在跑，因此是安全的清理时机。
        """
        if not self.settings.clean_temp_on_start or not self.temp_root.exists():
            return
        removed = 0
        for entry in self.temp_root.iterdir():
            Packer.cleanup(entry)
            removed += 1
        if removed:
            logger.info(f"[{PLUGIN_NAME}] 启动清理：已删除 {removed} 个临时目录")

    async def terminate(self) -> None:
        """重载/卸载时结束进行中的下载：先取消并记下进度，再清空临时目录。

        下载跑在线程池里，插件卸载并不会让它停，结果也无处投递——留着只会继续占带宽与
        磁盘，所以这里直接取消并清掉；日志里留下「跑到哪、下了多少 MB」备查。
        """
        for channel in self.channels.values():
            for summary in channel.cancel_downloads():
                logger.info(f"[{PLUGIN_NAME}] 重载中断下载：{summary}")

        if not self.temp_root.exists():
            return
        removed = 0
        for entry in self.temp_root.iterdir():
            Packer.cleanup(entry)
            removed += 1
        if removed:
            logger.info(f"[{PLUGIN_NAME}] 已清理 {removed} 个临时目录")

    # ==================== 命令 ====================

    @filter.command("jm")
    async def cmd_jm(
        self, event: AstrMessageEvent, comic_id: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载指定 ID 的 JM 漫画。用法：/jm <ID 或链接>"""
        async for result in self._handle_download(event, "jm", comic_id):
            yield result

    @filter.command("nh")
    async def cmd_nh(
        self, event: AstrMessageEvent, comic_id: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载指定 ID 的 nhentai 作品。用法：/nh <ID 或链接>"""
        async for result in self._handle_download(event, "nh", comic_id):
            yield result

    @filter.command("漫画帮助", alias={"comichelp"})
    async def cmd_help(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查看可用渠道与用法。"""
        packs = "、".join(PACK_FORMATS)
        progress_labels = {"emoji": "贴表情", "text": "文本进度", "none": "关闭"}
        template_labels = {
            "title_id": "标题[ID]",
            "title_id_pw": "标题[ID] + 密码",
            "id_title": "[ID]标题",
            "id_title_pw": "[ID]标题 + 密码",
            "id_only": "仅 ID",
            "title_only": "仅标题",
            "custom": "自定义模板",
        }
        text = (
            f"📚 {PLUGIN_DISPLAY_NAME}\n"
            f"{describe_channels(self.channels)}\n\n"
            f"打包格式：{self.settings.pack_format}（可选 {packs}）\n"
            f"打包密码：{'已设置' if self.settings.pack_password else '未设置'}\n"
            f"文件名模板：{template_labels.get(self.settings.filename_template, '[ID]标题')}\n"
            f"进度提示：{progress_labels.get(self.settings.progress_style, '关闭')}\n"
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
            # 贴表情先贴「开始」；贴不上（私聊、协议端不支持）就本次退回文本进度
            use_reaction = await self._react(event, "start")
            use_text_progress = self.settings.progress_style == "text" or (
                self.settings.progress_style == "emoji" and not use_reaction
            )
            succeeded = False
            try:
                if not use_reaction:
                    # 贴上了表情就等于「已开始」，不必再多一条消息
                    yield event.plain_result(
                        f"⏳ 开始下载 {channel.display_name} {comic_id}…"
                    )
                task = DownloadTask(
                    channel, self.settings, self.temp_root, self.save_root
                )
                try:
                    outcome = await task.run(
                        raw_id, self._progress_callback(event, use_text_progress)
                    )
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
                succeeded = True
            finally:
                if use_reaction:
                    await self._react(event, "done" if succeeded else "fail")

    async def _deliver(
        self, event: AstrMessageEvent, outcome: DownloadOutcome
    ) -> AsyncGenerator[MessageEventResult, None]:
        sendable = outcome.sendable_path
        summary = self._format_outcome(outcome) if self.settings.show_result_summary else ""

        if sendable is None:
            # 仅本地保存模式没有文件可发，摘要关掉也要告诉用户落在哪里
            yield event.plain_result(summary or f"已保存到本地：{outcome.source_dir}")
            return

        chain = [Comp.Plain(summary)] if summary else []
        chain.append(Comp.File(name=sendable.name, file=str(sendable)))
        yield event.chain_result(MessageChain(chain).chain)

    # ==================== 内部工具 ====================

    async def _react(self, event: AstrMessageEvent, status: str) -> bool:
        """按配置决定是否需要贴表情；关闭进度或选了文本进度时返回 False。"""
        if self.settings.progress_style != "emoji":
            return False
        return await set_progress_reaction(event, status)

    def _progress_callback(
        self, event: AstrMessageEvent, enabled: bool
    ) -> ProgressCallback | None:
        """文本进度：按百分比步进与最小间隔双重节流，避免长任务刷屏。"""
        if not enabled:
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

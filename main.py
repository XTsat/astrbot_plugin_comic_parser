"""漫画下载器 —— 多渠道按 ID 下载整本漫画，统一打包发送，支持密码加密。

命令层只做四件事：准入判断、渠道选择、进度转发、结果发送；
真正的下载在渠道里，打包与加密在 `core/packer.py`（上层统一）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    GroupFileResult,
    GroupFileUploader,
    ProgressState,
    build_channels,
    command_map,
    describe_channels,
    describe_links,
    resolve_link,
)
from .core.channel import ProgressCallback
from .core.domain_race import CACHE as DOMAIN_RACE_CACHE
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
    PROGRESS_STALL_INTERVAL,
    PROGRESS_STEP_PERCENT,
)
from .utils import format_size

# 贴表情（set_msg_emoji_like）与群文件上传（upload_group_file）都只有 OneBot(CQHTTP) 协议端提供
_ONEBOT_PLATFORMS = frozenset({"aiocqhttp"})
_REACTION_TIMEOUT = 5.0


@dataclass(slots=True)
class _RunningTask:
    """正在跑的下载在命令层的镜像：排队提示靠它说清「前面那本下到哪了」。"""

    label: str
    state: ProgressState | None = field(default=None)


def _event_call_action(
    event: AstrMessageEvent,
) -> Callable[..., Awaitable[Any]] | None:
    """取协议端的 call_action；拿不到（非 OneBot 平台、反向 ws 未连接）时返回 None。"""
    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None) or getattr(
        getattr(bot, "api", None), "call_action", None
    )
    return call_action if callable(call_action) else None


def _event_group_id(event: AstrMessageEvent) -> str:
    """消息发生的群号；私聊返回空串。"""
    try:
        value = event.get_group_id()
    except Exception as exc:  # 群文件是加分项，取不到群号就当私聊处理
        logger.debug(f"[{PLUGIN_NAME}] 取群号失败（忽略）: {exc}")
        return ""
    return str(value) if value else ""


async def set_progress_reaction(event: AstrMessageEvent, status: str) -> bool:
    """在触发命令的那条消息上贴表情，把「开始/完成/失败」压成一次轻量反馈。

    协议端不支持表情回应时返回 False，由调用方退回文本进度，不能因为提示失败影响下载。
    """
    if event.get_platform_name() not in _ONEBOT_PLATFORMS:
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

    call_action = _event_call_action(event)
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
        self.group_files = GroupFileUploader()
        self.channels = build_channels(self.settings)
        self._commands = command_map(self.channels)
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent_tasks)
        # 正在跑的任务：排队提示与「前面下到哪」都读它
        self._running: dict[int, _RunningTask] = {}
        self._task_seq = 0
        # 测速排名是上个配置周期的结论：配置改过（域名清单、代理、阈值）就该重新测
        DOMAIN_RACE_CACHE.clear()
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

        self._running.clear()

        if not self.temp_root.exists():
            return
        removed = 0
        for entry in self.temp_root.iterdir():
            Packer.cleanup(entry)
            removed += 1
        if removed:
            logger.info(f"[{PLUGIN_NAME}] 已清理 {removed} 个临时目录")

    # ==================== 命令 ====================

    # 推荐入口：整条作品链接自带站点，不用记命令；各渠道命令保留给「只有 ID」的场景
    @filter.command("comic")
    async def cmd_comic(
        self, event: AstrMessageEvent, link: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载链接指向的整本漫画，自动识别站点。用法：/comic <作品链接>"""
        async for result in self._handle_comic(event, link):
            yield result

    # 指令注册用站点全称，日常敲的缩写走别名；提示文案里展示的仍是缩写（见 cmd_help）
    @filter.command("jmcomic", alias={"jm"})
    async def cmd_jmcomic(
        self, event: AstrMessageEvent, comic_id: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载指定 ID 的 JM 漫画。用法：/jm <ID 或链接>"""
        async for result in self._handle_download(event, "jmcomic", comic_id):
            yield result

    @filter.command("nhentai", alias={"nh"})
    async def cmd_nhentai(
        self, event: AstrMessageEvent, comic_id: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载指定 ID 的 nhentai 作品。用法：/nh <ID 或链接>"""
        async for result in self._handle_download(event, "nhentai", comic_id):
            yield result

    @filter.command("picacg", alias={"pica"})
    async def cmd_picacg(
        self, event: AstrMessageEvent, comic_id: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载指定 ID 的哔咔漫画。用法：/pica <ID 或链接>"""
        async for result in self._handle_download(event, "picacg", comic_id):
            yield result

    @filter.command("hanime1", alias={"hm", "ha", "hanime"})
    async def cmd_hanime1(
        self, event: AstrMessageEvent, comic_id: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载指定 ID 的 Hanime1 漫画。用法：/hm <ID 或链接>（别名 /ha、/hanime）"""
        async for result in self._handle_download(event, "hanime1", comic_id):
            yield result

    @filter.command("wnacg", alias={"ssmh", "wn"})
    async def cmd_wnacg(
        self, event: AstrMessageEvent, comic_id: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载指定 ID 的紳士漫畫作品。用法：/ssmh <ID 或链接>（别名 /wn）"""
        async for result in self._handle_download(event, "wnacg", comic_id):
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
        if self.settings.upload_to_group_file:
            folder = self.settings.group_file_folder
            fallback = (
                "没有就新建"
                if self.settings.group_file_create_folder
                else "没有就传根目录"
            )
            group_file_desc = (
                f"下载完成后传进「{folder}」（{fallback}）"
                if folder
                else "下载完成后传进群文件根目录"
            )
        else:
            group_file_desc = "关闭"
        text = (
            f"📚 {PLUGIN_DISPLAY_NAME}\n"
            f"{describe_channels(self.channels)}\n\n"
            f"🔗 贴完整链接直接下：/comic <作品链接>\n"
            f"{describe_links(self.channels)}\n\n"
            f"打包格式：{self.settings.pack_format}（可选 {packs}）\n"
            f"打包密码：{'已设置' if self.settings.pack_password else '未设置'}\n"
            f"文件名模板：{template_labels.get(self.settings.filename_template, '[ID]标题')}\n"
            f"进度提示：{progress_labels.get(self.settings.progress_style, '关闭')}\n"
            f"群文件：{group_file_desc}\n"
            f"用法：/comic <作品链接>，或 /<渠道命令> <ID 或链接>\n"
            f"任一渠道命令后面贴别的站点的作品链接也按链接走，不会下错"
        )
        yield event.plain_result(text)

    # ==================== 下载流程 ====================

    async def _handle_comic(
        self, event: AstrMessageEvent, link: str
    ) -> AsyncGenerator[MessageEventResult, None]:
        """把完整链接路由到对应渠道；认不出站点时列出各渠道的链接示例。

        只认完整链接：裸 ID 无法判断站点，分享文本里的站点名也不可靠，那些走各渠道命令。
        """
        text = (link or "").strip()
        if not text:
            yield event.plain_result(
                f"用法：/comic <作品链接>\n{describe_links(self.channels)}"
            )
            return

        resolved = resolve_link(self.channels, text)
        if resolved is None:
            yield event.plain_result(
                "❌ 认不出这条链接属于哪个站点：请贴作品页的完整链接，"
                "或直接用对应渠道的命令（见 /漫画帮助）\n"
                f"{describe_links(self.channels)}"
            )
            return

        channel, _ = resolved
        async for result in self._handle_download(event, channel.command, text):
            yield result

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

        text = raw_id.strip()
        if not text:
            # 示例用缩写命令：用户日常敲的就是这个，全称只是注册名
            hint = channel.display_command
            yield event.plain_result(f"用法：/{hint} <ID 或链接>\n示例：/{hint} 123456")
            return

        # 链接自带站点，命令只是入口别名：命令与链接指向的渠道不一致时，以链接为准。
        # 否则 `/jm <nhentai 链接>` 会从链接里抠出一串数字当成 JM 的本子 ID，下到另一本
        resolved = resolve_link(self.channels, text)
        if resolved is not None:
            channel = resolved[0]

        try:
            channel.ensure_available()
            comic_id = channel.normalize_id(text)
        except PluginError as exc:
            yield event.plain_result(f"❌ {exc}")
            return

        debounce_key = f"{channel.name}:{comic_id}"
        if self.guard.is_duplicate(session_id, debounce_key):
            yield event.plain_result("⏳ 这个本子刚刚请求过，请稍后再试")
            return

        slot = self._begin_running(f"{channel.display_name} {comic_id}")
        result_state = "fail"
        try:
            if self._semaphore.locked():
                yield event.plain_result(self._queue_notice())

            async with self._semaphore:
                # 贴表情先贴「开始」；贴不上（私聊、协议端不支持）就本次退回文本进度
                use_reaction = await self._react(event, "start")
                use_text_progress = self.settings.progress_style == "text" or (
                    self.settings.progress_style == "emoji" and not use_reaction
                )
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
                        raw_id,
                        self._progress_callback(event, use_text_progress, slot),
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
                result_state = "ok"
        finally:
            self._end_running(slot)
            # 暂停/取消时表情停在「下载中」，别贴个锤子吓人
            if result_state != "paused":
                await self._react(event, "done" if result_state == "ok" else "fail")

    async def _deliver(
        self, event: AstrMessageEvent, outcome: DownloadOutcome
    ) -> AsyncGenerator[MessageEventResult, None]:
        sendable = outcome.sendable_path
        summary = self._format_outcome(outcome) if self.settings.show_result_summary else ""

        if sendable is None:
            # 仅本地保存模式没有文件可发，摘要关掉也要告诉用户落在哪里
            yield event.plain_result(summary or f"已保存到本地：{outcome.source_dir}")
            return

        upload = await self._upload_group_file(event, sendable)

        if upload is not None and upload.ok:
            # 群里已经出现这条文件消息了，不再补一句「已存入群文件」；只剩可选的下载摘要
            if summary:
                yield event.plain_result(summary)
            return

        lines = [summary] if summary else []
        if upload is not None:
            lines.append(f"⚠️ 群文件没传上去（{upload.error}），改为直接发文件")
        chain = [Comp.Plain("\n".join(lines))] if lines else []
        chain.append(Comp.File(name=sendable.name, file=str(sendable)))
        yield event.chain_result(MessageChain(chain).chain)

    async def _upload_group_file(
        self, event: AstrMessageEvent, path: Path
    ) -> GroupFileResult | None:
        """按配置把成品传进当前群的群文件；返回 None 表示这次不适用（关闭 / 私聊 / 协议端不支持）。"""
        if not self.settings.upload_to_group_file:
            return None
        if event.get_platform_name() not in _ONEBOT_PLATFORMS:
            logger.info(f"[{PLUGIN_NAME}] 当前协议端不支持群文件上传，跳过")
            return None
        group_id = _event_group_id(event)
        if not group_id:
            logger.debug(f"[{PLUGIN_NAME}] 私聊没有群文件，跳过上传")
            return None
        call_action = _event_call_action(event)
        if call_action is None:
            logger.info(f"[{PLUGIN_NAME}] 协议端没有 call_action，跳过群文件上传")
            return None

        result = await self.group_files.upload(
            call_action,
            group_id,
            path,
            self.settings.group_file_folder,
            self.settings.group_file_create_folder,
        )
        if result.ok:
            logger.info(
                f"[{PLUGIN_NAME}] 已上传群文件：{result.location} / {path.name}"
            )
        return result

    # ==================== 内部工具 ====================

    async def _react(self, event: AstrMessageEvent, status: str) -> bool:
        """按配置决定是否需要贴表情；关闭进度或选了文本进度时返回 False。"""
        if self.settings.progress_style != "emoji":
            return False
        return await set_progress_reaction(event, status)

    def _progress_callback(
        self, event: AstrMessageEvent, enabled: bool, slot: int
    ) -> ProgressCallback:
        """下载进度：**始终**更新任务登记表，只在开启时才往聊天里发。

        聊天侧双阈值节流：百分比每进 25% 说一句；张数长时间不涨（慢节点上一张图能拖
        好几分钟）也照样隔一阵报一次「仍在下载」，免得用户以为掉线——这就是心跳那一半。
        """
        box = {"last_pct": -1, "sent_at": time.monotonic()}

        async def on_progress(state: ProgressState) -> None:
            running = self._running.get(slot)
            if running is not None:
                running.state = state
            if not enabled:
                return

            now = time.monotonic()
            percent = state.percent
            if percent is None:
                if now - box["sent_at"] < PROGRESS_MIN_INTERVAL:
                    return
                text = f"📥 下载中，已完成 {state.counter}"
            else:
                stepped = percent - box["last_pct"] >= PROGRESS_STEP_PERCENT
                stalled = now - box["sent_at"] >= PROGRESS_STALL_INTERVAL
                if not stepped and not stalled:
                    return
                box["last_pct"] = percent
                text = f"📥 下载中 {percent}%（{state.headline}）"
            box["sent_at"] = now

            try:
                await event.send(MessageChain([Comp.Plain(text)]))
            except Exception as exc:  # 进度发不出去不影响下载本身
                logger.debug(f"[{PLUGIN_NAME}] 发送进度失败（忽略）: {exc}")

        return on_progress

    # ---- 任务登记：排队提示要说清「前面在跑什么、跑到哪」 ----

    def _begin_running(self, label: str) -> int:
        self._task_seq += 1
        self._running[self._task_seq] = _RunningTask(label=label)
        return self._task_seq

    def _end_running(self, slot: int) -> None:
        self._running.pop(slot, None)

    def _queue_notice(self) -> str:
        """排队提示：带上前面那一本的实时进度，用户才知道要等多久。"""
        lines = ["⏳ 前面的下载还没结束，你的请求已排队"]
        for running in self._running.values():
            state = running.state
            if state is None:
                lines.append(f" · {running.label}：正在取漫画信息…")
                continue
            lines.append(
                f" · {running.label}：{state.headline}"
                f" · {format_size(state.bytes_downloaded)}"
                f" · 已用 {state.elapsed:.0f}s"
            )
        minutes = max(1, round(self.settings.download_timeout / 60))
        lines.append(f"⏱ 单本上限 {minutes} 分钟，前一个任务结束后自动开始")
        return "\n".join(lines)

    def _format_outcome(self, outcome: DownloadOutcome) -> str:
        # 摘要里写明实际走的渠道：命令名只是入口，贴别的站点链接会把它带到对应站点
        channel = self.channels.get(outcome.meta.channel)
        label = channel.display_name if channel else outcome.meta.channel
        lines = [f"✅ {label} · {outcome.meta.display_title}"]
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

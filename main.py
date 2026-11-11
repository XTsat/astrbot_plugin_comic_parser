"""漫画下载器 —— 多渠道按 ID 下载整本漫画 / 在聊天里搜索，统一打包发送，支持密码加密。

命令层只做四件事：准入判断、渠道选择、进度转发、结果发送；搜索另走一条轻量链路，
真正的下载在渠道里，打包与加密在 `core/packer.py`（上层统一）。
"""

from __future__ import annotations

import asyncio
import json
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
try:  # 插件视图 Web API 是可选能力：老版本没有这个模块也要能正常加载插件
    from astrbot.api import web as plugin_web
except ImportError:  # pragma: no cover - 只在老版本 AstrBot 上走到
    plugin_web = None

from .core import (
    AccessGuard,
    CoverResult,
    CoverService,
    DownloadOutcome,
    DownloadTask,
    GroupFileResult,
    GroupFileUploader,
    ProgressState,
    ModelWeights,
    PanelService,
    RankingService,
    SearchService,
    create_detector,
    ensure_onnxruntime,
    build_channels,
    command_map,
    describe_channels,
    describe_links,
    describe_websites,
    resolve_link,
)
from .core.channel import (
    INTERRUPT_RELOAD,
    BaseChannel,
    CancelToken,
    ProgressCallback,
)
from .core.domain_race import CACHE as DOMAIN_RACE_CACHE
from .core.packer import Packer
from .core.ranking import board_label, parse_board
from .domain import (
    CoverCensorError,
    DownloadCancelled,
    DownloadPaused,
    PanelError,
    PluginError,
    PluginSettings,
    SearchQueryError,
)
from .domain.constants import (
    PACK_FORMAT_ALIASES,
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
    LAST_REQUEST_FILENAME,
    LAST_REQUEST_LIMIT,
    PROGRESS_STEP_PERCENT,
    RESUME_DIRNAME,
    RETRY_WORDS,
    HELP_WORDS,
    SITE_WORDS,
    STATUS_WORDS,
    SEARCH_MAX_KEYWORD_LENGTH,
    SEARCH_NEXT_WORDS,
    SEARCH_PREV_WORDS,
    SEARCH_RESULT_TTL,
    RANKING_BOARDS,
    RANKING_WORDS,
    SEARCH_WORDS,
    STOP_ALL_WORDS,
    STOP_WORDS,
    PANEL_MAX_UPLOAD_BYTES,
    PANEL_MODEL_UPLOAD_MAX_BYTES,
)
from .utils import describe_pack_format, format_size, split_pack_format

# 贴表情（set_msg_emoji_like）与群文件上传（upload_group_file）都只有 OneBot(CQHTTP) 协议端提供
_ONEBOT_PLATFORMS = frozenset({"aiocqhttp"})
_REACTION_TIMEOUT = 5.0


@dataclass(slots=True)
class _RunningTask:
    """正在跑（或正在排队）的下载在命令层的镜像。

    排队提示靠它说清「前面那本下到哪了」，`/comic stop` 靠它找到该取消谁。
    """

    label: str
    session_id: str
    cancel: CancelToken
    state: ProgressState | None = field(default=None)
    # 抢到并发名额才算真在跑；在此之前都是排队，状态查询与排队提示都靠它区分
    started: bool = False
    cancelled: bool = False


@dataclass(slots=True)
class _LastRequest:
    """本会话最近一次真正开始下载的请求，`/comic retry` 用它原样重放。

    存的是**规范化之后的 ID**：搜索序号会过期、作品链接可能带分享尾巴，重放时再解析一遍
    只会多出失败点，不如记已经能直接下手的那个值。
    """

    command: str  # 渠道命令名（不带斜杠）
    comic_id: str
    pack_format: str | None = None


def _event_call_action(
    event: AstrMessageEvent,
) -> Callable[..., Awaitable[Any]] | None:
    """取协议端的 call_action；拿不到（非 OneBot 平台、反向 ws 未连接）时返回 None。"""
    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None) or getattr(
        getattr(bot, "api", None), "call_action", None
    )
    return call_action if callable(call_action) else None


def _session_tag(session_id: str) -> str:
    """会话的可读标签：取统一消息来源的末段（群号 / 用户号），取不到就回原串。"""
    tail = session_id.rsplit(":", 1)[-1] if session_id else ""
    return tail or "未知会话"


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
        self.search_service = SearchService(self.channels, self.settings)
        self.ranking_service = RankingService(self.channels, self.settings)
        # 本会话最近一次列表是谁出的（search / ranking）：序号下载与翻页按它决定先问谁
        self._index_source: dict[str, str] = {}
        # 封面：权重默认不随插件分发，缺失时按配置自动下载（见 initialize）
        self.models_dir = Path(__file__).resolve().parent / "models"
        self.model_weights = ModelWeights(
            self.models_dir,
            preset=self.settings.nsfw_model_preset,
            auto_download=self.settings.nsfw_auto_download,
            download_url=self.settings.nsfw_model_url,
            proxy=self.settings.proxy,
        )
        self.nsfw_detector = create_detector(
            self.model_weights,
            threshold=self.settings.nsfw_threshold,
            imgsz=self.settings.nsfw_imgsz,
        )
        self.cover_service = CoverService(
            self.nsfw_detector,
            self.temp_root,
            send_caption=self.settings.cover_caption,
            censor=self.settings.cover_censor,
            censor_style=self.settings.cover_censor_style,
            censor_block=self.settings.cover_censor_block,
            censor_padding=self.settings.cover_censor_padding,
            full_censor_area=self.settings.cover_full_censor_area,
        )
        # 插件视图 views/panel：拿同一套检测器与打码函数试封面打码，参数由页面自带
        self.panel = PanelService(
            self.models_dir,
            self.temp_root,
            preset=self.settings.nsfw_model_preset,
            threshold=self.settings.nsfw_threshold,
            imgsz=self.settings.nsfw_imgsz,
            style=self.settings.cover_censor_style,
            block=self.settings.cover_censor_block,
            padding=self.settings.cover_censor_padding,
            full_area=self.settings.cover_full_censor_area,
            auto_download=self.settings.nsfw_auto_download,
            download_url=self.settings.nsfw_model_url,
            proxy=self.settings.proxy,
            plugin_info={
                "name": PLUGIN_NAME,
                "display_name": PLUGIN_DISPLAY_NAME,
                "version": PLUGIN_VERSION,
                "data_dir": str(self.data_dir),
                "models_dir": str(self.models_dir),
            },
            channels_provider=lambda: self.channels,
            tasks_provider=self._panel_tasks,
            config_provider=lambda: self.config,
            config_writer=self._panel_save_config,
            schema_path=Path(__file__).resolve().parent / "_conf_schema.json",
        )
        self._register_panel_apis()
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent_tasks)
        # 正在跑的任务：排队提示与「前面下到哪」都读它
        self._running: dict[int, _RunningTask] = {}
        self._task_seq = 0
        # 每个会话记一条最近的下载请求，供 `/comic retry` 原样重放。
        # 落盘：重载会重建实例，只放内存的话重载中断后 `/comic retry` 就查不到记录了
        self._last_request: dict[str, _LastRequest] = self._load_last_requests()
        # 测速排名是上个配置周期的结论：配置改过（域名清单、代理、阈值）就该重新测
        DOMAIN_RACE_CACHE.clear()
        self._clean_temp_on_start()

        logger.info(
            f"[{PLUGIN_NAME}] 已加载渠道: {', '.join(self.channels) or '无'}；"
            f"保存目录: {self.save_root}"
        )

    async def initialize(self) -> None:
        """插件激活时准备打码运行时（依赖 + 权重）。

        放后台任务里：几十 MB 的安装与下载不该拖住插件加载，也不该挡住其它命令。
        打码默认关闭，只有「发封面」与「给封面打码」都开着才需要准备。
        """
        if not self._needs_censor_runtime():
            return
        logger.info(f"[{PLUGIN_NAME}] 封面打码已开启，检查运行时依赖与权重")
        self._model_task = asyncio.create_task(self._prepare_censor_runtime())

    def _needs_censor_runtime(self) -> bool:
        """打码运行时是否用得上：发封面且开了打码。

        权重是否自动下载由「缺失时自动下载权重」单独决定，但依赖（onnxruntime）只要
        打算打码就得有，否则那张封面对不上检测、只能按未打码处理而不发。
        """
        return bool(self.settings.cover_send and self.settings.cover_censor)

    async def _prepare_censor_runtime(self) -> None:
        """按需补齐打码运行时：先确保 onnxruntime，再按配置取权重。

        每次插件加载（含重载）都会走一遍，依赖已在位时只是两次 find_spec，开销可忽略。
        任何一步失败都只记日志，不影响下载与打包。
        """
        if not await ensure_onnxruntime():
            logger.warning(
                f"[{PLUGIN_NAME}] ⚠️ 没有可用的 onnxruntime，本次加载不做封面打码；"
                "装好后重载插件即可"
            )
            return
        # 依赖到位后会话才建得起来，清掉旧状态让它重新加载
        self.nsfw_detector.reset()

        if not self.settings.nsfw_auto_download:
            return
        try:
            path, downloaded = await self.model_weights.ensure()
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 预取 NSFW 权重失败: {exc}")
            return

        if downloaded and path is not None:
            self.nsfw_detector.reset()
            logger.info(f"[{PLUGIN_NAME}] NSFW 权重已就绪：{path.name}")

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
        """重载/卸载时结束进行中的下载：先标好中断原因与进度，再按配置清理临时目录。

        下载跑在线程池里，插件卸载并不会让它停，结果也无处投递——留着只会继续占带宽与
        磁盘，所以这里取消。取消前要先把「为什么停、停在哪」写进令牌：目录随即会被清掉，
        任务层再去数只能数出 0，那样给用户的报告就是假的。
        """
        for running in self._running.values():
            running.cancel.cancel(INTERRUPT_RELOAD, running.state)

        for channel in self.channels.values():
            for summary in channel.cancel_downloads():
                logger.info(f"[{PLUGIN_NAME}] 重载中断下载：{summary}")

        self._running.clear()
        self.panel.close()

        if not self.temp_root.exists():
            return
        # 「启动时清理临时目录」开着说明下次加载会把断点一起删掉，留着也没用；关着才留
        keep_breakpoints = (
            self.settings.resume_ttl > 0 and not self.settings.clean_temp_on_start
        )
        removed = 0
        for entry in self.temp_root.iterdir():
            if keep_breakpoints and entry.name == RESUME_DIRNAME:
                continue
            Packer.cleanup(entry)
            removed += 1
        if removed:
            logger.info(f"[{PLUGIN_NAME}] 已清理 {removed} 个临时目录")
        if keep_breakpoints:
            logger.info(f"[{PLUGIN_NAME}] 断点已保留，重载后可接着下")

    # ==================== 插件视图：漫画下载器面板 ====================

    def _register_panel_apis(self) -> None:
        """注册 views/panel（漫画下载器面板）需要的接口。

        老版本 AstrBot 没有 register_web_api：这里只跳过这一个视图，插件其余功能照旧。
        """
        register_api = getattr(self.context, "register_web_api", None)
        if plugin_web is None or not callable(register_api):
            logger.info(
                f"[{PLUGIN_NAME}] 当前 AstrBot 不支持插件视图 Web API，插件面板不可用"
            )
            return

        for route, handler, methods, desc in (
            (
                f"/{PLUGIN_NAME}/panel/describe",
                self.api_panel_describe,
                ["GET"],
                "插件面板：首屏元信息（运行时、档位、渠道、默认参数）",
            ),
            (
                f"/{PLUGIN_NAME}/panel/upload",
                self.api_panel_upload,
                ["POST"],
                "插件面板：上传待测图片",
            ),
            (
                f"/{PLUGIN_NAME}/panel/score",
                self.api_panel_score,
                ["POST"],
                "插件面板：露骨度评分",
            ),
            (
                f"/{PLUGIN_NAME}/panel/censor",
                self.api_panel_censor,
                ["POST"],
                "插件面板：按框打码并返回预览",
            ),
            (
                f"/{PLUGIN_NAME}/panel/download",
                self.api_panel_download,
                ["GET"],
                "插件面板：下载最近一次打码结果",
            ),
            (
                f"/{PLUGIN_NAME}/panel/settings",
                self.api_panel_settings,
                ["GET", "POST"],
                "插件面板：读取 / 保存插件配置",
            ),
            (
                f"/{PLUGIN_NAME}/panel/status",
                self.api_panel_status,
                ["GET"],
                "插件面板：运行时、权重、渠道、任务与磁盘状态",
            ),
            (
                f"/{PLUGIN_NAME}/panel/images/delete",
                self.api_panel_image_delete,
                ["POST"],
                "插件面板：释放一张暂存图片",
            ),
            (
                f"/{PLUGIN_NAME}/panel/channels",
                self.api_panel_channels,
                ["GET"],
                "插件面板：可用渠道清单（是否启用、能否取封面）",
            ),
            (
                f"/{PLUGIN_NAME}/panel/channels/fetch",
                self.api_panel_channels_fetch,
                ["POST"],
                "插件面板：按渠道 + 作品 ID/链接取元信息与封面",
            ),
            (
                f"/{PLUGIN_NAME}/panel/models",
                self.api_panel_models,
                ["GET"],
                "插件面板：模型清单（类别、大小、来源、许可、是否已装）",
            ),
            (
                f"/{PLUGIN_NAME}/panel/models/download",
                self.api_panel_model_download,
                ["POST"],
                "插件面板：下载指定档位的权重",
            ),
            (
                f"/{PLUGIN_NAME}/panel/models/status",
                self.api_panel_model_status,
                ["GET"],
                "插件面板：权重下载进度与各档位状态",
            ),
            (
                f"/{PLUGIN_NAME}/panel/models/upload",
                self.api_panel_model_upload,
                ["POST"],
                "插件面板：手动上传权重文件（先暂存）",
            ),
            (
                f"/{PLUGIN_NAME}/panel/models/install",
                self.api_panel_model_install,
                ["POST"],
                "插件面板：把上传的权重装成指定档位",
            ),
            (
                f"/{PLUGIN_NAME}/panel/models/delete",
                self.api_panel_model_delete,
                ["POST"],
                "插件面板：删除指定档位已下载的权重",
            ),
        ):
            register_api(route, handler, methods, desc)

    async def api_panel_describe(self) -> Any:
        """页面首屏：能选哪些档位、各项默认值是多少。"""
        return plugin_web.json_response({"status": "ok", "data": self.panel.describe()})

    async def api_panel_upload(self) -> Any:
        """收一张图进暂存表，返回 id 与原始尺寸。

        体积自己卡：插件扩展路由没有统一的体积上限，页面传来的东西一律不可信。
        """
        files = await plugin_web.request.files()
        upload = files.get("file")
        if upload is None:
            return plugin_web.error_response("没有收到图片（表单字段名固定为 file）", status_code=400)

        raw = await upload.read(PANEL_MAX_UPLOAD_BYTES + 1)
        if len(raw) > PANEL_MAX_UPLOAD_BYTES:
            limit_mb = PANEL_MAX_UPLOAD_BYTES // 1024 // 1024
            return plugin_web.error_response(f"图片超过 {limit_mb}MB，换张小一点的", status_code=413)

        try:
            # 落盘 + 校验尺寸都要读整张图，丢线程里，别卡住事件循环
            image = await asyncio.to_thread(
                self.panel.add_image, raw, Path(upload.filename or "").suffix.lower()
            )
        except PanelError as exc:
            return plugin_web.error_response(str(exc), status_code=400)
        return plugin_web.json_response(
            {
                "status": "ok",
                "data": {
                    "image_id": image.id,
                    "width": image.width,
                    "height": image.height,
                    "bytes": image.size,
                },
            }
        )

    async def api_panel_score(self) -> Any:
        """按页面给的档位/阈值/输入尺寸评分。"""
        payload = await plugin_web.request.json(default={})
        if not isinstance(payload, dict):
            return plugin_web.error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            data = await self.panel.score(
                str(payload.get("image_id") or ""),
                preset=str(payload.get("preset") or ""),
                threshold=payload.get("threshold"),
                imgsz=payload.get("imgsz"),
            )
        except PanelError as exc:
            return plugin_web.error_response(str(exc), status_code=400)
        return plugin_web.json_response({"status": "ok", "data": data})

    async def api_panel_censor(self) -> Any:
        """按页面给的框与样式打码，返回可预览的 data URL。"""
        payload = await plugin_web.request.json(default={})
        if not isinstance(payload, dict):
            return plugin_web.error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            data = await self.panel.censor(
                str(payload.get("image_id") or ""),
                boxes=payload.get("boxes"),
                style=payload.get("style"),
                block=payload.get("block"),
                padding=payload.get("padding"),
                full_area=payload.get("full_area"),
            )
        except PanelError as exc:
            return plugin_web.error_response(str(exc), status_code=400)
        return plugin_web.json_response({"status": "ok", "data": data})

    def _panel_tasks(self) -> dict[str, Any]:
        """面板「状态」页要的任务快照：真正在跑的几个、还在排队的几个。"""
        items = []
        for task in self._running.values():
            state = task.state
            items.append(
                {
                    "label": task.label,
                    "session_id": task.session_id,
                    "started": task.started,
                    "done": getattr(state, "done", 0) if state is not None else 0,
                    "total": getattr(state, "total", 0) if state is not None else 0,
                }
            )
        running = [item for item in items if item["started"]]
        return {
            "running_count": len(running),
            "queue_count": len(items) - len(running),
            "running": running[:8],
        }

    def _panel_save_config(self, values: dict[str, Any]) -> None:
        """把面板提交的 ``{"组.键": 值}`` 合并进插件配置并落盘。

        分组的键不存在时新建：老配置可能还没有这个分组，直接写会丢掉整组。
        """
        for path, value in values.items():
            parts = [part for part in str(path).split(".") if part]
            if len(parts) < 2:
                continue
            node: Any = self.config
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):  # 老配置可能还没有这个分组
                    child = {}
                    node[part] = child
                node = child
            node[parts[-1]] = value
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()

    async def api_panel_settings(self) -> Any:
        """面板「设置」页：schema 摊平后的表单 + 当前值。"""
        if plugin_web.request.method == "POST":
            payload = await plugin_web.request.json(default={})
            if not isinstance(payload, dict):
                return plugin_web.error_response("请求体必须是 JSON 对象", status_code=400)
            try:
                data = self.panel.save_settings(payload.get("values"))
            except PanelError as exc:
                return plugin_web.error_response(str(exc), status_code=400)
            return plugin_web.json_response({"status": "ok", "data": data})
        return plugin_web.json_response({"status": "ok", "data": self.panel.settings()})

    async def api_panel_status(self) -> Any:
        """面板「状态」页的全景快照。"""
        return plugin_web.json_response({"status": "ok", "data": self.panel.status()})

    async def api_panel_channels(self) -> Any:
        """渠道清单：页面据此决定「渠道测试」能不能用、支持哪些能力。"""
        return plugin_web.json_response({"status": "ok", "data": {"channels": self.panel.channels()}})

    async def api_panel_image_delete(self) -> Any:
        """页面删掉一张图时，把后端暂存的那份也释放掉（本来就不在也不算错）。"""
        payload = await plugin_web.request.json(default={})
        if not isinstance(payload, dict):
            return plugin_web.error_response("请求体必须是 JSON 对象", status_code=400)
        removed = self.panel.remove_image(payload.get("image_id"))
        return plugin_web.json_response({"status": "ok", "data": {"removed": removed}})

    async def api_panel_channels_fetch(self) -> Any:
        """按渠道取一本作品的元信息与封面（封面进面板的图片暂存表）。"""
        payload = await plugin_web.request.json(default={})
        if not isinstance(payload, dict):
            return plugin_web.error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            data = await self.panel.fetch_from_channel(
                payload.get("channel"), payload.get("text")
            )
        except PanelError as exc:
            return plugin_web.error_response(str(exc), status_code=400)
        return plugin_web.json_response({"status": "ok", "data": data})

    async def api_panel_models(self) -> Any:
        """「模型下载」页的清单：全部档位 + 下载进度快照。"""
        return plugin_web.json_response(
            {
                "status": "ok",
                "data": {
                    "models_dir": str(self.panel.models_dir),
                    "models": self.panel.models(),
                    "download": self.panel.model_status(),
                },
            }
        )

    async def api_panel_model_download(self) -> Any:
        """下载页面选中的权重档位；下载在后台跑，进度靠 status 接口轮询。"""
        payload = await plugin_web.request.json(default={})
        if not isinstance(payload, dict):
            return plugin_web.error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            data = await self.panel.download_model(
                payload.get("preset"),
                payload.get("mode"),
                payload.get("custom_url"),
            )
        except PanelError as exc:
            return plugin_web.error_response(str(exc), status_code=400)
        return plugin_web.json_response({"status": "ok", "data": data})

    async def api_panel_model_status(self) -> Any:
        """当前下载进度 + 各档位状态。"""
        return plugin_web.json_response({"status": "ok", "data": self.panel.model_status()})

    async def api_panel_model_upload(self) -> Any:
        """收一个用户手动上传的 .onnx：先暂存，再由页面挑档位安装。

        体积自己卡（插件扩展路由没有统一上限）；只认得出来的整文件，太小的一律拒掉。
        """
        files = await plugin_web.request.files()
        upload = files.get("file")
        if upload is None:
            return plugin_web.error_response("没有收到权重文件（表单字段名固定为 file）", status_code=400)

        raw = await upload.read(PANEL_MODEL_UPLOAD_MAX_BYTES + 1)
        try:
            data = self.panel.stash_uploaded_model(raw, upload.filename or "")
        except PanelError as exc:
            return plugin_web.error_response(str(exc), status_code=400)
        return plugin_web.json_response({"status": "ok", "data": data})

    async def api_panel_model_install(self) -> Any:
        """把暂存的上传权重装成某个档位。"""
        payload = await plugin_web.request.json(default={})
        if not isinstance(payload, dict):
            return plugin_web.error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            data = self.panel.install_uploaded_model(
                payload.get("upload_id"), payload.get("preset")
            )
        except PanelError as exc:
            return plugin_web.error_response(str(exc), status_code=400)
        return plugin_web.json_response({"status": "ok", "data": data})

    async def api_panel_model_delete(self) -> Any:
        """删掉某个档位已下载的权重。"""
        payload = await plugin_web.request.json(default={})
        if not isinstance(payload, dict):
            return plugin_web.error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            data = self.panel.delete_model(payload.get("preset"))
        except PanelError as exc:
            return plugin_web.error_response(str(exc), status_code=400)
        return plugin_web.json_response({"status": "ok", "data": data})

    async def api_panel_download(self) -> Any:
        """把该图最近一次打码结果当文件发出去（视图走 bridge.download）。"""
        image_id = str(plugin_web.request.query.get("image_id") or "")
        try:
            path, filename = self.panel.download_path(image_id)
        except PanelError as exc:
            return plugin_web.error_response(str(exc), status_code=404)
        mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        return plugin_web.file_response(path, filename=filename, content_type=mime)

    # ==================== 命令 ====================

    # 推荐入口：整条作品链接自带站点，不用记命令；各渠道命令保留给「只有 ID」的场景
    @filter.command("comic")
    async def cmd_comic(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载链接指向的整本漫画，自动识别站点；`/comic sou 关键词` 搜所有渠道。"""
        async for result in self._handle_comic(event, self._command_args(event)):
            yield result

    # 指令注册用站点全称，日常敲的缩写走别名；提示文案里展示的仍是缩写（见 cmd_help）
    @filter.command("jmcomic", alias={"jm"})
    async def cmd_jmcomic(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载 JM 漫画；`/jm sou 关键词` 只搜 JM。"""
        async for result in self._handle_download(
            event, "jmcomic", self._command_args(event)
        ):
            yield result

    @filter.command("nhentai", alias={"nh"})
    async def cmd_nhentai(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载 nhentai 作品；`/nh sou 关键词` 只搜 nhentai。"""
        async for result in self._handle_download(
            event, "nhentai", self._command_args(event)
        ):
            yield result

    @filter.command("picacg", alias={"pica"})
    async def cmd_picacg(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载哔咔漫画；`/pica sou 关键词` 只搜哔咔（需已填账号）。"""
        async for result in self._handle_download(
            event, "picacg", self._command_args(event)
        ):
            yield result

    @filter.command("hanime1", alias={"hm", "ha", "hanime"})
    async def cmd_hanime1(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载 Hanime1 漫画；`/hm sou 关键词` 只搜 Hanime1。"""
        async for result in self._handle_download(
            event, "hanime1", self._command_args(event)
        ):
            yield result

    @filter.command("wnacg", alias={"ssmh", "wn"})
    async def cmd_wnacg(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载紳士漫畫作品；`/ssmh sou 关键词` 只搜紳士漫畫。"""
        async for result in self._handle_download(
            event, "wnacg", self._command_args(event)
        ):
            yield result

    @filter.command("ehentai", alias={"eh"})
    async def cmd_ehentai(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """下载 E-Hentai 作品；`/eh sou 关键词` 只搜 E-Hentai。"""
        async for result in self._handle_download(
            event, "ehentai", self._command_args(event)
        ):
            yield result

    # 说明与站点速查都有两个入口：`/comic help`、`/comic site` 是标准写法，
    # 老命令名继续留着；两条统一走下面的渲染函数，文案只维护一处
    @filter.command("漫画帮助", alias={"comichelp"})
    async def cmd_help(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查看可用渠道与用法（与 `/comic help` 同一份文案）。"""
        yield event.plain_result(self._render_help_text())

    def _render_help_text(self) -> str:
        """拼出完整说明正文。"""
        packs = "、".join(PACK_FORMATS)
        progress_labels = {"emoji": "贴表情", "text": "文本进度", "none": "关闭"}
        template_labels = {
            "id_short_title": "[简写ID]标题",
            "id_short_title_pw": "[简写ID]标题 + 密码",
            "id_title": "[ID]标题",
            "id_title_pw": "[ID]标题 + 密码",
            "title_id": "标题[ID]",
            "title_id_pw": "标题[ID] + 密码",
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
        if self.settings.segment_timeout > 0:
            minutes = max(1, round(self.settings.segment_timeout / 60))
            resume_desc = f"每 {minutes} 分钟先暂停，再发同一条命令接着下"
        else:
            resume_desc = "不分段，一次下完"
        searchable = [
            channel
            for channel in self.channels.values()
            if channel.enabled and channel.supports_search
        ]
        if not self.settings.search_enabled:
            search_desc = "未启用（在插件配置的「搜索」里打开）"
        elif searchable:
            search_desc = (
                f"/comic sou <关键词> 一次搜 {len(searchable)} 个渠道；"
                "也可 /<渠道命令> sou <关键词> 只搜一个"
            )
        else:
            search_desc = "当前没有支持搜索的渠道"
        search_filter_desc = (
            "搜索筛选：tag:标签 author:作者 actor:角色 work:作品 "
            "parody:原作 group:社团 language:语言\n"
            "（前缀前加 - 表示排除，值含空格用引号；JM 支持 tag/author/actor/work，"
            "E-Hentai 支持全部（tag/work 为近似），其余渠道按关键词搜索、排除不生效）\n"
            if self.settings.search_enabled and searchable
            else ""
        )
        if self.settings.cover_send:
            cover_desc = "下载完成后先发一张"
            if self.settings.cover_censor:
                cover_desc += "（已打码"
                cover_desc += "，附检测说明" if self.settings.cover_caption else ""
                cover_desc += "）"
                model_label = CoverService.describe_model(
                    self.nsfw_detector, self.settings.nsfw_model_preset
                )
                cover_desc += f"，评分模型：{model_label}"
        else:
            cover_desc = "关闭"
        rankable = [
            channel
            for channel in self.channels.values()
            if channel.enabled and channel.supports_ranking
        ]
        if not self.settings.ranking_enabled:
            ranking_desc = "未启用（在插件配置的「排行榜」里打开）"
        elif rankable:
            ranking_desc = (
                f"/comic 排行 [时间窗] 一次聚合 {len(rankable)} 个渠道；"
                "也可 /<渠道命令> 排行 只看一个"
            )
        else:
            ranking_desc = "当前没有支持排行榜的渠道"
        if self.settings.ranking_enabled and rankable:
            ranking_windows = "、".join(
                board_label(item) for item in RANKING_BOARDS
            )
            ranking_filter_desc = (
                f"榜单时间窗：{ranking_windows}"
                f"（默认{board_label(self.settings.ranking_default_board)}）；"
                "各渠道只开放自己有的那几档，缺的会在结果里说明\n"
            )
        else:
            ranking_filter_desc = ""
        index_minutes = max(1, SEARCH_RESULT_TTL // 60)
        index_hint = (
            f"回序号下载：搜索结果或榜单出来后，直接发 /comic <序号>（如 /comic 1），"
            f"也可以 /<渠道命令> <序号>；翻页用 /comic 下一页、"
            f"/<渠道命令> 上一页；序号在 {index_minutes} 分钟内有效\n"
            if self.settings.search_enabled and searchable
            else ""
        )
        text = (
            f"📚 {PLUGIN_DISPLAY_NAME}\n"
            f"{describe_channels(self.channels)}\n\n"
            f"🔗 支持的站点与作品页链接示意（<ID> 处替换成作品 ID）：\n"
            f"{describe_links(self.channels)}\n\n"
            f"🌐 支持的网站与作品页链接示意（找 ID 用）：/comic site\n\n"
            f"打包格式：{self.settings.pack_format}（可选 {packs}）\n"
            f"单次改格式：命令末尾加格式词，如 /jm 123456 pdf、/comic <链接> long_img，"
            f"只对这一次生效（也认中文 压缩包 / 文档 / 长图 / 本地）\n"
            f"打包密码：{'已设置' if self.settings.pack_password else '未设置'}\n"
            f"文件名模板：{template_labels.get(self.settings.filename_template, '[简写ID]标题')}\n"
            f"进度提示：{progress_labels.get(self.settings.progress_style, '关闭')}\n"
            f"封面：{cover_desc}\n"
            f"下载分段：{resume_desc}\n"
            f"群文件：{group_file_desc}\n"
            f"用法：/comic <作品链接> [格式]，或 /<渠道命令> <ID 或链接> [格式]\n"
            f"搜索：{search_desc}\n"
            f"{search_filter_desc}"
            f"排行榜：{ranking_desc}\n"
            f"{ranking_filter_desc}"
            f"{index_hint}"
            f"取消下载：/comic stop（管理员可用 /comic stop 全部）\n"
            f"接着下上一次：/comic retry（暂停、下载中断或失败后发这一条）\n"
            f"查看排队：/comic status（管理员可用 /comic status 全部）\n"
            f"任一渠道命令后面贴别的站点的作品链接也按链接走，不会下错\n"
            f"本说明：/comic help（也认 /漫画帮助）；站点速查：/comic site（也认 /漫画网址）"
        )
        return text

    @filter.command("漫画网站", alias={"comicsites", "comicsite"})
    async def cmd_sites(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """列出支持渠道的官网与作品页链接示意（与 `/comic site` 同一份文案）。"""
        yield event.plain_result(self._render_sites_text())

    def _render_sites_text(self) -> str:
        """拼出站点速查正文。"""
        return (
            f"🌐 支持的漫画网站（共 {len(self.channels)} 个渠道）\n\n"
            f"{describe_websites(self.channels)}\n\n"
            f"到站内搜到作品后打开作品页，把 <ID> 替换成链接里的 ID，\n"
            f"再发给 /comic 就能下载；也可以直接发完整作品页链接\n"
            f"不想离开聊天：/comic sou <关键词> 一次搜所有支持搜索的渠道\n"
            f"站点域名打不开时，可在渠道配置里换成其它可用域名"
        )

    # ==================== 下载流程 ====================

    async def _handle_comic(
        self, event: AstrMessageEvent, args: str
    ) -> AsyncGenerator[MessageEventResult, None]:
        """`/comic <链接>` 下载，`/comic sou <关键词>` 搜索所有渠道。

        链接下载只认完整链接：裸 ID 无法判断站点，分享文本里的站点名也不可靠，那些走各渠道命令。
        裸数字先按「上一次搜索结果的序号」解释，免得全渠道搜索后还得先记住 ID。
        """
        # 全角空格/多空格先压成单个半角空格，`/comic　sou　名字` 也能识别
        text = " ".join((args or "").split())
        if not text:
            yield event.plain_result(
                "用法：/comic <作品链接> [格式]、/comic <序号> [格式]，"
                "或 /comic sou <关键词>（全渠道搜索）、/comic 排行 [日榜|周榜|月榜|总榜]\n"
                "格式可选 zip / pdf / long_img / none，写在最后只对这一次生效\n"
                "（也认中文 压缩包 / 文档 / 长图 / 本地）\n"
                "接着下上一次：/comic retry（暂停、下载中断或失败后重发这一条即可）\n"
                "查看排队：/comic status（管理员可加 全部 看所有会话）\n"
                "完整说明：/comic help；站点速查：/comic site\n"
                f"{describe_links(self.channels)}"
            )
            return

        head, _, rest = text.partition(" ")
        head_lower = head.lower()
        if head_lower in SEARCH_WORDS:
            async for result in self._handle_search(event, None, rest.strip()):
                yield result
            return
        if head_lower in RANKING_WORDS:
            async for result in self._handle_ranking(event, None, rest.strip()):
                yield result
            return
        if head_lower in STOP_ALL_WORDS and rest.strip().lower() in STATUS_WORDS:
            # `全部` 既能配 stop 也能配 status：先把 `/comic 全部 status` 认成查看，
            # 否则它会掉进 stop 分支，看一眼排队就把所有会话的下载全取消了
            async for result in self._handle_status(event, include_all=True):
                yield result
            return
        if head_lower in STOP_WORDS or head_lower in STOP_ALL_WORDS:
            include_all = (
                head_lower in STOP_ALL_WORDS
                or rest.strip().lower() in STOP_ALL_WORDS
            )
            async for result in self._handle_stop(event, include_all=include_all):
                yield result
            return
        if head_lower in STATUS_WORDS:
            include_all = (
                head_lower in STOP_ALL_WORDS
                or rest.strip().lower() in STOP_ALL_WORDS
            )
            async for result in self._handle_status(event, include_all=include_all):
                yield result
            return
        if head_lower in RETRY_WORDS:
            override = None
            if rest.strip():
                # `/comic retry pdf` 只给这一次重试换个格式，其余参数沿用上次；
                # 单发一个格式词时 split_pack_format 不认（它只切「有前文 + 末尾」），补一次查表
                _, override = split_pack_format(rest.strip())
                if override is None:
                    override = PACK_FORMAT_ALIASES.get(rest.strip().lower())
            async for result in self._handle_retry(event, override):
                yield result
            return
        if not rest.strip() and (
            head_lower in SEARCH_NEXT_WORDS or head_lower in SEARCH_PREV_WORDS
        ):
            step = 1 if head_lower in SEARCH_NEXT_WORDS else -1
            async for result in self._handle_turn_page(event, step):
                yield result
            return

        # 说明与站点速查放在下载解析之前：它们的词是固定命令词，不能被当成作品 ID 或链接
        if head_lower in HELP_WORDS:
            yield event.plain_result(self._render_help_text())
            return
        if head_lower in SITE_WORDS:
            yield event.plain_result(self._render_sites_text())
            return

        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        # 末尾的格式词只改这一次的产物形态（`/comic <链接> pdf`）；搜索与翻页已在上面分流，
        # 因此关键词里的格式词（如 long_img、长图）不会被当成格式
        text, pack_format = split_pack_format(text)
        indexed = self._resolve_index(session_id, text)
        if indexed is not None:
            channel, comic_id = indexed
            # 序号已经解析成渠道 + 规范 ID，不能再让下载入口把它当成新序号
            async for result in self._handle_download(
                event,
                channel.command,
                comic_id,
                allow_index=False,
                pack_format=pack_format,
            ):
                yield result
            return

        resolved = resolve_link(self.channels, text)
        if resolved is None:
            if text.isdigit():
                # 越界/过期先说清是哪一种，别让用户拿着一个序号去猜为什么不动
                hint = self._index_hint(session_id, text)
                yield event.plain_result(
                    hint
                    or (
                        "❌ 没有找到这个序号对应的结果（可能已过期）："
                        "先 /comic sou <关键词> 或 /comic 排行 看一次；"
                        "按 ID 下载请用对应渠道命令，或直接贴作品链接"
                    )
                )
            else:
                yield event.plain_result(
                    "❌ 认不出这条链接属于哪个站点：请贴作品页的完整链接，"
                    "或直接用对应渠道的命令（见 /comic help）\n"
                    f"{describe_links(self.channels)}"
                )
            return

        channel, _ = resolved
        async for result in self._handle_download(
            event, channel.command, text, pack_format=pack_format
        ):
            yield result

    async def _handle_download(
        self,
        event: AstrMessageEvent,
        command: str,
        raw_id: str,
        allow_index: bool = True,
        pack_format: str | None = None,
    ) -> AsyncGenerator[MessageEventResult, None]:
        text = " ".join((raw_id or "").split())
        head, _, rest = text.partition(" ")
        head_lower = head.lower()
        # 翻页看的是本会话上一次搜索，与敲的是哪个渠道命令无关，先于渠道查找处理
        if not rest.strip() and (
            head_lower in SEARCH_NEXT_WORDS or head_lower in SEARCH_PREV_WORDS
        ):
            step = 1 if head_lower in SEARCH_NEXT_WORDS else -1
            async for result in self._handle_turn_page(event, step):
                yield result
            return

        if head_lower in RETRY_WORDS:
            # 重试只认 `/comic retry`：它要知道「上一条」是哪个渠道的，渠道命令无从判断
            yield event.plain_result(
                "ℹ️ 重试请用 /comic retry（只重放本会话最近一次下载）；"
                f"要下新的就发 /{command} <ID 或链接>"
            )
            return
        if head_lower in STATUS_WORDS:
            yield event.plain_result("ℹ️ 查看在跑与排队的下载请用 /comic status")
            return
        # 说明与站点速查是全局的（与渠道无关），渠道命令下指回 `/comic`
        if head_lower in HELP_WORDS:
            yield event.plain_result("ℹ️ 完整说明请用 /comic help")
            return
        if head_lower in SITE_WORDS:
            yield event.plain_result("ℹ️ 站点与作品页链接速查请用 /comic site")
            return

        channel = self._commands.get(command)
        if channel is None:
            yield event.plain_result("❌ 该下载渠道未启用，请先在插件配置里添加")
            return

        if head_lower in SEARCH_WORDS:
            async for result in self._handle_search(
                event, channel, rest.strip()
            ):
                yield result
            return
        if head_lower in RANKING_WORDS:
            async for result in self._handle_ranking(
                event, channel, rest.strip()
            ):
                yield result
            return
        if head_lower in STOP_WORDS or head_lower in STOP_ALL_WORDS:
            include_all = (
                head_lower in STOP_ALL_WORDS
                or rest.strip().lower() in STOP_ALL_WORDS
            )
            async for result in self._handle_stop(event, include_all=include_all):
                yield result
            return
        if not text:
            # 示例用缩写命令：用户日常敲的就是这个，全称只是注册名
            hint = channel.display_command
            yield event.plain_result(
                f"用法：/{hint} <ID 或链接> [格式]，/{hint} sou <关键词>，/{hint} 排行\n"
                f"示例：/{hint} 123456、/{hint} 123456 pdf（这一次只用 PDF 打包）"
            )
            return

        # 末尾的格式词只改这一次的产物形态（`/jm 123456 pdf`）：搜索与停止已在上面分流，
        # 所以 `/jm sou 长图` 里的「长图」仍是关键词（格式词放在最后才会被切走）
        text, override = split_pack_format(text)
        if override is not None:
            pack_format = override

        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        denied = self.guard.check_session(session_id)
        if denied:
            yield event.plain_result(denied)
            return

        if allow_index:
            indexed = self._resolve_index(session_id, text)
            if indexed is not None:
                # 序号指向哪本就走哪个渠道：聚合搜索的结果跨渠道，命令只是入口
                channel, text = indexed
            else:
                # 序号越界/过期时说清原因，别掉进渠道的「无法识别 ID」里让人以为是 ID 写错了
                hint = self._index_hint(
                    session_id, text, channel.display_command
                )
                if hint:
                    yield event.plain_result(hint)
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

        # 记下这次要下的东西：之后不管是暂停、重载还是失败，`/comic retry` 都能原样重放
        self._remember_request(
            session_id, _LastRequest(channel.command, comic_id, pack_format)
        )

        debounce_key = f"{channel.name}:{comic_id}"
        if self.guard.is_duplicate(session_id, debounce_key):
            yield event.plain_result("⏳ 这个本子刚刚请求过，请稍后再试")
            return

        # 先登记再抢锁：排队期间也要能被 /comic stop 取消
        slot, cancel_token = self._begin_running(
            session_id, f"{channel.display_name} {comic_id}"
        )
        # ok / fail / paused：暂停与取消都不是失败，表情就停在「下载中」，别贴个锤子吓人
        result_state = "fail"
        try:
            if self._semaphore.locked():
                yield event.plain_result(self._queue_notice(session_id, slot))

            async with self._semaphore:
                running = self._running.get(slot)
                if running is not None and running.cancelled:
                    yield event.plain_result(
                        f"⏹ 已取消排队的请求：{channel.display_name} {comic_id}"
                    )
                    return
                if running is not None:
                    running.started = True
                # 贴表情先贴「开始」；贴不上（私聊、协议端不支持）就本次退回文本进度
                use_reaction, use_text_progress = await self._begin_progress(event)
                if not use_reaction:
                    # 贴上了表情就等于「已开始」，不必再多一条消息
                    fmt_note = (
                        f"（{describe_pack_format(pack_format)}）"
                        if pack_format
                        else ""
                    )
                    yield event.plain_result(
                        f"⏳ 开始下载 {channel.display_name} {comic_id}{fmt_note}…"
                    )
                task = DownloadTask(
                    channel,
                    self.settings,
                    self.temp_root,
                    self.save_root,
                    pack_format=pack_format,
                )
                try:
                    # 传规范化后的 ID 而不是用户原话：搜索序号（`/pica 1`）与作品链接
                    # 都已在上面解析成 comic_id，再交给任务重新 normalize 会把序号当 ID
                    outcome = await task.run(
                        comic_id,
                        self._progress_callback(event, use_text_progress, slot),
                        cancel_token,
                    )
                except (DownloadPaused, DownloadCancelled) as exc:
                    # 到点暂停 / 手动取消：断点留着，允许马上再发一次接着下
                    result_state = "paused"
                    self.guard.forget(session_id, debounce_key)
                    yield event.plain_result(str(exc))
                    return
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
                    if self.settings.cover_send:
                        async for item in self._deliver_cover(event, outcome):
                            yield item
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

    # ==================== 搜索流程 ====================

    async def _handle_search(
        self,
        event: AstrMessageEvent,
        channel: BaseChannel | None,
        keyword: str,
    ) -> AsyncGenerator[MessageEventResult, None]:
        """`/comic sou` 搜全部渠道，`/jm sou` 只搜一个渠道。

        聚合搜索由 SearchService 给每个渠道单独计时：慢渠道到点记为「已跳过」，
        其余结果照常返回，不会出现「一个渠道卡住整条命令一直转」。
        """
        if not self.settings.search_enabled:
            yield event.plain_result(
                "❌ 搜索未启用：请在插件配置的「搜索」里打开「启用搜索」"
            )
            return

        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        denied = self.guard.check_session(session_id)
        if denied:
            yield event.plain_result(denied)
            return

        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result(self._search_usage(channel))
            return

        if len(keyword) > SEARCH_MAX_KEYWORD_LENGTH:
            yield event.plain_result(
                f"❌ 搜索条件太长了，最多 {SEARCH_MAX_KEYWORD_LENGTH} 个字符"
            )
            return

        try:
            query = self.search_service.parse_query(keyword)
        except SearchQueryError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        if query.empty:
            yield event.plain_result(
                "❌ 只有排除条件不构成一次搜索：请再给一个关键词或正向前缀，"
                "例如 /comic sou tag:巨乳 -tag:NTR"
            )
            return

        cooling = self.search_service.begin(session_id)
        if cooling:
            yield event.plain_result(cooling)
            return

        searchable: list[BaseChannel] = []
        if channel is not None:
            try:
                channel.ensure_available()
            except PluginError as exc:
                yield event.plain_result(f"❌ {exc}")
                return
            if not channel.supports_search:
                yield event.plain_result(
                    f"❌ {channel.display_name} 暂不支持搜索"
                )
                return
        else:
            searchable = self.search_service.searchable()
            if not searchable:
                yield event.plain_result(
                    "❌ 当前没有支持搜索的渠道；可以先用 /comic site 到站内搜作品、找 ID"
                )
                return

        # 搜索与下载共用同一套进度设置：能贴表情就贴（❤️ 搜索中 → OK / 🔨），
        # 贴不上（私聊、协议端不支持）或选了文本进度时，改发文字进度。
        use_reaction, use_text = await self._begin_progress(event)
        result_state = "fail"
        try:
            if channel is not None:
                if use_text:
                    yield event.plain_result(
                        f"🔎 正在搜索 {channel.display_name}「{query.raw}」…"
                    )
                outcome = await self.search_service.search_one(channel, query)
                result_state = "fail" if outcome.error else "ok"
                if outcome.page is not None and not outcome.page.empty:
                    self._mark_index_source(session_id, "search")
                yield event.plain_result(
                    await self.search_service.render_one(outcome, query, session_id)
                )
                return

            try:
                if use_text:
                    yield event.plain_result(
                        f"🔎 正在搜索 {len(searchable)} 个渠道「{query.raw}」…"
                    )
                outcomes = await self.search_service.search_all(
                    query,
                    on_progress=(
                        self._search_progress_callback(event) if use_text else None
                    ),
                )
            except PluginError as exc:
                yield event.plain_result(f"❌ {exc}")
                return
            # 只要有一个渠道正常返回就算完整跑完；全部超时/报错才贴失败
            result_state = (
                "ok" if any(not outcome.error for outcome in outcomes) else "fail"
            )
            if any(
                outcome.page is not None and not outcome.page.empty
                for outcome in outcomes
            ):
                self._mark_index_source(session_id, "search")
            yield event.plain_result(
                await self.search_service.render_all(outcomes, query, session_id)
            )
        finally:
            if use_reaction:
                await self._react(event, "done" if result_state == "ok" else "fail")

    def _search_usage(self, channel: BaseChannel | None) -> str:
        """搜索用法提示：前缀写法、引号规则与一个组合示例。"""
        prefix_hint = (
            "类型前缀：tag:标签 author:作者 actor:角色 work:作品 "
            "parody:原作 group:社团 language:语言\n"
            "前缀前加 - 表示排除；值含空格时用引号，如 tag:\"big breasts\""
        )
        if channel is not None:
            hint = channel.display_command
            return (
                f"用法：/{hint} sou <关键词>\n"
                f"{prefix_hint}\n"
                f"示例：/{hint} sou tag:巨乳 -tag:NTR 堕落"
            )
        searchable = self.search_service.searchable()
        names = "、".join(
            f"/{item.display_command}" for item in searchable
        ) or "（没有支持搜索的渠道）"
        return (
            "用法：/comic sou <关键词>（一次搜索所有支持搜索的渠道）\n"
            f"{prefix_hint}\n"
            f"可搜索的渠道：{names}\n"
            "也可以只搜一个：/<渠道命令> sou <关键词>\n"
            "示例：/comic sou tag:巨乳 -tag:NTR 堕落"
        )

    async def _handle_turn_page(
        self, event: AstrMessageEvent, step: int
    ) -> AsyncGenerator[MessageEventResult, None]:
        """翻上一页/下一页；翻的是本会话最近一次搜索，与敲的是哪个命令无关。"""
        if not self.settings.search_enabled:
            yield event.plain_result(
                "❌ 搜索未启用：请在插件配置的「搜索」里打开「启用搜索」"
            )
            return

        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        denied = self.guard.check_session(session_id)
        if denied:
            yield event.plain_result(denied)
            return

        # 榜单与搜索共用「上一页/下一页」：翻的是本会话最近一次出列表的那个
        if (
            self._index_source.get(session_id) == "ranking"
            and self.ranking_service.session_active(session_id)
        ):
            async for item in self._turn_ranking_page(event, step):
                yield item
            return

        # 翻页也归在搜索流程里：本地缓冲够翻时一瞬完成，去上游取下一页时才需要提示
        use_reaction, use_text = await self._begin_progress(event)
        result_state = "fail"

        async def on_fetch() -> None:
            if not use_text:
                return
            try:
                await event.send(
                    MessageChain([Comp.Plain("📄 正在从上游取下一页结果…")])
                )
            except Exception as exc:  # 进度发不出去不影响翻页本身
                logger.debug(f"[{PLUGIN_NAME}] 发送翻页进度失败（忽略）: {exc}")

        try:
            text = await self.search_service.turn_page(
                session_id, step, on_fetch=on_fetch
            )
            if text is None:
                yield event.plain_result(
                    "❌ 没有可翻页的搜索结果（可能已过期）："
                    "先用 /comic sou <关键词> 或 /<渠道命令> sou <关键词> 搜一次"
                )
                return
            result_state = "ok"
            self._mark_index_source(session_id, "search")
            yield event.plain_result(text)
        finally:
            if use_reaction:
                await self._react(event, "done" if result_state == "ok" else "fail")

    # ==================== 排行榜流程 ====================

    async def _handle_ranking(
        self,
        event: AstrMessageEvent,
        channel: BaseChannel | None,
        args: str,
    ) -> AsyncGenerator[MessageEventResult, None]:
        """`/comic 排行 [时间窗]` 聚合所有渠道，`/<渠道> 排行 [时间窗]` 只看一个渠道。

        榜单与搜索是两条平行链路：不共享关键词解析，也不共享会话——只有「回序号下载」
        与「上一页/下一页」两句入口是共用的，靠 `_index_source` 记住最近一次的列表是谁出的。
        """
        if not self.settings.ranking_enabled:
            yield event.plain_result(
                "❌ 排行榜未启用：请在插件配置的「排行榜」里打开「启用排行榜」"
            )
            return

        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        denied = self.guard.check_session(session_id)
        if denied:
            yield event.plain_result(denied)
            return

        token = " ".join((args or "").split())
        board = self.settings.ranking_default_board
        if token:
            parsed = parse_board(token)
            if parsed is None:
                yield event.plain_result(self._ranking_usage(channel, unknown=token))
                return
            board = parsed

        targets: list[BaseChannel]
        if channel is not None:
            try:
                channel.ensure_available()
            except PluginError as exc:
                yield event.plain_result(f"❌ {exc}")
                return
            if not channel.supports_ranking:
                yield event.plain_result(f"❌ {channel.display_name} 暂不支持排行榜")
                return
            if board not in channel.ranking_boards:
                supported = "、".join(
                    board_label(item) for item in channel.ranking_boards
                )
                yield event.plain_result(
                    f"❌ {channel.display_name} 没有{board_label(board)}，只有：{supported}"
                )
                return
            targets = [channel]
        else:
            targets = self.ranking_service.rankable()
            if not targets:
                yield event.plain_result(
                    "❌ 当前没有支持排行榜的渠道：请先在插件配置里启用渠道"
                )
                return

        cooling = self.ranking_service.begin(session_id)
        if cooling:
            yield event.plain_result(cooling)
            return

        use_reaction, use_text = await self._begin_progress(event)
        result_state = "fail"
        try:
            if use_text:
                names = "、".join(item.display_name for item in targets)
                yield event.plain_result(
                    f"🏆 正在取{board_label(board)}：{names}…"
                )
            result = await self.ranking_service.run(session_id, channel, board)
            if result.count:
                result_state = "ok"
                self._mark_index_source(session_id, "ranking")
            yield event.plain_result(result.text)
        finally:
            if use_reaction:
                await self._react(event, "done" if result_state == "ok" else "fail")

    async def _turn_ranking_page(
        self, event: AstrMessageEvent, step: int
    ) -> AsyncGenerator[MessageEventResult, None]:
        """翻榜单的上一页/下一页（榜单与搜索共用同一句「下一页」）。"""
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        use_reaction, use_text = await self._begin_progress(event)
        result_state = "fail"
        try:
            if use_text:
                yield event.plain_result("📄 正在取下一页榜单…")
            turn = await self.ranking_service.turn_page(session_id, step)
            if turn.result is None:
                yield event.plain_result(
                    turn.message
                    or "❌ 没有可翻页的榜单（可能已过期）：先发 /comic 排行 看一次"
                )
                return
            result_state = "ok"
            self._mark_index_source(session_id, "ranking")
            yield event.plain_result(turn.result.text)
        finally:
            if use_reaction:
                await self._react(event, "done" if result_state == "ok" else "fail")

    # ---- 序号路由：搜索与榜单共用「回序号下载」 ----
    def _index_order(self, session_id: str) -> list[str]:
        """先问最近一次出列表的服务，再问另一个。"""
        latest = self._index_source.get(session_id, "")
        return ["ranking", "search"] if latest == "ranking" else ["search", "ranking"]

    def _mark_index_source(self, session_id: str, source: str) -> None:
        """记下这次列表是谁出的：下一个裸数字该按谁的序号解释。"""
        if not session_id:
            return
        self._index_source[session_id] = source
        if len(self._index_source) > 1024:
            self._index_source = {
                key: value
                for key, value in self._index_source.items()
                if self.search_service.session_active(key)
                or self.ranking_service.session_active(key)
            }

    def _resolve_index(
        self, session_id: str, token: str
    ) -> tuple[BaseChannel, str] | None:
        for source in self._index_order(session_id):
            found = (
                self.ranking_service.resolve_index(session_id, token)
                if source == "ranking"
                else self.search_service.resolve_index(session_id, token)
            )
            if found is not None:
                return found
        return None

    def _index_hint(self, session_id: str, token: str, command: str = "comic") -> str | None:
        for source in self._index_order(session_id):
            hint = (
                self.ranking_service.index_hint(session_id, token, command)
                if source == "ranking"
                else self.search_service.index_hint(session_id, token, command)
            )
            if hint:
                return hint
        return None

    def _ranking_usage(self, channel: BaseChannel | None = None, unknown: str = "") -> str:
        """榜单用法提示：时间窗取值、省略时的默认值，以及一个具体例子。"""
        head = f"❌ 认不出「{unknown}」是哪一档榜单" + "\n" if unknown else ""
        boards = "、".join(board_label(item) for item in RANKING_BOARDS)
        command = f"/{channel.display_command}" if channel is not None else "/comic"
        return (
            f"{head}用法：{command} 排行 [时间窗]，时间窗可省略"
            f"（默认{board_label(self.settings.ranking_default_board)}）\n"
            f"时间窗：{boards}（也认 今日 / 本周 / 本月 / 全部）\n"
            f"例：{command} 排行 日榜、{command} rank week\n"
            "看后面的名次：/comic 下一页；下载：直接回序号"
        )

    async def _deliver_cover(
        self, event: AstrMessageEvent, outcome: DownloadOutcome
    ) -> AsyncGenerator[MessageEventResult, None]:
        """先发一张封面（可选打码），再发文件。

        封面是附加交付：出错只记日志、不补话术——用户真正要的是文件，不能被一张封面挡掉。
        封面说明（若开启）单独发一条，并且只在封面确实发出去之后才发，免得说明描述了一张
        用户根本没收到的图。
        """
        try:
            result = await self.cover_service.prepare(
                outcome.meta,
                outcome.source_dir,
                channel=outcome.meta.channel,
            )
        except CoverCensorError as exc:
            # 开了打码却没打成：宁可少发一张封面，也不能把没糊的图发出去
            logger.warning(f"[{PLUGIN_NAME}] 封面未发送：{exc}")
            yield event.plain_result(f"⚠️ 封面未发送：{exc}")
            return
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 生成封面失败（忽略）: {exc}")
            return

        if result is None:
            return

        if await self._send_cover(event, result):
            await self._send_cover_caption(event, outcome, result)
            return

        # 只有开了打码才敢自动加糊：用户主动关掉打码时，不能再擅自把图糊掉
        if not (self.settings.cover_censor and self.settings.cover_retry_strong):
            logger.warning(f"[{PLUGIN_NAME}] 封面未发送成功（平台拦截），本次跳过")
            return

        logger.warning(f"[{PLUGIN_NAME}] 封面被平台拦下，改用整张打码重发一次")
        try:
            strong = await self.cover_service.reinforce(result)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 封面加强打码失败（本次跳过）: {exc}")
            return
        # 重发的是降级版：说明要按新的打码结论重算，否则仍写着「局部打码」
        if await self._send_cover(event, strong):
            await self._send_cover_caption(event, outcome, strong)
        else:
            logger.warning(f"[{PLUGIN_NAME}] 封面整张打码后仍未被平台接受，本次跳过")

    async def _send_cover(
        self, event: AstrMessageEvent, result: CoverResult
    ) -> bool:
        """直接发一张封面（只有图），返回是否发送成功。

        走 ``event.send`` 而不是 ``yield``：只有自己发才拿得到失败（QQ 的图片审核
        会「收下但不投递」，AstrBot 只在 respond 阶段记一条 ERROR，插件侧无从补救）。
        """
        chain: list[Any] = [Comp.Image.fromFileSystem(str(result.path))]
        try:
            await event.send(MessageChain(chain))
        except Exception as exc:
            # 平台侧的拦截/网络失败都走这里：拿不到更精确的类型，交给调用方决定重发
            logger.warning(f"[{PLUGIN_NAME}] 封面发送失败: {exc}")
            return False
        return True

    async def _send_cover_caption(
        self, event: AstrMessageEvent, outcome: DownloadOutcome, result: CoverResult
    ) -> None:
        """封面说明单独发一条（开关关着时没有说明）；说明发不出去不影响封面本身。"""
        caption = self.cover_service.caption(outcome.meta, result)
        if not caption:
            return
        try:
            await event.send(MessageChain([Comp.Plain(caption)]))
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 封面说明发送失败（忽略）: {exc}")

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

    @staticmethod
    def _command_args(event: AstrMessageEvent) -> str:
        """取命令名之后的原始文本，供 `/jm sou 名字` 这类多词参数使用。

        AstrBot 的指令参数按空格切分，`cmd_xxx(comic_id: str)` 只会拿到第一个 token，
        `/jm sou 触手 少女` 会把关键词截成「sou」；因此这里直接读整条消息并剥掉命令名
        （命令名本身不含空格）。搜索关键词允许带空格，下载 ID/链接的解析仍按原逻辑走。
        """
        try:
            raw = event.get_message_str() or ""
        except Exception:  # 兼容实现了 message_str 但没有 getter 的事件对象
            raw = str(getattr(event, "message_str", "") or "")
        parts = raw.strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    async def _react(self, event: AstrMessageEvent, status: str) -> bool:
        """按配置决定是否需要贴表情；关闭进度或选了文本进度时返回 False。"""
        if self.settings.progress_style != "emoji":
            return False
        return await set_progress_reaction(event, status)

    async def _begin_progress(self, event: AstrMessageEvent) -> tuple[bool, bool]:
        """贴「开始」表情，并决定这一次要不要改用文本进度。

        返回 `(是否贴上了表情, 是否走文本进度)`：贴上了就不再发开始消息，由命令层
        完成时换成 OK / 🔨；贴不上（私聊、协议端不支持表情回应）或用户选了文本进度时，
        命令层补一条开始消息，期间按需推送文本进度。
        """
        use_reaction = await self._react(event, "start")
        use_text = self.settings.progress_style == "text" or (
            self.settings.progress_style == "emoji" and not use_reaction
        )
        return use_reaction, use_text

    def _search_progress_callback(
        self, event: AstrMessageEvent
    ) -> Callable[[int, int], Awaitable[None]]:
        """全渠道搜索的文本进度：渠道陆续返回时更新一句，避免刷屏。

        双阈值节流：进度每进 25% 且距上一条消息至少 ``PROGRESS_MIN_INTERVAL`` 秒才发；
        搜索通常一两秒就结束，这样快搜索只会看到开始与结果两条，慢搜索才补进度。
        最后一个渠道结束时不再补一句，紧跟着就是结果消息。
        """
        box = {"last_pct": -1, "sent_at": time.monotonic()}

        async def on_progress(done: int, total: int) -> None:
            if total <= 0 or done >= total:
                return
            now = time.monotonic()
            percent = int(done * 100 / total)
            if percent - box["last_pct"] < PROGRESS_STEP_PERCENT:
                return
            if now - box["sent_at"] < PROGRESS_MIN_INTERVAL:
                return
            box["last_pct"] = percent
            box["sent_at"] = now
            text = f"🔎 搜索中… 已返回 {done}/{total} 个渠道"
            try:
                await event.send(MessageChain([Comp.Plain(text)]))
            except Exception as exc:  # 进度发不出去不影响搜索本身
                logger.debug(f"[{PLUGIN_NAME}] 发送搜索进度失败（忽略）: {exc}")

        return on_progress

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

    def _begin_running(self, session_id: str, label: str) -> tuple[int, CancelToken]:
        self._task_seq += 1
        token = CancelToken()
        self._running[self._task_seq] = _RunningTask(
            label=label, session_id=session_id, cancel=token
        )
        return self._task_seq, token

    def _end_running(self, slot: int) -> None:
        self._running.pop(slot, None)

    def _remember_request(self, session_id: str, request: _LastRequest) -> None:
        """记住本会话最近一次下载请求；只保留最近若干个会话，长时间运行也不会堆积。"""
        self._last_request.pop(session_id, None)
        self._last_request[session_id] = request
        while len(self._last_request) > LAST_REQUEST_LIMIT:
            self._last_request.pop(next(iter(self._last_request)))
        self._save_last_requests()

    # ---- 重试记录的落盘 ----

    def _last_request_file(self) -> Path:
        return self.data_dir / LAST_REQUEST_FILENAME

    def _load_last_requests(self) -> dict[str, _LastRequest]:
        """读回上次存盘的下载记录；文件缺失、损坏或字段不对都只当没有记录。"""
        path = self.data_dir / LAST_REQUEST_FILENAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning(f"[{PLUGIN_NAME}] 重试记录读取失败，按空处理: {exc}")
            return {}

        if not isinstance(raw, dict):
            logger.warning(f"[{PLUGIN_NAME}] 重试记录格式不对，按空处理")
            return {}

        loaded: dict[str, _LastRequest] = {}
        for session_id, item in raw.items():
            if not isinstance(session_id, str) or not isinstance(item, dict):
                continue
            command = item.get("command")
            comic_id = item.get("comic_id")
            if not isinstance(command, str) or not isinstance(comic_id, str):
                continue
            pack_format = item.get("pack_format")
            loaded[session_id] = _LastRequest(
                command=command,
                comic_id=comic_id,
                pack_format=pack_format if isinstance(pack_format, str) else None,
            )
        return loaded

    def _save_last_requests(self) -> None:
        """整表写回磁盘（原子替换）；落盘失败只记日志，不能影响这次下载。"""
        path = self._last_request_file()
        payload = {
            session_id: {
                "command": item.command,
                "comic_id": item.comic_id,
                "pack_format": item.pack_format,
            }
            for session_id, item in self._last_request.items()
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(path)
        except OSError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 重试记录写入失败（不影响下载）: {exc}")
            tmp.unlink(missing_ok=True)

    async def _handle_status(
        self, event: AstrMessageEvent, *, include_all: bool
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查看当前在跑与排队的下载；管理员可以用 `全部` 看所有会话。只读，不动任何任务。"""
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        is_admin = bool(getattr(event, "is_admin", None) and event.is_admin())
        if include_all and not is_admin:
            yield event.plain_result(
                "❌ 只有管理员可以查看所有会话的下载；你自己的用 /comic status"
            )
            return

        indexes = self._queue_indexes()
        targets = [
            (slot, task)
            for slot, task in sorted(self._running.items())
            if include_all or task.session_id == session_id
        ]
        if not targets:
            others = sum(1 for task in self._running.values() if task.started)
            if others:
                yield event.plain_result(
                    "ℹ️ 你这里没有在跑或排队的下载；"
                    f"另有 {others} 个其他会话的下载正在进行"
                    "（管理员可用 /comic status 全部）"
                )
            else:
                yield event.plain_result("ℹ️ 现在没有下载在跑，也没有排队的请求")
            return

        running = sum(1 for _, task in targets if task.started)
        queued = len(targets) - running
        head = "📊 全部下载状态" if include_all else "📊 当前下载状态"
        lines = [head]
        for slot, task in targets:
            line = self._task_line(task, queue_index=indexes.get(slot))
            if include_all:
                # 管理员看全体时要能分辨是谁的，带上会话末尾那段（群号/用户号）
                line = f"{line} · {_session_tag(task.session_id)}"
            lines.append(line)
        minutes = max(1, round(self.settings.download_timeout / 60))
        lines.append(
            f"⏱ 在跑 {running} / 排队 {queued} · 单本上限 {minutes} 分钟"
        )
        lines.append("⏹ 取消：/comic stop　↻ 接着下：/comic retry")
        yield event.plain_result("\n".join(lines))

    async def _handle_retry(
        self, event: AstrMessageEvent, pack_format: str | None = None
    ) -> AsyncGenerator[MessageEventResult, None]:
        """重放本会话最近一次下载，效果等同「再发一次同样的命令」。

        存的是规范化后的 ID，所以重试不依赖搜索结果是否还在、也不需要用户回去翻原消息。
        """
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        last = self._last_request.get(session_id)
        if last is None:
            yield event.plain_result(
                "ℹ️ 本会话还没有下载记录，没有可重试的漫画；"
                "直接发 /comic <作品链接> 或 /<渠道命令> <ID> 开始一本新的"
            )
            return
        channel = self._commands.get(last.command)
        if channel is None:
            yield event.plain_result(
                f"❌ 上次用的渠道（{last.command}）现在没有启用，"
                "重试前请先在插件配置里加回来"
            )
            return
        async for result in self._handle_download(
            event,
            channel.command,
            last.comic_id,
            allow_index=False,
            # 命令里临时指定的格式优先，其次沿用上次那一次的格式
            pack_format=pack_format or last.pack_format,
        ):
            yield result

    async def _handle_stop(
        self, event: AstrMessageEvent, *, include_all: bool
    ) -> AsyncGenerator[MessageEventResult, None]:
        """手动取消：默认只取消本会话自己的下载，管理员可以用 `全部` 一次收掉所有会话。

        取消与「到点暂停」等价——已经下好的图片留在断点里，重发同一条命令就能接着下。
        """
        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        is_admin = bool(getattr(event, "is_admin", None) and event.is_admin())
        if include_all and not is_admin:
            yield event.plain_result("❌ 只有管理员可以取消所有会话的下载；你自己的用 /comic stop")
            return

        targets = [
            running
            for running in self._running.values()
            if include_all or running.session_id == session_id
        ]
        if not targets:
            others = len(self._running)
            if others:
                yield event.plain_result(
                    f"ℹ️ 你这里没有在跑的下载；另有 {others} 个其他会话的下载正在进行"
                    "（/comic stop 只管自己的，管理员可用 /comic stop 全部）"
                )
            else:
                yield event.plain_result("ℹ️ 现在没有下载在跑")
            return

        lines = ["⏹ 已通知停止："]
        for running in targets:
            running.cancelled = True
            running.cancel.cancel()
            state = running.state
            if state is None:
                lines.append(f" · {running.label}：正在取漫画信息…")
            else:
                lines.append(
                    f" · {running.label}：{state.headline}"
                    f" · {format_size(state.bytes_downloaded)}"
                    f" · 已用 {state.elapsed:.0f}s"
                )
        lines.append("已下好的图片会保留，停稳后会再回一条；发 /comic retry 可以接着下")
        yield event.plain_result("\n".join(lines))

    def _task_line(self, task: _RunningTask, *, queue_index: int | None = None) -> str:
        """一条任务的状态行：在跑的给实时进度，排队的给位次。"""
        if not task.started:
            where = "排队中" if queue_index is None else f"排队中（第 {queue_index} 位）"
            return f" · {task.label}：{where}"
        state = task.state
        if state is None:
            return f" · {task.label}：正在取漫画信息…"
        return (
            f" · {task.label}：{state.headline}"
            f" · {format_size(state.bytes_downloaded)}"
            f" · 已用 {state.elapsed:.0f}s"
        )

    def _queue_indexes(self) -> dict[int, int]:
        """排队位次：按登记顺序（slot 递增）算，先登记者排在前面。"""
        queued = [
            slot for slot, task in sorted(self._running.items()) if not task.started
        ]
        return {slot: index for index, slot in enumerate(queued, 1)}

    def _queue_notice(self, session_id: str, slot: int) -> str:
        """排队提示：说清前面跑到哪、还要等这几个，用户才知道大概等多久。

        只列**本会话**的任务：别的会话在下什么属于别人的事，这里最多报个数。
        """
        lines = ["⏳ 前面的下载还没结束，你的请求已排队"]
        indexes = self._queue_indexes()
        others = 0
        for other_slot, task in sorted(self._running.items()):
            if other_slot == slot:
                continue
            if task.session_id != session_id:
                others += task.started
                continue
            lines.append(self._task_line(task, queue_index=indexes.get(other_slot)))
        if others:
            lines.append(f" · 另有 {others} 个其他会话的下载正在进行")
        minutes = max(1, round(self.settings.download_timeout / 60))
        lines.append(f"⏱ 单本上限 {minutes} 分钟，前一个任务结束后自动开始")
        lines.append("📊 看排队情况：/comic status　⏹ 取消：/comic stop")
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
        if outcome.pack.note:
            lines.append(f"⚠️ {outcome.pack.note}")

        # 与全局配置不一致说明这次是命令里指定的格式，否则用户会以为配置被改掉了
        if outcome.pack.fmt != self.settings.pack_format:
            lines.append(f"打包格式：{describe_pack_format(outcome.pack.fmt)}（本次指定）")

        if outcome.pack.fmt == "none":
            lines.append(f"已保存到本地：{outcome.source_dir}")
        elif outcome.pack.encrypted:
            lines.append("已加密打包")
        # 耗时统一并到最后一行：它和上面那句一样都是「这次结果如何」，不该为一个词再占一行
        lines[-1] = f"{lines[-1]}，耗时 {outcome.elapsed:.1f}s"
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

"""渠道抽象。

一个渠道只负责三件事：把用户输入收敛成本渠道的漫画 ID、取元信息、把图片下载到本地目录。
**打包、加密、发送都在上层统一处理**，渠道不参与，新增渠道时也不该重复实现。
"""

from __future__ import annotations

import importlib.util
import os
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar

from ..domain import ChannelUnavailableError, ComicMeta, DownloadError, DownloadedComic, PluginSettings

# 进度回调：(已完成, 总数, 单位)；总数未知时传 0
ProgressCallback = Callable[[int, int, str], Awaitable[None]]

# 代理来源：日志与提示里要能看出「这一趟走的是哪条路」，取值本身就是对用户展示的文案
PROXY_SOURCE_PLUGIN = "代理"
PROXY_SOURCE_ASTRBOT = "宿主代理"
PROXY_SOURCE_DIRECT = "直连"


def split_option_list(raw: Any) -> list[str]:
    """把「多行文本 / 逗号空格分隔的字符串 / list」拆成列表，保持填写顺序。

    域名这类配置的顺序就是降级顺序，因此不排序、不去重。
    """
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        parts = raw.replace(",", " ").replace("\r", " ").split("\n")
        return [token for part in parts for token in part.split() if token]
    return []


class BaseChannel(ABC):
    """渠道基类。子类只需声明类属性并实现三个抽象方法。"""

    name: ClassVar[str]  # 内部名，与 _conf_schema.json 的模板 key 一致
    command: ClassVar[str]  # 注册给指令系统的全称（站点名，如 jmcomic / nhentai）
    # 缩写别名：用户日常敲的是这些；第一个别名同时是提示文案里优先展示的命令
    command_aliases: ClassVar[tuple[str, ...]] = ()
    display_name: ClassVar[str]  # 帮助文案里的展示名
    requires: ClassVar[tuple[str, ...]] = ()  # 运行时依赖的模块名
    # 「使用代理」开关在配置里缺失时的默认值：面向境外站点的渠道默认开启
    default_use_proxy: ClassVar[bool] = False

    def __init__(self, options: Mapping[str, Any], settings: PluginSettings) -> None:
        self.options: dict[str, Any] = dict(options)
        self.settings = settings

    # ---- 命令名 ----
    @property
    def display_command(self) -> str:
        """提示文案里优先展示的命令：缩写别名排在前面，所以用户看到的是更短的那个。"""
        return self.command_aliases[0] if self.command_aliases else self.command

    @property
    def command_names(self) -> tuple[str, ...]:
        """全称在前、缩写在后，重复与空别名剔掉。

        注册指令与「/命令 → 渠道」映射都用这一份清单，避免两处各写一遍导致别名只在其中一边生效。
        """
        names = [self.command]
        for alias in self.command_aliases:
            if alias and alias not in names:
                names.append(alias)
        return tuple(names)

    # ---- 配置读取：渠道私有配置项一律走这三个方法，缺失/脏值自动兜底 ----
    @property
    def enabled(self) -> bool:
        return self.opt_bool("enable", True)

    def opt_bool(self, key: str, default: bool) -> bool:
        value = self.options.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
        return default

    def opt_int(self, key: str, default: int, minimum: int | None = None) -> int:
        try:
            value = int(self.options.get(key))
        except (TypeError, ValueError):
            return default
        if minimum is not None and value < minimum:
            return default
        return value

    def opt_str(self, key: str, default: str = "") -> str:
        value = self.options.get(key)
        return value.strip() if isinstance(value, str) else default

    def opt_list(self, key: str) -> list[str]:
        """兼容「多行文本 / 逗号空格分隔的字符串」与「真正的 list」三种填法。"""
        return split_option_list(self.options.get(key))

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        """渠道没被配置过时使用的默认项（内置渠道因此开箱即启用）。"""
        return {}

    # ---- 代理：渠道开关 → 插件代理 → AstrBot 本体代理（环境变量） ----
    def resolve_proxy(self, *, default_enabled: bool | None = None) -> tuple[str, str]:
        """解析本渠道这一趟要走的代理，返回（代理串，来源）。

        - 渠道「使用代理」关掉 → 直连，不看任何环境变量
        - 插件配置了全局代理 → 用它
        - 插件没配 → 用 AstrBot 本体代理（AstrBot 把它写在 `http_proxy`/`https_proxy`
          环境变量里），这样本体配过代理的人不用在插件里再填一遍
        - 都没有 → 直连
        """
        if default_enabled is None:
            default_enabled = self.default_use_proxy
        if not self.opt_bool("use_proxy", default_enabled):
            return "", PROXY_SOURCE_DIRECT
        if self.settings.proxy:
            return self.settings.proxy, PROXY_SOURCE_PLUGIN
        for key in ("https_proxy", "http_proxy", "HTTPS_PROXY", "HTTP_PROXY"):
            value = os.environ.get(key, "").strip()
            if value:
                return value, PROXY_SOURCE_ASTRBOT
        return "", PROXY_SOURCE_DIRECT

    def proxy_summary(self, *, default_enabled: bool | None = None) -> str:
        """代理的「人话」描述，供日志与用户提示用：只说这一趟走哪条路。

        **不回显代理地址**：帮助文案会发到群里，而代理串里常带内网主机与账号密码；
        要确认具体用哪个代理时直接看配置即可。
        """
        proxy, source = self.resolve_proxy(default_enabled=default_enabled)
        return source if proxy else PROXY_SOURCE_DIRECT

    def missing_requirement(self) -> str | None:
        """返回首个缺失的运行时依赖名；依赖齐备时返回 None。"""
        for module in self.requires:
            try:
                found = importlib.util.find_spec(module) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                return module
        return None

    def ensure_available(self) -> None:
        """命令入口的第一道闸：未启用或依赖缺失时，用面向用户的文案拒绝。"""
        if not self.enabled:
            raise ChannelUnavailableError(
                f"{self.display_name} 渠道未启用，请在插件配置里添加并启用该渠道"
            )
        missing = self.missing_requirement()
        if missing is not None:
            raise ChannelUnavailableError(
                f"{self.display_name} 依赖 {missing} 未安装，请先安装后重载插件"
            )

    # ---- 子类必须实现 ----
    @abstractmethod
    def normalize_id(self, raw: str) -> str:
        """把用户输入（纯 ID / 链接 / 分享文本）收敛成本渠道的漫画 ID。"""

    @abstractmethod
    async def fetch_meta(self, comic_id: str, work_dir: Path) -> ComicMeta:
        """取元信息。work_dir 是该任务专属的临时目录。"""

    @abstractmethod
    async def download(
        self,
        comic_id: str,
        work_dir: Path,
        on_progress: ProgressCallback | None = None,
    ) -> DownloadedComic:
        """下载整本漫画到 work_dir（或它的子目录）并返回落盘结果。"""

    def cancel_downloads(self) -> list[str]:
        """插件卸载时取消进行中的下载，返回可写进日志的状态摘要。

        默认不做事；下载跑在线程里的渠道（如 JM）应覆盖它，避免重载后线程继续白跑。
        """
        return []

    def wrap_failure(self, exc: Exception) -> DownloadError:
        """把渠道内部异常翻译成面向用户的下载失败文案。"""
        return DownloadError(f"{self.display_name} 下载失败：{exc}")

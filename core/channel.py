"""渠道抽象。

一个渠道只负责三件事：把用户输入收敛成本渠道的漫画 ID、取元信息、把图片下载到本地目录。
**打包、加密、发送都在上层统一处理**，渠道不参与，新增渠道时也不该重复实现。
"""

from __future__ import annotations

import importlib.util
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar

from ..domain import ChannelUnavailableError, ComicMeta, DownloadError, DownloadedComic, PluginSettings

# 进度回调：(已完成, 总数, 单位)；总数未知时传 0
ProgressCallback = Callable[[int, int, str], Awaitable[None]]


class BaseChannel(ABC):
    """渠道基类。子类只需声明类属性并实现三个抽象方法。"""

    name: ClassVar[str]  # 内部名，与 _conf_schema.json 的模板 key 一致
    command: ClassVar[str]  # 聊天命令名：命令 "foo" 对应 /foo <ID>
    display_name: ClassVar[str]  # 帮助文案里的展示名
    requires: ClassVar[tuple[str, ...]] = ()  # 运行时依赖的模块名

    def __init__(self, options: Mapping[str, Any], settings: PluginSettings) -> None:
        self.options: dict[str, Any] = dict(options)
        self.settings = settings

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
        """兼容「逗号/空格分隔的字符串」与「真正的 list」两种填法。"""
        value = self.options.get(key)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [part for part in value.replace(",", " ").split() if part]
        return []

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

    def wrap_failure(self, exc: Exception) -> DownloadError:
        """把渠道内部异常翻译成面向用户的下载失败文案。"""
        return DownloadError(f"{self.display_name} 下载失败：{exc}")

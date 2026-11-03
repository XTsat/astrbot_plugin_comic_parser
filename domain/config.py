"""配置解析。

WebUI 的配置是裸 dict（含用户手改的脏值），这里统一收口成强类型对象：
类型转换失败一律回落到默认值，绝不让配置问题在业务层炸成运行时异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .constants import (
    DEFAULT_DOWNLOAD_TIMEOUT,
    DEFAULT_MAX_CONCURRENT_TASKS,
    DEFAULT_PACK_FORMAT,
    DEFAULT_PROGRESS_STYLE,
    DEFAULT_RETRY_TIMES,
    PACK_FORMATS,
    PROGRESS_STYLES,
)

# template_list 里每条渠道配置都带这个 key，标记它属于哪个渠道模板
TEMPLATE_KEY = "__template_key"


def _as_int(raw: Any, default: int, minimum: int | None = None) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def _as_bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def _as_str(raw: Any, default: str = "") -> str:
    return raw.strip() if isinstance(raw, str) else default


def _as_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


@dataclass(slots=True)
class PluginSettings:
    """全局设置。渠道私有配置留在 channels 里，由各渠道自行解释。"""

    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    debounce_interval: int = 60
    download_dir: str = "downloads"
    pack_format: str = DEFAULT_PACK_FORMAT
    pack_password: str = ""
    filename_show_password: bool = False
    auto_delete_after_send: bool = True
    progress_style: str = DEFAULT_PROGRESS_STYLE
    clean_temp_on_start: bool = True
    max_concurrent_tasks: int = DEFAULT_MAX_CONCURRENT_TASKS
    download_timeout: int = DEFAULT_DOWNLOAD_TIMEOUT
    download_retry_times: int = DEFAULT_RETRY_TIMES
    proxy: str = ""
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> PluginSettings:
        data: Mapping[str, Any] = config or {}
        pack_format = _as_str(data.get("pack_format"), DEFAULT_PACK_FORMAT)
        if pack_format not in PACK_FORMATS:
            pack_format = DEFAULT_PACK_FORMAT
        progress_style = _as_str(data.get("progress_style"), DEFAULT_PROGRESS_STYLE)
        if progress_style not in PROGRESS_STYLES:
            progress_style = DEFAULT_PROGRESS_STYLE

        return cls(
            whitelist=_as_str_list(data.get("whitelist")),
            blacklist=_as_str_list(data.get("blacklist")),
            debounce_interval=_as_int(data.get("debounce_interval"), 60, minimum=0),
            download_dir=_as_str(data.get("download_dir"), "downloads") or "downloads",
            pack_format=pack_format,
            pack_password=_as_str(data.get("pack_password")),
            filename_show_password=_as_bool(
                data.get("filename_show_password"), False
            ),
            auto_delete_after_send=_as_bool(
                data.get("auto_delete_after_send"), True
            ),
            progress_style=progress_style,
            clean_temp_on_start=_as_bool(data.get("clean_temp_on_start"), True),
            max_concurrent_tasks=_as_int(
                data.get("max_concurrent_tasks"),
                DEFAULT_MAX_CONCURRENT_TASKS,
                minimum=1,
            ),
            download_timeout=_as_int(
                data.get("download_timeout"), DEFAULT_DOWNLOAD_TIMEOUT, minimum=60
            ),
            download_retry_times=_as_int(
                data.get("download_retry_times"), DEFAULT_RETRY_TIMES, minimum=0
            ),
            proxy=_as_str(data.get("proxy")),
            channels=_parse_channels(data.get("channels")),
        )

    def channel_options(self, channel_name: str) -> dict[str, Any] | None:
        """取出某渠道的模板配置；返回 None 表示用户没有添加该渠道。"""
        return self.channels.get(channel_name)

    @property
    def enabled_channel_names(self) -> list[str]:
        return [
            name
            for name, options in self.channels.items()
            if _as_bool(options.get("enable"), True)
        ]


def _parse_channels(raw: Any) -> dict[str, dict[str, Any]]:
    """把 template_list 的条目收敛成 {渠道名: 条目}。

    同一渠道被添加多次时后者覆盖前者——WebUI 里靠前的条目通常是用户已经废弃的旧配置。
    """
    channels: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, list):
        return channels
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _as_str(item.get(TEMPLATE_KEY))
        if not name:
            continue
        channels[name] = item
    return channels

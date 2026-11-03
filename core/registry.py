"""渠道注册表。

新增一个下载渠道的固定动作有五处（漏一处渠道就跑不起来）：
1. `core/channels/` 下新增一个 `BaseChannel` 子类
2. 把该类加进 `core/registry.py` 的 `CHANNEL_CLASSES`
3. 在 `main.py` 里加一个同名的 `cmd_<命令>` 薄方法（命令装饰器无法动态生成）
4. 在 `_conf_schema.json` 的 `channels.templates` 里加同名模板
5. 需要新依赖时补 `requirements.txt`，并同步 `metadata.yaml` / README / CHANGELOG 文案

打包、加密、发送不用动——它们在上层统一。
"""

from __future__ import annotations

from ..domain import PluginSettings
from .channel import BaseChannel

# 渠道实现放在 core/channels/，登记后才会被 build_channels 实例化
CHANNEL_CLASSES: tuple[type[BaseChannel], ...] = ()


def build_channels(settings: PluginSettings) -> dict[str, BaseChannel]:
    """按配置实例化渠道。用户没在 WebUI 添加的渠道不会出现在结果里。"""
    channels: dict[str, BaseChannel] = {}
    for channel_cls in CHANNEL_CLASSES:
        options = settings.channel_options(channel_cls.name)
        if options is None:
            continue
        channels[channel_cls.name] = channel_cls(options, settings)
    return channels


def command_map(channels: dict[str, BaseChannel]) -> dict[str, BaseChannel]:
    """命令名 → 渠道实例，供命令层按 `/命令` 找渠道。"""
    return {channel.command: channel for channel in channels.values()}


def describe_channels(channels: dict[str, BaseChannel]) -> str:
    """帮助文案里的渠道列表。"""
    if not channels:
        return "（当前没有任何渠道，请先在插件配置里添加下载渠道）"

    lines = []
    for channel in channels.values():
        state = "" if channel.enabled else "（未启用）"
        lines.append(f"/{channel.command} <ID> — {channel.display_name}{state}")
    return "\n".join(lines)

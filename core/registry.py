"""渠道注册表。

新增一个下载渠道的固定动作有六处（漏一处渠道就跑不起来）：
1. `core/channels/<渠道名>/` 新建渠道文件夹：`channel.py` 写 `BaseChannel` 子类
   （实现 `normalize_id` / `match_link` / `fetch_meta` / `download`），
   渠道私有模块（接入细节、进度上报等）都留在这个文件夹里，由它的 `__init__.py` 导出
2. 把该类加进 `core/registry.py` 的 `CHANNEL_CLASSES`
3. 在 `main.py` 里加一个同名的 `cmd_<命令>` 薄方法（命令装饰器无法动态生成）
4. 在 `_conf_schema.json` 的 `channels.templates` 里加同名模板
5. 需要新依赖时补 `requirements.txt`，并同步 `metadata.yaml` / README / CHANGELOG 文案
6. 实现 `default_options()`（至少含 `enable: True`），`build_channels()` 用它兜底

打包、加密、发送不用动——它们在上层统一。
"""

from __future__ import annotations

from ..domain import PluginSettings
from .channel import BaseChannel
from .channels.hanime1 import Hanime1Channel
from .channels.jmcomic import JMComicChannel
from .channels.nhentai import NhentaiChannel
from .channels.picacg import PicacgChannel
from .channels.wnacg import WnacgChannel

CHANNEL_CLASSES: tuple[type[BaseChannel], ...] = (
    JMComicChannel,
    NhentaiChannel,
    PicacgChannel,
    Hanime1Channel,
    WnacgChannel,
)


def build_channels(settings: PluginSettings) -> dict[str, BaseChannel]:
    """按配置实例化渠道。

    内置渠道默认启用：配置里没有该渠道时用它的 `default_options()` 建实例，
    因此新增渠道后老配置也能直接用；要停用就把对应渠道的「启用」关掉。
    """
    channels: dict[str, BaseChannel] = {}
    for channel_cls in CHANNEL_CLASSES:
        options = settings.channel_options(channel_cls.name)
        if options is None:
            options = channel_cls.default_options()
        channels[channel_cls.name] = channel_cls(options, settings)
    return channels


def command_map(channels: dict[str, BaseChannel]) -> dict[str, BaseChannel]:
    """命令名 → 渠道实例，供命令层按 `/命令` 找渠道；别名与主命令指向同一个渠道。"""
    mapping: dict[str, BaseChannel] = {}
    for channel in channels.values():
        for name in channel.command_names:
            mapping[name] = channel
    return mapping


def describe_channels(channels: dict[str, BaseChannel]) -> str:
    """帮助文案里的渠道列表：行首是缩写命令，全称与其它别名收进括号，不单独占行。"""
    if not channels:
        return "（当前没有任何渠道，请先在插件配置里添加下载渠道）"

    lines = []
    for channel in channels.values():
        state = "" if channel.enabled else "（未启用）"
        others = [name for name in channel.command_names if name != channel.display_command]
        also = f"；也可用 {'、'.join(f'/{name}' for name in others)}" if others else ""
        lines.append(
            f"/{channel.display_command} <ID 或链接> — {channel.display_name}{state}"
            f"（网络：{channel.proxy_summary()}{also}）"
        )
    return "\n".join(lines)


def resolve_link(
    channels: dict[str, BaseChannel], text: str
) -> tuple[BaseChannel, str] | None:
    """按完整作品链接找到渠道，返回 (渠道, 作品 ID)；认不出站点时返回 None。

    未启用的渠道也参与识别：命中未启用站点时能提示「渠道未启用」，
    比笼统的「认不出这条链接」更能指出问题所在。
    """
    for channel in channels.values():
        comic_id = channel.match_link(text)
        if comic_id:
            return channel, comic_id
    return None


def describe_links(channels: dict[str, BaseChannel]) -> str:
    """帮助文案里的链接示例：一行一个渠道，用户照着贴自己的作品页链接。"""
    lines = [
        f"{channel.display_name}：{channel.link_example}"
        for channel in channels.values()
        if channel.link_example
    ]
    return "\n".join(lines) if lines else "（当前没有支持链接直下的渠道）"

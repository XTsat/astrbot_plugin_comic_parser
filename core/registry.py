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
from .channels.ehentai import EHentaiChannel
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
    EHentaiChannel,
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


_NO_CHANNEL_TIP = "（还没有启用任何渠道，让管理员在插件配置里添加）"


def describe_channels(channels: dict[str, BaseChannel]) -> str:
    """帮助文案里的渠道列表：一行一个「命令 — 站点名」。

    用法在帮助的「下载」段落里统一讲过，这里不再逐个重复 `<ID 或链接>`；代理走法只有在
    各渠道不一致时才逐行标注——六行一模一样的「网络：直连」只是噪音。
    """
    if not channels:
        return _NO_CHANNEL_TIP

    enabled = [channel for channel in channels.values() if channel.enabled]
    proxies = {channel.proxy_summary() for channel in enabled}
    per_line_proxy = len(proxies) > 1
    lines = []
    for channel in channels.values():
        state = "" if channel.enabled else "（未启用）"
        others = [name for name in channel.command_names if name != channel.display_command]
        also = f"（也可用 {'、'.join(f'/{name}' for name in others)}）" if others else ""
        proxy = f"· {channel.proxy_summary()}" if per_line_proxy else ""
        lines.append(
            f"/{channel.display_command} — {channel.display_name}{state}"
            f"{also}{proxy}"
        )
    if len(proxies) == 1:
        lines.append(f"网络：全部{next(iter(proxies))}")
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


def describe_websites(channels: dict[str, BaseChannel]) -> str:
    """网站速查：每个渠道的官网 + 作品页链接示意，用户据此去站内搜作品、认 ID。

    作品页用真实域名 + `<ID>` 占位（不放具体数字），既说明 ID 在链接里的位置，
    也不会让用户拿到一个点不开的假链接。
    """
    if not channels:
        return _NO_CHANNEL_TIP

    entries: list[str] = []
    for channel in channels.values():
        state = "（未启用）" if not channel.enabled else ""
        lines = [f"/{channel.display_command} — {channel.display_name}{state}"]
        if channel.website:
            lines.append(f"  官网：{channel.website}")
        if channel.link_template:
            lines.append(
                f"  作品页：{channel.link_template}"
                f"（{channel.link_hint or '<ID> 处替换成作品 ID'}）"
            )
        entries.append("\n".join(lines))
    return "\n\n".join(entries)

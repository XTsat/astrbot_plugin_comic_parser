"""渠道连通性探测：按「渠道 + 作品 ID/链接」取一次元信息（能取到封面就一并取）。

**一处实现、两个入口**：聊天里的 `/comic 测试 <渠道> <ID>` 与面板「状态 → 渠道测试」
走的是同一套逻辑。两边各写一份的话，「面板通、命令不通」这种分叉迟早会出现，
而用户只会觉得插件坏了。

与 `core/health.py` 的分工：`health` 查**本机**（依赖装没装、配置冲不冲突），
毫秒级、不联网；这里查**站点**（这个渠道这台机器能不能真的取到东西），要真打上游、
会超时。`/comic 测试` 不带渠道时只跑 `health`，带了渠道才打这一趟——「我不想联网」
和「我想知道站点通不通」是两个问题，不该绑在一起。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..domain.constants import PANEL_CHANNEL_TIMEOUT, PLUGIN_NAME
from ..domain.models import ChannelSelfcheck
from ..domain.errors import PluginError

# 探测失败时的通用原因：站点被墙、风控、结构改版都在这一档，详情进日志
_GENERIC_HINT = (
    "可能被站点风控、站点结构有变或网络不通；"
    "可以去渠道配置里打开代理再试，详情看插件日志"
)


@dataclass(slots=True)
class ProbeOutcome:
    """一次渠道探测的结果。

    ``ok`` 为 False 时 ``error`` 一定是能直接给人看的中文；``ok`` 为 True 时
    ``meta`` 必有值、``cover`` 可能为 None（渠道不支持或这次没取到）。
    """

    channel: str
    display_command: str
    ok: bool = False
    comic_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    cover: bytes | None = None
    cover_error: str = ""
    error: str = ""
    elapsed: float = 0.0

    def render(self) -> str:
        """聊天里发的正文：先给结论（通不通、多久），再给这本的信息。"""
        head = f"{'✅' if self.ok else '❌'} {self.display_command}"
        if self.comic_id:
            head += f" {self.comic_id}"
        if not self.ok:
            return f"{head}：{self.error}"
        head += f" 通了 · {self.elapsed * 1000:.0f} ms"
        lines = [head]
        meta = self.meta
        if meta.get("title"):
            lines.append(f"标题：{meta['title']}")
        if meta.get("author"):
            lines.append(f"作者：{meta['author']}")
        if meta.get("page_count"):
            lines.append(f"页数：{meta['page_count']}")
        if int(meta.get("chapter_count") or 0) > 1:
            lines.append(f"章节：{meta['chapter_count']} 章")
        if meta.get("tags"):
            tags = "、".join(str(item) for item in list(meta["tags"])[:8])
            lines.append(f"标签：{tags}")
        if self.cover is not None:
            lines.append(f"封面：已取到（{len(self.cover) // 1024} KB）")
        elif self.cover_error:
            lines.append(f"封面：{self.cover_error}")
        return "\n".join(lines)


def pick_channel(channels: Mapping[str, Any], name: str) -> Any | None:
    """按全称、简写或渠道名找到渠道对象。

    面板与命令层共用：两边都认「全称 / 第一个别名 / 内部名」，用户填哪个都能命中。
    """
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    for key, channel in channels.items():
        if wanted in {str(key).lower(), str(getattr(channel, "command", "")).lower()}:
            return channel
        names = {str(item).lower() for item in getattr(channel, "command_names", ())}
        aliases = {str(item).lower() for item in getattr(channel, "command_aliases", ())}
        if wanted in names or wanted in aliases:
            return channel
    return None


def looks_like_link(text: str) -> bool:
    """粗判是不是作品链接：命令层与面板都先看这个，再决定要不要走链接路由。"""
    raw = str(text or "").strip().lower()
    return raw.startswith(("http://", "https://"))


def resolve_target(
    channels: Mapping[str, Any], channel: Any, text: str
) -> tuple[Any, str]:
    """把「渠道 + 输入」收敛成「实际渠道 + 规范化 ID」。

    贴的是作品链接就按链接走：命令与渠道只是入口，不能拿 A 站的 ID 去问 B 站。

    Raises:
        PluginError: 链接认不出站点，或 ID 写法不对（消息可直接展示）。
    """
    raw = str(text or "").strip()
    if looks_like_link(raw):
        # 链接路由要的是渠道表，延迟 import 免得本模块与 registry 互相牵制
        from .registry import resolve_link

        resolved = resolve_link(channels, raw)
        if resolved is None:
            raise PluginError(
                "认不出这条链接属于哪个渠道，检查一下链接或改用作品 ID"
            )
        channel, comic_id = resolved
        return channel, comic_id
    try:
        return channel, channel.normalize_id(raw)
    except PluginError:
        raise
    except Exception as exc:  # 各渠道的 ID 解析抛的异常类型不统一
        raise PluginError(f"作品 ID 看着不对：{exc}") from exc


async def probe_channel(
    channels: Mapping[str, Any],
    channel_name: str,
    text: str,
    work_dir: Path,
    *,
    timeout: float = PANEL_CHANNEL_TIMEOUT,
    with_cover: bool = False,
) -> ProbeOutcome:
    """按渠道取一次元信息（可选取封面），把成败与耗时收敛成一份结果。

    Args:
        channels: 渠道对象表（键是渠道名）。
        channel_name: 渠道全称或简写。
        text: 作品 ID 或作品链接；链接指向别的站点时按链接那家走。
        work_dir: 取元信息时渠道可以落临时文件的目录。
        timeout: 单次取元信息/封面的超时（秒）。
        with_cover: 是否顺手取一张封面；面板要预览，聊天命令只要结论。

    Returns:
        ProbeOutcome: ``ok`` 为 False 时 ``error`` 是可直接展示的中文。
    """
    started = asyncio.get_running_loop().time()
    channel = pick_channel(channels, channel_name)
    if channel is None:
        return ProbeOutcome(
            channel=channel_name,
            display_command=channel_name or "?",
            error=f"没有这个渠道：{channel_name or '（空）'}",
        )

    outcome = ProbeOutcome(
        channel=str(getattr(channel, "command", channel_name)),
        display_command=str(getattr(channel, "display_command", channel_name)),
    )
    if not getattr(channel, "enabled", False):
        outcome.error = (
            f"渠道 {outcome.display_command} 没有启用；先在插件设置里打开它"
        )
        return outcome

    try:
        channel, comic_id = resolve_target(channels, channel, text)
    except PluginError as exc:
        outcome.error = str(exc)
        return outcome
    outcome.comic_id = str(comic_id)

    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        meta = await asyncio.wait_for(
            channel.fetch_meta(comic_id, work_dir), timeout=timeout
        )
    except asyncio.TimeoutError:
        outcome.error = (
            f"取元信息超过 {timeout:.0f} 秒没响应，{_GENERIC_HINT}"
        )
        return outcome
    except PluginError as exc:
        # 渠道已经把「下架 / ID 不对 / 连不上」翻译成人话了，原样透出
        outcome.error = str(exc)
        return outcome
    except Exception as exc:  # 各渠道抛的异常类型不统一
        logger.warning(
            f"[{PLUGIN_NAME}] 渠道探测失败 {outcome.channel}/{comic_id}: {exc}"
        )
        outcome.error = f"取元信息失败：{_GENERIC_HINT}"
        return outcome

    outcome.ok = True
    outcome.elapsed = asyncio.get_running_loop().time() - started
    outcome.meta = {
        "channel": meta.channel,
        "comic_id": meta.comic_id,
        "title": meta.title,
        "author": meta.author,
        "page_count": meta.page_count,
        "chapter_count": meta.chapter_count,
        "tags": list(meta.tags),
    }

    if not with_cover:
        return outcome
    if not getattr(channel, "supports_cover", False):
        outcome.cover_error = (
            f"渠道 {outcome.display_command} 只支持查元信息，取不到封面"
        )
        return outcome

    try:
        raw_cover = await asyncio.wait_for(
            channel.fetch_cover(comic_id), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"[{PLUGIN_NAME}] 渠道探测取封面超时 {outcome.channel}/{comic_id}"
            f"（{timeout:.0f}s）"
        )
        raw_cover = None
    except Exception as exc:  # 取封面失败不该把「元信息通了」这个结论推翻
        logger.warning(
            f"[{PLUGIN_NAME}] 渠道探测取封面失败 {outcome.channel}/{comic_id}: {exc}"
        )
        raw_cover = None

    if raw_cover:
        outcome.cover = raw_cover
        return outcome

    # 渠道自己在 fetch_cover 里记了失败原因（哪几个域名、什么错）就透给用户，
    # 光说「没取到」用户没法判断该开代理还是换域名
    reason = str(getattr(channel, "cover_error", "") or "").strip()
    outcome.cover_error = (
        f"没取到封面：{reason}" if reason else f"没取到封面：{_GENERIC_HINT}"
    )
    return outcome


async def selfcheck_channels(
    channels: Mapping[str, Any],
    *,
    timeout: float = 15.0,
    concurrency: int = 6,
) -> list[ChannelSelfcheck]:
    """并发问每个**已启用**渠道「现在能不能用」，各走各的域名与代理。

    并发而不是串行：六个渠道串起来要等一分多钟，用户早就走了。
    单个渠道内部失败只影响它自己——这是自检，不是下载，没有「整单失败」这回事。
    """
    targets = [item for item in channels.values() if getattr(item, "enabled", False)]
    if not targets:
        return []

    semaphore = asyncio.Semaphore(max(1, concurrency))

    def route_of(channel: Any) -> str:
        """这个渠道这一趟会走的线路。失败时同样要显示——直连挂了该开代理，
        代理挂了该换线路，两者的下一步完全不同，不写出来用户只能猜。"""
        resolve = getattr(channel, "resolve_proxy", None)
        if not callable(resolve):
            return ""
        try:
            proxy, source = resolve()
        except Exception:  # noqa: BLE001 - 读线路失败不该让自检失败
            return ""
        return source if proxy else source

    async def guarded(channel: Any) -> ChannelSelfcheck:
        async with semaphore:
            try:
                return await asyncio.wait_for(channel.selfcheck(timeout), timeout + 5)
            except asyncio.TimeoutError:
                return ChannelSelfcheck(
                    channel=str(getattr(channel, "name", "")),
                    display_command=str(getattr(channel, "display_command", "")),
                    enabled=True,
                    proxy_source=route_of(channel),
                    reason=f"超过 {timeout:g} 秒没有回应",
                    hint="多半是线路问题：打开该渠道的代理再试",
                )
            except Exception as exc:  # noqa: BLE001 - 自检本身绝不能因为某个渠道炸掉而中断
                logger.warning(
                    f"[{PLUGIN_NAME}] 平台自检异常 {getattr(channel, 'name', '?')}: {exc}"
                )
                return ChannelSelfcheck(
                    channel=str(getattr(channel, "name", "")),
                    display_command=str(getattr(channel, "display_command", "")),
                    enabled=True,
                    proxy_source=route_of(channel),
                    reason="自检时出错",
                    hint="详情看插件日志",
                )

    results = await asyncio.gather(*(guarded(item) for item in targets))
    # 能用的排前面：一眼看到「哪几个还行」，不用在失败项里找
    return sorted(results, key=lambda item: (not item.ok, item.channel))


def render_selfcheck(results: list[ChannelSelfcheck]) -> str:
    """把各渠道的自检结论拼成一条消息：先给总览，再逐个说清卡在哪。"""
    if not results:
        return "ℹ️ 没有已启用的渠道：先去插件配置里打开至少一个"

    ok = [item for item in results if item.ok]
    lines = [f"🩺 平台自检：{len(ok)}/{len(results)} 个渠道可用"]
    for item in results:
        mark = "✅" if item.ok else "❌"
        line = f"{mark} {item.display_command}"
        # 失败的也要写清走的哪条路：同样是「连不上」，直连和代理指向完全不同的下一步
        # （直连失败该开代理，代理失败该换线路），不写出来用户只能猜
        route = f" · {item.proxy_source}" if item.proxy_source else ""
        if item.ok:
            line += f"（{item.latency_ms}ms{route}）"
        else:
            line += f"（{item.reason}{route}）"
        lines.append(line)
        if not item.ok and item.hint:
            lines.append(f"　　→ {item.hint}")

    failed = [item for item in results if not item.ok]
    if failed:
        # 只提示最常见的两种成因：其余原因在逐条里已经写清楚了
        lines.append("")
        lines.append("连不上时先看两件事：该渠道的「使用代理」开没开、站点域名还对不对")
    return "\n".join(lines)


def split_target_args(args: str) -> tuple[str, str]:
    """把 ``<渠道> <ID 或链接>`` 拆成两段；只剩一个词时整段当渠道。

    作品链接里不会有空格，ID 也不会，所以按第一个空格切就够；空串原样返回，
    由调用方决定「没给渠道」时该走哪条路。
    """
    text = " ".join((args or "").split())
    if not text:
        return "", ""
    head, _, rest = text.partition(" ")
    # 只有一个词时它是渠道（`/comic 测试 jm` → 列出该渠道该怎么测），不是作品 ID：
    # 让用户少打一个词就得到「还差什么」的提示
    return head, rest.strip()


def known_channel_names(channels: Mapping[str, Any]) -> str:
    """把可用渠道命令拼成一行，供「没给渠道」时的提示用。"""
    names = sorted(
        str(getattr(item, "display_command", name)) for name, item in channels.items()
    )
    return " / ".join(names)


__all__ = [
    "ProbeOutcome",
    "render_selfcheck",
    "selfcheck_channels",
    "known_channel_names",
    "looks_like_link",
    "pick_channel",
    "probe_channel",
    "resolve_target",
    "split_target_args",
]

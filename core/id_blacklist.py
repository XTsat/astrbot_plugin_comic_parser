"""作品 ID 黑名单：按「哪一本」拦截下载、简介与封面。

与会话白/黑名单（`guard.py`）分工不同：那条管「谁在用」，这条管「哪本作品」。
布局上只留**一份全量清单**，每条可带渠道前缀：

- `123456`：所有渠道都拦这个 ID
- `jm:123456`：只拦禁漫天堂的这本
- `https://18comic.vip/album/123456/`：按链接自带的站点认渠道

各站的作品 ID 空间互不相交，绝大多数部署一条全量清单就够；前缀留给「同一个 ID
只在某个渠道要拦」这种少数情况，因此不必给每个渠道模板各配一份清单（那样反而要在
五处配置里找同一本作品）。

命中后拦哪几件事由配置里的复选框决定（下载 / 简介 / 封面），见
`domain/constants.py` 的 `ID_BLACKLIST_ACTIONS`。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ..domain import PluginError, PluginSettings
from ..domain.constants import DEFAULT_ID_BLACKLIST_ACTIONS, ID_BLACKLIST_ACTIONS
from .channel import BaseChannel

# 条目里的渠道前缀分隔符：中英文冒号都认（用户在中文输入法下很容易敲出全角冒号）
_SCOPE_SEPARATORS = (":", "：")
# 带主机名的地址才算链接：裸 ID 与站名式分享文本交给各渠道自己的 ID 解析
_LINK_PREFIX = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def _norm(value: str) -> str:
    """比较用的规范形：去空白、转小写。

    ID 大小写只在哔咔这类十六进制 ID 上有区别，统一小写比较免得用户抄错大小写就漏拦。
    """
    return (value or "").strip().lower()


@dataclass(frozen=True, slots=True)
class BlockedWork:
    """一条生效的黑名单：渠道内部名为 None 表示对所有渠道生效。"""

    channel: str | None
    work_id: str


class IdBlacklist:
    """把配置里的条目解析成可比较的 (渠道, 作品 ID)，并按作用范围判定拦截。"""

    def __init__(
        self,
        entries: Sequence[str],
        actions: Sequence[str],
        channels: Mapping[str, BaseChannel] | None = None,
    ) -> None:
        self._actions = tuple(
            action for action in ID_BLACKLIST_ACTIONS if action in set(actions or ())
        )
        self._entries: list[BlockedWork] = []
        # 解析不了的条目留个说法：自检（`/comic 测试`）据此提醒管理员，而不是静默不生效
        self._unresolved: list[str] = []
        self._parse(entries or (), channels or {})

    @classmethod
    def from_settings(
        cls,
        settings: PluginSettings,
        channels: Mapping[str, BaseChannel] | None = None,
    ) -> "IdBlacklist":
        """按配置建一份：没配条目时得到一个「什么都不拦」的空表。"""
        # 空列表是「用户把选项全取消」的合法取值（等于黑名单暂时不生效），
        # 不能像脏值那样回落成默认值，所以只对「压根没有这个字段」兜底
        actions = getattr(settings, "id_blacklist_actions", None)
        if actions is None:
            actions = DEFAULT_ID_BLACKLIST_ACTIONS
        return cls(getattr(settings, "id_blacklist", []) or [], actions, channels)

    # ---- 供上层查询 ----

    @property
    def actions(self) -> tuple[str, ...]:
        """生效的作用范围（已按常量顺序排好、剔掉不认识的值）。"""
        return self._actions

    @property
    def entries(self) -> tuple[BlockedWork, ...]:
        return tuple(self._entries)

    @property
    def unresolved(self) -> tuple[str, ...]:
        """认不出渠道的条目原文：配置里写了错别字时会出现在这里。"""
        return tuple(self._unresolved)

    def blocked(self, channel: BaseChannel, work_id: str, action: str) -> bool:
        """这本作品在指定动作上是否被拦。

        Args:
            channel: 请求命中的渠道实例。
            work_id: 该渠道规范化后的作品 ID。
            action: `download` / `info` / `cover` 之一。
        """
        if action not in self._actions or not self._entries:
            return False
        key = _norm(work_id)
        if not key:
            return False
        for entry in self._entries:
            if entry.channel is not None and entry.channel != channel.name:
                continue
            if self._same_work(channel, entry.work_id, key):
                return True
        return False

    # ---- 解析 ----

    def _same_work(self, channel: BaseChannel, raw: str, key: str) -> bool:
        """条目与本次请求是不是同一本。

        条目允许写成链接或 `gid:token` 这类原始形态，所以除了直接比对，再让渠道自己
        规范化一次：`https://e-hentai.org/g/123/abc/` 与规范形 `123-abc` 才能对上。
        """
        if _norm(raw) == key:
            return True
        try:
            return _norm(channel.normalize_id(raw)) == key
        except PluginError:
            # 条目形态本渠道认不出（例如把 nhentai 的链接写进了 jm 前缀下）——不算命中
            return False
        except Exception:  # noqa: BLE001 - 渠道 ID 解析各异，任何异常都只意味着「不是这本」
            return False

    def _parse(
        self, entries: Iterable[str], channels: Mapping[str, BaseChannel]
    ) -> None:
        aliases = _channel_aliases(channels)
        for raw in entries:
            if not isinstance(raw, str):
                continue
            text = raw.strip()
            if not text:
                continue
            scope, token = _split_scope(text, aliases)

            if scope is not None:
                # 带了渠道前缀就由该渠道自己规范化一次：`jm:https://18comic.vip/album/1/`
                # 与 `jm:1` 记成同一条，也不必在比对时借用别的渠道的正则去猜
                channel = channels.get(scope)
                if channel is not None:
                    normalized = _normalize_for(channel, token)
                    if normalized:
                        token = normalized
                    elif _is_link(token):
                        # 前缀说的渠道认不出这条链接，多半是贴了别站的链接
                        self._unresolved.append(text)
                self._entries.append(BlockedWork(scope, token))
                continue

            if _is_link(token):
                # 链接自带站点：能归到哪个渠道就记成该渠道，归不出来才留作全量条目
                resolved = _resolve_link(channels, token)
                if resolved is None:
                    self._unresolved.append(text)
                    self._entries.append(BlockedWork(None, token))
                    continue
                self._entries.append(BlockedWork(*resolved))
                continue

            if _looks_like_scope(token):
                # `jmcomic1:123` 这种前缀写错的情况：既不匹配任何渠道，也不该被当成
                # 一个「全量 ID」静默吞掉
                self._unresolved.append(text)
            self._entries.append(BlockedWork(None, token))


def _channel_aliases(
    channels: Mapping[str, BaseChannel],
) -> dict[str, str]:
    """别名/全称/内部名 → 渠道内部名，供条目里的前缀查表。"""
    mapping: dict[str, str] = {}
    for name, channel in channels.items():
        mapping[_norm(name)] = channel.name
        for alias in getattr(channel, "command_names", ()) or ():
            if alias:
                mapping[_norm(alias)] = channel.name
    return mapping


def _split_scope(text: str, aliases: Mapping[str, str]) -> tuple[str | None, str]:
    """拆出「渠道前缀:作品」里的渠道；没有前缀（或前缀不是任何渠道）时原样返回。

    只按第一个分隔符拆：`eh:1234567:0123456789` 的后半段仍是 E-Hentai 的 `gid:token`。
    """
    for separator in _SCOPE_SEPARATORS:
        head, found, tail = text.partition(separator)
        if not found:
            continue
        scope = aliases.get(_norm(head))
        if scope is not None and tail.strip():
            return scope, tail.strip()
        return None, text
    return None, text


def _looks_like_scope(text: str) -> bool:
    """条目里带前缀分隔符、前半段又写着字母：多半是把渠道名写错了。

    `1234567:0123456789` 这类纯数字两段是 E-Hentai 的 `gid:token`，不是写错前缀，
    所以只有前半段含字母时才当作「疑似渠道名」。
    """
    for separator in _SCOPE_SEPARATORS:
        head, found, tail = text.partition(separator)
        if found and tail.strip() and any(char.isalpha() for char in head):
            return True
    return False


def _is_link(text: str) -> bool:
    return bool(_LINK_PREFIX.match(text))


def _normalize_for(channel: BaseChannel, token: str) -> str | None:
    """用渠道自己的 ID 解析把条目收敛成规范形；认不出时返回 None。"""
    try:
        return channel.normalize_id(token)
    except Exception:  # noqa: BLE001 - 条目形态各异，认不出等于「这条不生效」
        return None


def _resolve_link(
    channels: Mapping[str, BaseChannel], text: str
) -> tuple[str, str] | None:
    """按链接找到渠道与规范化后的 ID；认不出站点时返回 None。"""
    for channel in channels.values():
        try:
            comic_id = channel.match_link(text)
        except Exception:  # noqa: BLE001 - 渠道链接解析各异，认不出就当不是它
            continue
        if comic_id:
            return channel.name, comic_id
    return None


__all__ = [
    "BlockedWork",
    "IdBlacklist",
]

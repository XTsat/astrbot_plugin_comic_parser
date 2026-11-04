"""作品链接的解析小工具：把用户粘贴的链接拆成主机名与路径。

为什么要拆开给渠道自己判断：有的站点路径特征已经足够唯一（`/album/<id>`、`/g/<id>`），
域名又随镜像天天换，锁死域名反而认不出；有的站点（Hanime1、哔咔）共用 `/comic/<id>`
这种通用路径，必须靠域名才能分清。所以主机名与路径分开返回，由渠道决定认哪个。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# 主机名不含斜杠与空白；路径取到空白为止——粘贴分享文本时链接后面常还跟着一堆别的内容
_LINK = re.compile(r"https?://([^/\s]+)([^\s]*)", re.IGNORECASE)


def split_link(text: str) -> tuple[str, str]:
    """把输入里的第一个 http(s) 链接拆成 (主机名, 路径)。

    主机名转小写并去掉端口与末尾的点（`WWW.Hanime1.me:443` → `www.hanime1.me`），
    路径保留前导 `/`。输入里没有链接时返回 `("", "")`。
    """
    matched = _LINK.search(text or "")
    if matched is None:
        return "", ""
    host = matched.group(1).lower().split(":", 1)[0].rstrip(".")
    return host, matched.group(2)


def host_matches(host: str, domains: Iterable[str]) -> bool:
    """主机名是否属于给定域名，子域也算（`www.hanime1.me` 属于 `hanime1.me`）。"""
    if not host:
        return False
    for raw in domains:
        domain = raw.strip().lower().rstrip(".")
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            return True
    return False

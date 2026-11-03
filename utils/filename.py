"""文件名处理：上游标题可能带路径分隔符、控制字符或超长，落盘前必须清洗。"""

from __future__ import annotations

import re

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")

# 预留 ".zip" / 分段序号 "#PWxxx" 等后缀的空间
DEFAULT_NAME_LIMIT = 80


def sanitize_filename(name: str, limit: int = DEFAULT_NAME_LIMIT) -> str:
    """清洗成可安全落盘的文件名（不含扩展名）。"""
    cleaned = _ILLEGAL_CHARS.sub("_", name)
    cleaned = _WHITESPACE.sub(" ", cleaned)
    # 两端残留的 "_" 是被替换掉的非法字符留下的痕迹，一并去掉
    cleaned = cleaned.strip(" ._")
    if not cleaned:
        return "comic"
    return cleaned[:limit].strip(" ._") or "comic"


def build_package_name(
    title: str,
    comic_id: str,
    password: str = "",
    show_password: bool = False,
) -> str:
    """生成本次打包的基名（不含扩展名）。

    密码提示只跟随 `show_password` 开关出现：开启后把密码写进文件名，
    方便用户在同一条聊天记录里直接看到；关闭时密码只能来自插件配置。
    """
    base = f"{sanitize_filename(title)}[{comic_id}]"
    if password and show_password:
        base = f"{base}#PW{password}"
    return sanitize_filename(base, limit=100)

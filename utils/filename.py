"""文件名处理：上游标题可能带路径分隔符、控制字符或超长，落盘前必须清洗。"""

from __future__ import annotations

import re

from ..domain.constants import (
    DEFAULT_FILENAME_TEMPLATE,
    FILENAME_TEMPLATES,
)

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
# 未设密码时模板里的密码段要整段消失，不能留下孤零零的 "()" 或 "#PW"
_PASSWORD_SUFFIX = re.compile(r"\(\{password\}\)|#PW\{password\}")

# 预留 ".zip" / 分段序号 "(密码)" 等后缀的空间
DEFAULT_NAME_LIMIT = 80


class _SafeDict(dict[str, str]):
    """模板里写了不认识的占位符时留空，而不是抛 KeyError。"""

    def __missing__(self, key: str) -> str:
        return ""


def sanitize_filename(name: str, limit: int = DEFAULT_NAME_LIMIT) -> str:
    """清洗成可安全落盘的文件名（不含扩展名）。"""
    cleaned = _ILLEGAL_CHARS.sub("_", name)
    cleaned = _WHITESPACE.sub(" ", cleaned)
    # 两端残留的 "_" 是被替换掉的非法字符留下的痕迹，一并去掉
    cleaned = cleaned.strip(" ._")
    if not cleaned:
        return "comic"
    return cleaned[:limit].strip(" ._") or "comic"


def resolve_template(template: str, custom_template: str = "") -> str:
    """把配置里的模板选项翻译成模板字符串；未知选项回落默认模板。"""
    if template == "custom":
        return custom_template.strip() or FILENAME_TEMPLATES[DEFAULT_FILENAME_TEMPLATE]
    return FILENAME_TEMPLATES.get(template) or FILENAME_TEMPLATES[
        DEFAULT_FILENAME_TEMPLATE
    ]


def build_package_name(
    title: str,
    comic_id: str,
    template: str = DEFAULT_FILENAME_TEMPLATE,
    custom_template: str = "",
    password: str = "",
    author: str = "",
) -> str:
    """按模板生成本次打包的基名（不含扩展名）。

    密码是否出现在文件名里由模板决定：模板带 `{password}` 才写，没设密码时该段整体去掉。
    """
    pattern = resolve_template(template, custom_template)
    if not password:
        pattern = _PASSWORD_SUFFIX.sub("", pattern)
    rendered = pattern.format_map(
        _SafeDict(
            title=title,
            id=comic_id,
            author=author,
            password=password,
        )
    )
    return sanitize_filename(rendered, limit=100)

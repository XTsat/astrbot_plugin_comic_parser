"""命令末尾的格式词：`/jm 123456 pdf` 只改这一次的产物形态，不动全局配置。"""

from __future__ import annotations

from ..domain.constants import COVER_WORDS, PACK_FORMAT_ALIASES, PACK_FORMAT_LABELS


def split_pack_format(text: str) -> tuple[str, str | None]:
    """切出末尾的格式词，返回 `(剩余参数, 规范格式名或 None)`。

    只在「有前文 + 末尾能对上已知别名」时才切：单独一个 `/jm pdf` 里没有 ID，
    把它当成格式覆盖只会让用户看到「没给 ID」而不是「pdf 不是 ID」，所以原样留下。
    关键词搜索（`/jm sou 长图`）与翻页由调用方先处理，不会被这里吃掉。
    """
    body, sep, tail = text.rpartition(" ")
    if not sep:
        return text, None
    fmt = PACK_FORMAT_ALIASES.get(tail.strip().lower())
    if fmt is None:
        return text.strip(), None
    return body.strip(), fmt


def split_cover_word(text: str) -> tuple[str, bool]:
    """切出末尾的「封面」词，返回 "剩余参数, 是否只要封面"。

    与格式词同一套规则（只在「有前文 + 末尾命中」时才切）：单独一个 /jm 封面 里没有
    ID，切掉只会让用户看到「没给 ID」，所以原样留下、交给 ID 解析去报错。
    """
    body, sep, tail = text.rpartition(" ")
    if not sep:
        return text, False
    if tail.strip().lower() not in COVER_WORDS:
        return text.strip(), False
    return body.strip(), True


def describe_pack_format(fmt: str) -> str:
    """格式的中文展示名；认不出的格式原样返回，方便把它回显给用户。"""
    return PACK_FORMAT_LABELS.get(fmt, fmt)

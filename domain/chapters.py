"""章节范围：把「章节 1-20」「章节 最新 5」这类写法解析成具体的章节序号。

连载漫画动辄几百章，整本下载既没必要也很占盘，所以命令层允许在末尾写一段范围，
由渠道按自己的章节清单挑出对应的那几章。本模块只做解析与序号计算，不认识任何渠道：
渠道拿 :meth:`ChapterSpec.indices` 拿到 1 基序号列表，再自己从章节清单里筛。

范围写法（`,`/`，`/`、`拐开的每一段都会被合并去重，结果永远按章节自然顺序排列）::

    3          第 3 章
    1-20       第 1 到 20 章（写成 20-1 也按 1-20 算）
    -20        前 20 章
    100-       第 100 章到最后一章
    最新5      最后 5 章（连载追更时最常用）
    全部       整本（与不写范围等价）
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from .errors import ChapterError

# 「最后 N 章」的前缀：最新 / last / 末 / 尾，后面直接跟数字
_TAIL_PREFIXES: Final = ("最新", "last", "末", "尾")
# 「整本」的写法：留一个显式说法，省得用户猜「不写」和「写全部」有什么区别
_ALL_WORDS: Final = frozenset({"全部", "整本", "所有", "all", "*"})
# 一段的结束符：中英文逗号、顿号、分号、斜杠、空白都当分隔
_SPLIT_PATTERN: Final = re.compile(r"[,，、;；/\s]+")
# 一段左右的空白不构成分隔符（`1 - 5` 仍是一段），所以先把 `-` 两侧的空白抹掉
_DASH_SPACES: Final = re.compile(r"\s*[-－—–~～至到]\s*")
# 全角数字、全角连字符与中文「至/到」都收敛成半角写法，用户从网页抄下来常常是混合的
_TRANSLATION: Final = str.maketrans(
    {
        "０": "0",
        "１": "1",
        "２": "2",
        "３": "3",
        "４": "4",
        "５": "5",
        "６": "6",
        "７": "7",
        "８": "8",
        "９": "9",
        "－": "-",
        "—": "-",
        "–": "-",
        "~": "-",
        "～": "-",
        "至": "-",
        "到": "-",
    }
)
# 「第 3 章」「3话」这类写法里的装饰词：去掉不影响含义
_DECORATIONS: Final = str.maketrans({"第": "", "章": "", "话": "", "話": "", "集": ""})


@dataclass(frozen=True, slots=True)
class ChapterEntry:
    """渠道给出的一章：1 基序号、标题，以及页数（渠道不知道就留 0）。"""

    index: int
    title: str = ""
    pages: int = 0


@dataclass(frozen=True, slots=True)
class _Span:
    """范围里的一段：`第 start 到 end 章`；`start` 为空表示从第 1 章起，`end` 为空表示到末尾。

    ``tail`` 是「最后 N 章」的写法，非 0 时它自己就是完整含义，忽略 start/end。
    """

    start: int | None = None
    end: int | None = None
    tail: int = 0

    def indices(self, total: int) -> list[int]:
        """这一段落在 ``total`` 章的作品里是哪些序号（超出范围的部分直接截掉）。"""
        if total <= 0:
            return []
        if self.tail:
            begin = max(1, total - self.tail + 1)
            return list(range(begin, total + 1))
        begin = 1 if self.start is None else max(1, self.start)
        end = total if self.end is None else min(self.end, total)
        if begin > end:
            return []
        return list(range(begin, end + 1))

    def label(self) -> str:
        if self.tail:
            return f"最新 {self.tail} 章"
        if self.start is None and self.end is None:
            return "整本"
        if self.start is None:
            return f"前 {self.end} 章"
        if self.end is None:
            return f"第 {self.start} 章起"
        if self.start == self.end:
            return f"第 {self.start} 章"
        return f"第 {self.start}–{self.end} 章"


@dataclass(frozen=True, slots=True)
class ChapterSpec:
    """用户写的章节范围（已规范化）。"""

    text: str
    spans: tuple[_Span, ...] = field(default_factory=tuple)

    # ---- 解析 ----
    @classmethod
    def parse(cls, raw: str) -> ChapterSpec:
        """把用户写法解析成范围；看不懂时抛 :class:`ChapterError`（消息可直接展示）。"""
        normalized = _DASH_SPACES.sub("-", str(raw or "").strip().translate(_TRANSLATION))
        tokens = [token for token in _SPLIT_PATTERN.split(normalized) if token]
        if not tokens:
            raise ChapterError(
                "章节范围是空的：写成「章节 1-20」这样，或者干脆不写章节"
            )

        spans: list[_Span] = []
        for token in tokens:
            spans.append(_parse_token(token))
        return cls(text=_render_text(spans), spans=tuple(spans))

    # ---- 计算 ----
    def indices(self, total: int) -> list[int]:
        """在 ``total`` 章的作品里要下载哪些章（1 基、去重、按自然顺序）。"""
        picked: set[int] = set()
        for span in self.spans:
            picked.update(span.indices(total))
        return sorted(picked)

    def is_all(self, total: int) -> bool:
        """是不是整本：范围覆盖了全部章节时，摘要里不必多写一行「仅下载」。"""
        return total > 0 and self.indices(total) == list(range(1, total + 1))

    def label(self, total: int = 0) -> str:
        """人话描述：`第 1–20 章`、`最新 5 章`；``total`` 已知且全覆盖时只说「整本」。"""
        if len(self.spans) == 1:
            text = self.spans[0].label()
        else:
            text = "、".join(span.label() for span in self.spans)
        return "整本" if total > 0 and self.is_all(total) else text


def _parse_token(token: str) -> _Span:
    """解析一段：`数字` / `a-b` / `-b` / `a-` / `最新N` / `全部`。"""
    cleaned = token.strip().translate(_DECORATIONS).lower()
    if not cleaned:
        raise ChapterError(f"章节范围「{token}」看不懂：写成「章节 1-20」这样")

    if cleaned in _ALL_WORDS:
        return _Span()

    for prefix in _TAIL_PREFIXES:
        if cleaned.startswith(prefix):
            digits = cleaned[len(prefix) :].lstrip("-")
            if digits.isdigit() and int(digits) > 0:
                return _Span(tail=int(digits))
            raise ChapterError(
                f"章节范围「{token}」看不懂：「最新」后面要跟一个章数，如「最新5」"
            )

    if cleaned.isdigit():
        number = int(cleaned)
        if number <= 0:
            raise ChapterError(f"章节范围「{token}」看不懂：章节序号从 1 开始")
        return _Span(start=number, end=number)

    head, sep, tail = cleaned.partition("-")
    if not sep:
        raise ChapterError(
            f"章节范围「{token}」看不懂：写成「1-20」「3」「-20」「100-」或「最新5」"
        )
    if (head and not head.isdigit()) or (tail and not tail.isdigit()):
        raise ChapterError(
            f"章节范围「{token}」看不懂：写成「1-20」「3」「-20」「100-」或「最新5」"
        )
    if not head and not tail:
        raise ChapterError(f"章节范围「{token}」看不懂：至少要给一个章数")

    start = int(head) if head else None
    end = int(tail) if tail else None
    if (start is not None and start <= 0) or (end is not None and end <= 0):
        raise ChapterError(f"章节范围「{token}」看不懂：章节序号从 1 开始")
    # 写成 20-1 的人想表达的还是这一区间；方向由章节自身的先后决定，不按倒序下
    if start is not None and end is not None and start > end:
        start, end = end, start
    return _Span(start=start, end=end)


def _render_text(spans: Sequence[_Span]) -> str:
    """把解析出的各段拼回一段紧凑写法（写进日志、记录与回显都用它）。"""
    parts: list[str] = []
    for span in spans:
        if span.tail:
            parts.append(f"最新{span.tail}")
        elif span.start is None and span.end is None:
            parts.append("全部")
        elif span.start is None:
            parts.append(f"-{span.end}")
        elif span.end is None:
            parts.append(f"{span.start}-")
        elif span.start == span.end:
            parts.append(str(span.start))
        else:
            parts.append(f"{span.start}-{span.end}")
    return ",".join(parts)


def select_entries(
    entries: Sequence[ChapterEntry], spec: ChapterSpec
) -> list[ChapterEntry]:
    """按范围从章节清单里挑出要下的章；一章都没挑中时抛 :class:`ChapterError`。

    越界与笔误都会落在这里，所以文案要说清「这本有几章」，用户才知道是不是把范围写错了。
    """
    if not entries:
        raise ChapterError("这本没有章节清单，没法按章节下载")
    picked = set(spec.indices(len(entries)))
    chosen = [entry for entry in entries if entry.index in picked]
    if not chosen:
        raise ChapterError(
            f"这本只有 {len(entries)} 章，「{spec.text}」没有对应的章节"
        )
    return chosen

"""搜索条件解析：把 ``tag:巨乳 -tag:NTR 堕落`` 拆成结构化条件。

各站上游的筛选能力并不一致：JM 用 ``main_tag`` 区分搜索类型，E-Hentai 用命名空间，
其余渠道只收一个关键词。因此命令层先做一次统一解析，再由各渠道按自己的能力翻译；
解析层只管语法，不判断某个渠道支不支持。

语法要点：

- 前缀只认词首，大小写不敏感，半角/全角冒号都认；
- 值默认到下一个空白结束；含空格的值用引号包起来（``tag:"big breasts"``）；
- 前缀前加 ``-``（也认全角 ``－``）表示排除；
- 不认识的 ``xxx:`` 不拦截，原样留给自由关键词（E-Hentai 的 ``female:`` 等原生语法可透传）。
"""

from __future__ import annotations

import re

from ..domain import SearchFilter, SearchQuery, SearchQueryError

# 规范化字段名 → 用户可读名称（只用于结果提示，不参与解析）
FIELD_LABELS: dict[str, str] = {
    "tag": "标签",
    "author": "作者",
    "actor": "角色",
    "work": "作品",
    "parody": "原作",
    "group": "社团",
    "language": "语言",
}

# 前缀别名，全部收敛到同一个字段；键使用小写（中文不受大小写影响）
FIELD_ALIASES: dict[str, str] = {
    "tag": "tag",
    "标签": "tag",
    "標籤": "tag",
    "author": "author",
    "artist": "author",
    "作者": "author",
    "画师": "author",
    "畫師": "author",
    "actor": "actor",
    "character": "actor",
    "角色": "actor",
    "work": "work",
    "title": "work",
    "作品": "work",
    "标题": "work",
    "標題": "work",
    "parody": "parody",
    "原作": "parody",
    "系列": "parody",
    "group": "group",
    "circle": "group",
    "社团": "group",
    "社團": "group",
    "language": "language",
    "lang": "language",
    "语言": "language",
    "語言": "language",
}

_PREFIX = re.compile(r"^(?P<field>[A-Za-z\u4e00-\u9fff]+)\s*[:：]\s*(?P<value>.*)$", re.S)
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’"}
_NEGATIVE_MARKS = ("-", "－")


def parse_search_query(raw: str) -> SearchQuery:
    """解析用户输入；语法错误抛 ``SearchQueryError``，message 可直接发给用户。"""
    text = (raw or "").strip()
    includes: list[SearchFilter] = []
    excludes: list[SearchFilter] = []
    keywords: list[str] = []

    for token in _tokenize(text):
        field, value, negative = _parse_filter_token(token)
        if field is None:
            keywords.append(_strip_quotes(token))
            continue
        if not value:
            raise SearchQueryError(
                f"`{token}` 缺少筛选值；冒号后直接写值，含空格时用引号包起来"
            )
        search_filter = SearchFilter(field=field, value=value)
        if negative:
            excludes.append(search_filter)
        else:
            includes.append(search_filter)

    return SearchQuery(
        raw=text,
        keyword=" ".join(keywords),
        includes=tuple(includes),
        excludes=tuple(excludes),
    )


def plain_search_text(query: SearchQuery) -> str:
    """不支持筛选的渠道使用的关键词：把正向条件值当普通词，排除项忽略。"""
    parts = [item.value for item in query.includes]
    if query.keyword:
        parts.append(query.keyword)
    return " ".join(part.strip() for part in parts if part.strip())


def fallback_note(query: SearchQuery) -> str:
    """渠道只能用关键词搜索时给用户的说明；没有筛选条件时返回空串。

    不拼渠道名：结果渲染层会按渠道分组，拼在这里会重复成「nhentai：nhentai…」。
    """
    if not query.has_filters:
        return ""
    if query.includes and query.excludes:
        issue = "不支持字段筛选与排除条件"
    elif query.includes:
        issue = "不支持字段筛选"
    else:
        issue = "不支持排除条件"
    return f"{issue}，已按关键词搜索"


def field_label(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def _tokenize(text: str) -> list[str]:
    """按空白切词，但保留引号内的空格（引号本身留在 token 里，后面再剥）。"""
    tokens: list[str] = []
    buffer: list[str] = []
    closer = ""
    for char in text:
        if closer:
            buffer.append(char)
            if char == closer:
                closer = ""
            continue
        if char in _QUOTE_PAIRS:
            closer = _QUOTE_PAIRS[char]
            buffer.append(char)
            continue
        if char.isspace():
            if buffer:
                tokens.append("".join(buffer))
                buffer = []
            continue
        buffer.append(char)
    if buffer:
        tokens.append("".join(buffer))
    return tokens


def _parse_filter_token(token: str) -> tuple[str | None, str, bool]:
    """解析单个 token，返回（规范字段, 值, 是否排除）；不是已知前缀时字段为 None。"""
    negative = False
    body = token
    if body.startswith(_NEGATIVE_MARKS):
        negative = True
        body = body[1:]

    matched = _PREFIX.match(body)
    if matched is None:
        return None, "", False
    field = FIELD_ALIASES.get(matched.group("field").lower())
    if field is None:
        return None, "", False
    return field, _strip_quotes(matched.group("value").strip()), negative


def _strip_quotes(value: str) -> str:
    """剥掉值两端成对的引号；不成对或没有引号时原样返回。"""
    if len(value) >= 2 and value[0] in _QUOTE_PAIRS:
        if value[-1] == _QUOTE_PAIRS[value[0]]:
            return value[1:-1].strip()
    return value

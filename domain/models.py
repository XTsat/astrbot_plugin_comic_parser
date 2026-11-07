"""跨层传递的数据定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ComicMeta:
    """一本漫画的元信息，由渠道在下载前返回。"""

    channel: str
    comic_id: str
    title: str = ""
    author: str = ""
    page_count: int = 0  # 章节数，0 表示渠道未提供
    tags: list[str] = field(default_factory=list)

    @property
    def display_title(self) -> str:
        """展示用标题：渠道没给标题时退回 ID，避免消息里出现空标题。"""
        return self.title.strip() or f"{self.channel}-{self.comic_id}"


@dataclass(slots=True)
class SearchItem:
    """一条搜索结果：只够让用户认出作品并决定要不要下载，不承载完整元信息。

    ``comic_id`` 必须是该渠道 ``normalize_id()`` 能直接接受的规范形（E-Hentai 是
    ``gid-token`` 两段、哔咔是 24 位 ObjectId）。上层只负责展示，不拼 ID。
    """

    channel: str
    comic_id: str
    title: str = ""
    author: str = ""
    page_count: int = 0  # 作品页数，0 表示渠道搜索结果里没有
    tags: list[str] = field(default_factory=list)
    cover_url: str = ""

    @property
    def display_title(self) -> str:
        """展示用标题：渠道没给标题时退回 ID，避免消息里出现空标题。"""
        return self.title.strip() or f"{self.channel}-{self.comic_id}"


@dataclass(slots=True)
class SearchFilter:
    """一条结构化搜索条件：``field`` 是规范化字段名，``value`` 是用户填的值。"""

    field: str
    value: str


@dataclass(slots=True)
class SearchQuery:
    """一次搜索请求：保留原始输入，同时拆出正/负向条件与自由关键词。

    之所以要在命令层先解析成结构，而不是把 ``tag:xxx`` 原样交给每个渠道：
    各站上游的筛选语法完全不同（JM 靠 ``main_tag``，E-Hentai 靠命名空间，
    其余渠道只收一个关键词），解析一次再各自翻译才能保证行为一致。
    """

    raw: str
    keyword: str = ""
    includes: tuple[SearchFilter, ...] = ()
    excludes: tuple[SearchFilter, ...] = ()

    @property
    def empty(self) -> bool:
        """没有关键词也没有正向条件时视为空查询；只有排除项不构成一次搜索。"""
        return not self.keyword and not self.includes

    @property
    def has_filters(self) -> bool:
        return bool(self.includes or self.excludes)

    @property
    def cache_key(self) -> str:
        """缓存键：把条件规范化，避免 ``Tag:X`` 与 ``tag:x`` 命中不同缓存。"""

        def encode(items: tuple[SearchFilter, ...]) -> str:
            return "\x1f".join(f"{item.field}\x1e{item.value}" for item in items)

        return f"{self.keyword}\x1d{encode(self.includes)}\x1d{encode(self.excludes)}"


@dataclass(slots=True)
class SearchPage:
    """某渠道一次搜索的结果页；搜索没有分页，``items`` 已是截断后的展示集。"""

    channel: str
    keyword: str
    items: list[SearchItem] = field(default_factory=list)
    total: int | None = None  # 上游给出的总结果数；没有时为 None
    has_more: bool = False  # 上游这一页之后还有结果（本地翻页用）
    note: str = ""  # 该渠道对筛选条件的处理说明（降级/不支持时给用户看）

    @property
    def empty(self) -> bool:
        return not self.items


@dataclass(slots=True)
class DownloadedComic:
    """一次下载的落盘结果：图片按原始目录结构保存在 source_dir 下。"""

    meta: ComicMeta
    source_dir: Path
    image_count: int
    failed_images: int = 0


@dataclass(slots=True)
class PackResult:
    """打包结果。fmt 为 none 时 output_path 是原始目录，调用方不应发送。"""

    success: bool
    output_path: Path | None
    fmt: str
    encrypted: bool
    error: str | None = None
    note: str = ""  # 非致命提示（个别页无法解码已跳过等），写进结果摘要给用户看

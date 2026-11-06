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
class SearchPage:
    """某渠道一次搜索的结果页；搜索没有分页，``items`` 已是截断后的展示集。"""

    channel: str
    keyword: str
    items: list[SearchItem] = field(default_factory=list)
    total: int | None = None  # 上游给出的总结果数；没有时为 None
    has_more: bool = False  # 上游这一页之后还有结果（本地翻页用）

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

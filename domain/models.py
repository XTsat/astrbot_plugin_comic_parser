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

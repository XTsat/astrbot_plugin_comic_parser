"""封面交付：从下载好的目录里挑出封面，落成可发送的临时文件。

渠道只负责把图片下到目录里，**不区分哪张是封面**——nhentai、JM、e-hentai 这些站点的
第 1 张图就是封面，因此这里按路径顺序取第一张像样的图，不需要每个渠道各写一套。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from PIL import Image
from astrbot.api import logger

from ..domain.constants import COVER_MIN_SIDE, IMAGE_SUFFIXES, PLUGIN_NAME
from ..domain.models import ComicMeta

# 封面临时文件只留这么久：消息要在发送前一直存在，不能发完就删（协议端是异步取图的）
COVER_FILE_TTL = 3600.0


class CoverService:
    """把「下载结果」变成「聊天里能看到的一张封面」。"""

    def __init__(
        self,
        temp_root: Path,
        *,
        send_caption: bool = True,
    ) -> None:
        self.temp_root = temp_root
        self.send_caption = send_caption

    # ---- 取图 ----

    @staticmethod
    def _iter_images(source_dir: Path) -> list[Path]:
        """按路径顺序列出目录里所有图片（渠道可能按章节分子目录）。"""
        if not source_dir.is_dir():
            return []

        images: list[Path] = []
        for path in source_dir.rglob("*"):
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                if path.is_file() and path.stat().st_size > 0:
                    images.append(path)
            except OSError:
                continue
        return sorted(images)

    def pick_cover(self, source_dir: Path) -> Path | None:
        """挑封面：优先第一张「够大」的图，都是小图时退回第一张。

        太小的图（站徽、占位、章节目录缩略图）当封面毫无意义，所以要按边长过一遍。
        """
        images = self._iter_images(source_dir)
        if not images:
            return None

        for path in images:
            try:
                with Image.open(path) as image:
                    width, height = image.size
            except Exception:
                continue
            if min(width, height) >= COVER_MIN_SIDE:
                return path
        return images[0]

    # ---- 生成 ----

    def prepare(self, source_dir: Path, *, channel: str = "", comic_id: str = "") -> Path | None:
        """把封面复制到临时目录并返回待发送路径；没有封面图时返回 None。"""
        cover = self.pick_cover(source_dir)
        if cover is None:
            logger.debug(f"[{PLUGIN_NAME}] 下载目录里没有可用图片，跳过封面：{source_dir}")
            return None

        self._cleanup_old_files()
        return self._stage_cover(cover, channel, comic_id)

    def _stage_cover(self, cover: Path, channel: str, comic_id: str) -> Path:
        """把封面复制到临时目录：原图要留在下载目录里进压缩包，不能被就地改写。"""
        covers_dir = self.temp_root / "covers"
        covers_dir.mkdir(parents=True, exist_ok=True)
        suffix = cover.suffix.lower() or ".jpg"
        target = covers_dir / f"{channel}-{comic_id}-cover{suffix}"
        shutil.copyfile(cover, target)
        return target

    def _cleanup_old_files(self) -> None:
        """清掉过期的临时代封面：发送是异步的，所以只能按时间清，不能发完就删。"""
        covers_dir = self.temp_root / "covers"
        if not covers_dir.is_dir():
            return

        now = time.time()
        for path in covers_dir.iterdir():
            try:
                if path.is_file() and now - path.stat().st_mtime > COVER_FILE_TTL:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    # ---- 文案 ----

    def caption(self, meta: ComicMeta) -> str:
        """封面消息的说明文字：标题 + 作者 + 页数（关闭「附带说明」时返回空串）。"""
        if not self.send_caption:
            return ""

        lines = [f"🖼️ {meta.display_title}"]
        details: list[str] = []
        if meta.author:
            details.append(f"作者：{meta.author}")
        if meta.page_count:
            details.append(f"页数：{meta.page_count}")
        if details:
            lines.append(" · ".join(details))
        return "\n".join(lines)

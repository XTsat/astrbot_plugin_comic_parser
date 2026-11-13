"""搜图（以图搜图）：单独一套逻辑，与「按 ID 下载」的渠道体系互不依赖。

放在 `core/` 而不是 `core/channels/`：渠道只负责「给定 ID → 取元信息 → 下载图片」，
而搜图是「给定图片 → 找到作品 ID」，方向相反。命中之后再用对应渠道的命令去下载，
两个能力在这里只是恰好首尾相接，不共享状态。
"""

from __future__ import annotations

from .candidates import collect_image_refs, has_image_ref, resolve_image_bytes
from .client import ImageSearchClient, ImageSearchError
from .service import ImageSearchHit, normalize_hits, render_report, select_hits

__all__ = [
    "ImageSearchClient",
    "ImageSearchError",
    "ImageSearchHit",
    "collect_image_refs",
    "has_image_ref",
    "normalize_hits",
    "render_report",
    "resolve_image_bytes",
    "select_hits",
]

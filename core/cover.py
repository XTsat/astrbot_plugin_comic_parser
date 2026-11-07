"""封面交付：从下载好的目录里挑出封面，按配置打分/打码，落成可发送的临时文件。

渠道只负责把图片下到目录里，**不区分哪张是封面**——nhentai、JM、e-hentai 这些站点的
第 1 张图就是封面，因此这里按路径顺序取第一张像样的图，不需要每个渠道各写一套。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from astrbot.api import logger

from ..domain.constants import (
    COVER_MIN_SIDE,
    IMAGE_SUFFIXES,
    NSFW_MODEL_VARIANT_LABELS,
    PLUGIN_NAME,
)
from ..domain.models import ComicMeta
from .nsfw import Detection, Detector, NsfwScore, censor_cover

# 封面临时文件只留这么久：消息要在发送前一直存在，不能发完就删（协议端是异步取图的）
COVER_FILE_TTL = 3600.0


@dataclass(slots=True)
class CoverResult:
    """一张准备好的封面：路径 + 评分 + 打码结论。"""

    path: Path
    score: NsfwScore
    censored: bool
    full_censored: bool


class CoverService:
    """把「下载结果」变成「聊天里能看到的一张封面」。"""

    def __init__(
        self,
        detector: Detector,
        temp_root: Path,
        *,
        send_caption: bool = True,
        score_enabled: bool = True,
        censor: bool = False,
        censor_style: str = "mosaic",
        censor_block: int = 14,
        censor_padding: int = 12,
        full_censor_area: float = 0.2,
    ) -> None:
        self.detector = detector
        self.temp_root = temp_root
        self.send_caption = send_caption
        self.score_enabled = score_enabled
        self.censor = censor
        self.censor_style = censor_style
        self.censor_block = censor_block
        self.censor_padding = censor_padding
        self.full_censor_area = full_censor_area

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

    async def prepare(
        self,
        meta: ComicMeta,
        source_dir: Path,
        *,
        channel: str = "",
    ) -> CoverResult | None:
        """准备好待发送的封面；没有封面图时返回 None。

        评分与打码都是可选能力：模型缺失、推理失败都只影响这两项，封面照旧发出。
        """
        cover = self.pick_cover(source_dir)
        if cover is None:
            logger.debug(f"[{PLUGIN_NAME}] 下载目录里没有可用图片，跳过封面：{source_dir}")
            return None

        await asyncio.to_thread(self._cleanup_old_files)
        target = await asyncio.to_thread(self._stage_cover, cover, channel or meta.channel, meta.comic_id)

        # 关掉评分又不开打码时，连推理都不必做——省下每本几百毫秒
        score = NsfwScore.empty()
        detections: list[Detection] = []
        if self.detector.model_path is not None and (self.censor or self.score_enabled):
            analysis = await asyncio.to_thread(self.detector.analyze, target)
            detections = list(analysis.detections)
            if self.score_enabled:
                score = analysis

        censored = False
        full_censored = False
        if self.censor:
            boxes = [item.box for item in detections]
            if not boxes and self.detector.model_path is None:
                # 打码默认打开，但没权重时整张糊掉比不打码更糟：跳过并提示配置模型
                logger.warning(
                    f"[{PLUGIN_NAME}] 没有可用的 NSFW 权重，本次跳过封面打码；"
                    "在「NSFW 模型」里放好 .pt 或开启自动下载即可生效"
                )
            if boxes:
                censored, coverage = await asyncio.to_thread(
                    censor_cover,
                    target,
                    boxes,
                    style=self.censor_style,
                    block=self.censor_block,
                    padding=self.censor_padding,
                    full_area=self.full_censor_area,
                )
                full_censored = bool(
                    censored
                    and self.full_censor_area > 0
                    and coverage >= self.full_censor_area
                )

        return CoverResult(
            path=target,
            score=score,
            censored=censored,
            full_censored=full_censored,
        )

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

    def caption(self, meta: ComicMeta, result: CoverResult) -> str:
        """封面消息的说明文字：标题 + 评分结论（关闭「附带说明」时返回空串）。"""
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

        if result.score.value > 0:
            labels = "、".join(result.score.labels) or "露骨部位"
            lines.append(f"封面露骨度：{result.score.value:.1f}%（{labels}）")
        if result.censored:
            lines.append("🔒 封面已打码" + ("（整张）" if result.full_censored else ""))
        return "\n".join(lines)

    @staticmethod
    def describe_model(detector: Detector, variant: str) -> str:
        """给帮助文案用的一句话：当前用的是哪份权重、还是根本没配。"""
        model_path = detector.model_path
        if model_path is not None:
            return model_path.name
        label = NSFW_MODEL_VARIANT_LABELS.get(variant, variant)
        return f"未配置（可在配置里开启自动下载 {label}）"

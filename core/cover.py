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
    COVER_RETRY_CENSOR_BLOCK,
    IMAGE_SUFFIXES,
    NSFW_MODEL_PRESETS,
    PLUGIN_NAME,
)
from ..domain.errors import CoverCensorError
from ..domain.models import ComicMeta
from .nsfw import Detection, Detector, NsfwScore, censor_cover, censor_full_image

# 封面临时文件只留这么久：消息要在发送前一直存在，不能发完就删（协议端是异步取图的）
COVER_FILE_TTL = 3600.0


@dataclass(slots=True)
class CoverResult:
    """一张准备好的封面：路径 + 评分 + 打码结论。

    ``detected`` 区分「跑过检测但没检出」和「根本没跑检测（没开打码）」——前者要如实
    写出阈值，好让用户知道该往哪调；``coverage`` 是检出框并集占整图的比例，用户靠它判断
    「为什么这次只糊了局部 / 为什么整张都糊了」；``reinforced`` 标记这是发送被拦后整张
    重糊的降级版，摘要里要说清楚，否则用户看到的糊图来源不明。
    """

    path: Path
    score: NsfwScore
    censored: bool
    full_censored: bool
    detected: bool = False
    coverage: float = 0.0
    reinforced: bool = False


class CoverService:
    """把「下载结果」变成「聊天里能看到的一张封面」。"""

    def __init__(
        self,
        detector: Detector,
        temp_root: Path,
        *,
        send_caption: bool = True,
        censor: bool = False,
        censor_style: str = "mosaic",
        censor_block: int = 14,
        censor_padding: int = 12,
        full_censor_area: float = 0.2,
    ) -> None:
        self.detector = detector
        self.temp_root = temp_root
        self.send_caption = send_caption
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

        **打码是硬要求**：开了打码就必须真的糊过图才发，否则抛
        :class:`CoverCensorError` 打断这次封面发送——用户要的是「打码后的封面」，
        发一张没糊的图等于骗他。评分仍是可选能力，模型缺失只影响是否显示露骨度。

        Raises:
            CoverCensorError: 已开启打码，但模型缺失或检测没能执行。
        """
        cover = self.pick_cover(source_dir)
        if cover is None:
            logger.debug(f"[{PLUGIN_NAME}] 下载目录里没有可用图片，跳过封面：{source_dir}")
            return None

        # 先判模型是否就位，避免为一个注定发不出去的封面白复制文件
        if self.censor and self.detector.model_path is None:
            raise CoverCensorError(
                "已开启「给封面打码」，但检测还跑不起来，本次不发送封面"
                "（缺 onnxruntime 时插件会自行安装、装好后重载插件；缺权重可把对应 .onnx 放进插件目录的 models/，"
                "或在「封面与模型 → 模型与检测」里开启「缺失时自动下载权重」）"
            )

        await asyncio.to_thread(self._cleanup_old_files)
        target = await asyncio.to_thread(
            self._stage_cover, cover, channel or meta.channel, meta.comic_id
        )

        # 露骨度是用同一趟推理顺带算出来的，所以它跟着打码走：不开打码就不做推理，
        # 封面原样发送、也不写露骨度（每本省下几百毫秒）
        score = NsfwScore.empty()
        detections: list[Detection] = []
        if self.censor and self.detector.model_path is not None:
            analysis = await asyncio.to_thread(self.detector.analyze, target)
            if analysis.failed:
                # 检测没跑成 ≠ 图是干净的，不能当作「无需打码」放行
                target.unlink(missing_ok=True)
                raise CoverCensorError(
                    "封面 NSFW 检测执行失败，本次不发送封面（详情见日志）"
                )
            detections = list(analysis.detections)
            score = analysis

        censored = False
        full_censored = False
        coverage = 0.0
        if self.censor and detections:
            censored, coverage = await asyncio.to_thread(
                censor_cover,
                target,
                [item.box for item in detections],
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
            detected=self.censor and self.detector.model_path is not None,
            coverage=coverage,
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

    # ---- 补救 ----

    async def reinforce(self, result: CoverResult) -> CoverResult:
        """把已经打码的封面再整张糊一遍，用于「发出去被平台拦了」的重发。

        局部打码在平台审核眼里常常还不够（见 :func:`censor_full_image` 的说明），
        所以重发不再看检出框，直接整张糊，块大小取配置值与补救下限里更大的那个。

        Args:
            result: 上一次准备好的封面。

        Returns:
            CoverResult: 整张打码后的封面（分数沿用上次推理结果）。
        """
        block = max(int(self.censor_block), COVER_RETRY_CENSOR_BLOCK)
        await asyncio.to_thread(
            censor_full_image, result.path, style=self.censor_style, block=block
        )
        return CoverResult(
            path=result.path,
            score=result.score,
            censored=True,
            full_censored=True,
            detected=result.detected,
            coverage=result.coverage,
            reinforced=True,
        )

    # ---- 文案 ----

    def caption(self, meta: ComicMeta, result: CoverResult) -> str:
        """封面说明：只写这一次的检测结论（开关关着时返回空串）。

        标题、作者、页数已经在下载摘要里了，这里不重复；这里要说的是调参用得上的数据：
        露骨度、检出几处、并集覆盖多少、判定用的阈值。一处都没检出时也要明确写出阈值，
        否则用户分不清「这张图干净」和「阈值太高没检出来」。

        Args:
            meta: 作品元信息（标题/作者由下载摘要负责，这里不再使用）。
            result: 这次的封面准备结果。
        """
        if not self.send_caption:
            return ""

        lines: list[str] = []
        threshold = getattr(self.detector, "threshold", None)
        threshold_text = (
            f"{float(threshold):g}" if isinstance(threshold, (int, float)) else "默认"
        )
        if result.detected and result.score.value > 0:
            labels = "、".join(result.score.labels) or "露骨部位"
            lines.append(
                f"🖼️ 封面露骨度 {result.score.value:.1f}%（{labels}）"
                f" · 检出 {len(result.score.detections)} 处"
                f" · 覆盖 {result.coverage:.0%}"
                f" · 阈值 {threshold_text}"
            )
        elif result.detected:
            lines.append(f"🖼️ 封面未检出露骨部位（阈值 {threshold_text}，覆盖 0%）")
        if result.reinforced:
            lines.append("🔒 平台把封面当露骨图拦下，已整张重糊后重发")
        elif result.censored:
            lines.append("🔒 封面已打码" + ("（整张）" if result.full_censored else ""))
        return "\n".join(lines)

    @staticmethod
    def describe_model(detector: Detector, preset: str) -> str:
        """给帮助文案用的一句话：当前用的是哪份权重、还是根本没配。"""
        model_path = detector.model_path
        if model_path is not None:
            return model_path.name
        label = NSFW_MODEL_PRESETS.get(preset, ("", preset))[1]
        return f"未配置（可在配置里开启自动下载：{label}）"

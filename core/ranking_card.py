"""榜单卡片：把一页榜单画成一张可以直接发到聊天的图。

排版只用 Pillow（项目里长图拼接、封面打码已经在用它，不额外引入依赖）：标题栏 + 网格
封面 + 名次角标 + 标题/作者 + 脚注。几条硬约束都写在实现里：

- **先糊后拼**：每张封面单独跑检测与打码，再贴进网格。合成后再糊会连名次、标题一起糊掉。
- **打码开关跟封面共用一套配置**（``cover.censor.*``）：榜单里的封面和「下载完成后发的那
  张封面」风险一样，用户开一次就该两处都生效。
- **打不开检测也不裸发**：单封面流程是「检测跑不起来就不发这张图」，但一页多张图整页
  放弃代价太大，这里退化成**整图打码**——仍然是「绝不把没糊的图发出去」，只是糊得更多。
- **渲染必须离开事件循环**：贴图、缩放、模糊都是纯 CPU 活，统一走 ``asyncio.to_thread``。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from astrbot.api import logger

from ..domain import RankingItem, RankingPage
from ..domain.constants import (
    PLUGIN_NAME,
    RANKING_CARD_AUTHOR_CHARS,
    RANKING_CARD_GAP,
    RANKING_CARD_PADDING,
    RANKING_CARD_TITLE_LINES,
    RANKING_CARD_WIDTH,
    RANKING_COVER_ASPECT,
    RANKING_COVER_CONCURRENCY,
    RANKING_COVER_MAX_BYTES,
    RANKING_COVER_TIMEOUT,
    RANKING_MAX_CARD_HEIGHT,
)
from ..utils.font import TextFont, load_font
from ..utils.title import has_ai_mark
from .channel import BaseChannel
from .nsfw import Detector, censor_cover, censor_full_image

# 配色：白底深字在手机上看最稳；前三名用金银铜角标，其余用中性灰
_BG = (250, 250, 252)
_CARD_BG = (255, 255, 255)
_TEXT = (28, 30, 36)
_MUTED = (110, 116, 128)
_LINE = (228, 230, 236)
_RANK_COLORS = {
    1: (214, 154, 34),
    2: (150, 154, 162),
    3: (176, 122, 78),
}
_RANK_DEFAULT = (94, 100, 112)
# AI 生成作品的角标：紫底白字，和名次角标并排但明显不是名次
_AI_BADGE_COLOR = (124, 82, 214)
# 封面缺失时的占位色（图床挂掉、作品下架都会走到）
_PLACEHOLDER = (236, 238, 243)
# 榜单卡片与中间封面在临时目录里的保留时长：发送是异步的，不能发完就删
CARD_FILE_TTL = 3600.0


@dataclass(slots=True)
class CardResult:
    """一张榜单卡片的成品与打码结论。"""

    path: Path
    censored: bool
    full_censored: bool
    drawn: int  # 实际画进卡片的封面数
    missing: int  # 没取到的封面数
    note: str = ""


class RankingCardRenderer:
    """把一页榜单渲染成一张图；打码与封面下载都在这里收口。"""

    def __init__(
        self,
        temp_root: Path,
        detector: Detector | None = None,
        *,
        columns: int = 3,
        page_size: int = 6,
        show_author: bool = True,
        censor: bool = False,
        censor_style: str = "mosaic",
        censor_block: int = 14,
        censor_padding: int = 12,
        full_censor_area: float = 0.3,
    ) -> None:
        self.temp_root = temp_root
        self.detector = detector
        self.columns = max(1, int(columns))
        self.page_size = max(1, int(page_size))
        self.show_author = show_author
        self.censor = censor
        self.censor_style = censor_style
        self.censor_block = censor_block
        self.censor_padding = censor_padding
        self.full_censor_area = full_censor_area

    # ---- 对外 ----
    async def render(
        self,
        channel: BaseChannel,
        page: RankingPage,
        *,
        board_label: str,
        page_number: int = 1,
    ) -> CardResult | None:
        """渲染一页榜单；没有可画的条目时返回 None。"""
        items = page.items[: self.page_size]
        if not items:
            return None

        covers_dir = self.temp_root / "ranking"
        covers_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._cleanup_old)
        staged = await self._stage_covers(channel, items, covers_dir)
        censor_state = await asyncio.to_thread(self._censor_covers, staged)

        out_path = covers_dir / f"{channel.name}-{page.board}-p{page_number}.jpg"
        drawn, missing = await asyncio.to_thread(
            self._compose,
            items,
            staged,
            out_path,
            channel.display_name,
            board_label,
            page_number,
        )

        await asyncio.to_thread(self._drop_staged, staged, out_path)

        full_censored = False
        note = ""
        if self.censor and censor_state.needs_full:
            # 检测跑不起来（没模型/推理失败）时整图糊：宁可难看，也不能把没糊的图发出去
            await asyncio.to_thread(
                censor_full_image,
                out_path,
                style=self.censor_style,
                block=self.censor_block,
            )
            full_censored = True
            note = "打码检测不可用，已整张打码"
            logger.warning(
                f"[{PLUGIN_NAME}] {channel.display_name} 榜单检测不可用，已整张打码："
                f"{censor_state.reason}"
            )
        return CardResult(
            path=out_path,
            censored=self.censor and (censor_state.hit or full_censored),
            full_censored=full_censored,
            drawn=drawn,
            missing=missing,
            note=note,
        )

    async def reinforce(self, result: CardResult) -> CardResult:
        """发送被平台拦下后整张重糊一次再发（与单封面同一套补救思路）。"""
        await asyncio.to_thread(
            censor_full_image,
            result.path,
            style=self.censor_style,
            block=max(self.censor_block, 28),
        )
        return CardResult(
            path=result.path,
            censored=True,
            full_censored=True,
            drawn=result.drawn,
            missing=result.missing,
            note="平台拦下了这张图，已整张重糊后重发",
        )

    @staticmethod
    def _drop_staged(staged: list[Path | None], out_path: Path) -> None:
        """删掉已贴进卡片的中间封面文件：它们的唯一用途就是这次合成。"""
        for path in staged:
            if path is None or path == out_path:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

    def _cleanup_old(self) -> None:
        """清掉过期的榜单临时图：卡片要等消息发出去才能删，所以只能按时间清。"""
        covers_dir = self.temp_root / "ranking"
        if not covers_dir.is_dir():
            return
        now = time.time()
        for path in covers_dir.iterdir():
            try:
                if path.is_file() and now - path.stat().st_mtime > CARD_FILE_TTL:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    # ---- 封面 ----
    async def _stage_covers(
        self, channel: BaseChannel, items: list[RankingItem], covers_dir: Path
    ) -> list[Path | None]:
        """并发取封面并落到临时文件；单张失败只记 None，不影响整张卡片。"""
        semaphore = asyncio.Semaphore(RANKING_COVER_CONCURRENCY)

        async def fetch(index: int, item: RankingItem) -> Path | None:
            async with semaphore:
                try:
                    payload = await asyncio.wait_for(
                        channel.fetch_cover(item.comic_id),
                        timeout=RANKING_COVER_TIMEOUT,
                    )
                except Exception as exc:  # noqa: BLE001 - 单张取不到就按缺图处理，不拖垮整张卡片
                    logger.info(
                        f"[{PLUGIN_NAME}] 榜单封面取不到（{channel.name} {item.comic_id}）：{exc}"
                    )
                    return None
                if not payload or len(payload) > RANKING_COVER_MAX_BYTES:
                    return None
                target = covers_dir / f"cover-{index:02d}.img"
                try:
                    await asyncio.to_thread(target.write_bytes, payload)
                except OSError as exc:
                    logger.warning(f"[{PLUGIN_NAME}] 榜单封面写入失败：{exc}")
                    return None
                return target

        results = await asyncio.gather(
            *(fetch(index, item) for index, item in enumerate(items))
        )
        return list(results)

    def _censor_covers(self, staged: list[Path | None]) -> "_CensorState":
        """逐张检测并打码（检测器自身串行，所以一并放在一次线程调用里）。"""
        state = _CensorState()
        if not self.censor:
            return state
        detector = self.detector
        if detector is None or detector.model_path is None:
            # 开关打开但模型没到位：整图兜底，别把没糊的图发出去
            state.needs_full = True
            state.reason = "没有可用的检测模型"
            return state

        for path in staged:
            if path is None:
                continue
            try:
                analysis = detector.analyze(path)
            except Exception as exc:  # noqa: BLE001 - 检测后端异常统一降级
                state.needs_full = True
                state.reason = f"检测执行失败：{exc}"
                continue
            if analysis.failed:
                state.needs_full = True
                state.reason = "检测执行失败"
                continue
            if not analysis.detections:
                continue
            boxes = [item.box for item in analysis.detections]
            try:
                censored, _coverage = censor_cover(
                    path,
                    boxes,
                    style=self.censor_style,
                    block=self.censor_block,
                    padding=self.censor_padding,
                    full_area=self.full_censor_area,
                )
            except Exception as exc:  # noqa: BLE001 - 糊图失败时整图兜底
                state.needs_full = True
                state.reason = f"打码失败：{exc}"
                continue
            if censored:
                state.hit = True
        return state

    # ---- 排版 ----
    def _compose(
        self,
        items: list[RankingItem],
        staged: list[Path | None],
        out_path: Path,
        channel_name: str,
        board_label: str,
        page_number: int,
    ) -> tuple[int, int]:
        """把封面与文字画成一张图，返回（画进去的张数, 缺图的张数）。"""
        width = RANKING_CARD_WIDTH
        pad = RANKING_CARD_PADDING
        gap = RANKING_CARD_GAP
        columns = min(self.columns, max(1, len(items)))
        cell_w = max(1, (width - pad * 2 - gap * (columns - 1)) // columns)
        cover_h = int(cell_w * RANKING_COVER_ASPECT)
        title_font = load_font(max(16, cell_w // 15))
        meta_font = load_font(max(14, cell_w // 19))
        rank_font = load_font(max(18, cell_w // 11))
        head_font = load_font(46)
        sub_font = load_font(26)
        foot_font = load_font(24)

        rows = (len(items) + columns - 1) // columns
        line_gap = 6
        text_h = title_font.size * RANKING_CARD_TITLE_LINES + line_gap + meta_font.size
        cell_h = cover_h + 12 + text_h
        header_h = 132
        footer_h = 76
        height = (
            pad + header_h + rows * cell_h + max(0, rows - 1) * gap + footer_h + pad
        )
        height = min(height, RANKING_MAX_CARD_HEIGHT)

        canvas = Image.new("RGB", (width, max(240, height)), _BG)
        draw = ImageDraw.Draw(canvas)

        # 标题栏
        head_font.draw(draw, (pad, pad), f"{board_label} · {channel_name}", _TEXT)
        sub_font.draw(
            draw,
            (pad, pad + head_font.size + 14),
            f"第 {page_number} 页 · 共 {len(items)} 部作品",
            _MUTED,
        )
        draw.line(
            ((pad, pad + header_h - 12), (width - pad, pad + header_h - 12)),
            fill=_LINE,
            width=2,
        )

        drawn = 0
        missing = 0
        top = pad + header_h
        for index, item in enumerate(items):
            row, column = divmod(index, columns)
            left = pad + column * (cell_w + gap)
            cell_top = top + row * (cell_h + gap)
            staged_cover = staged[index] if index < len(staged) else None
            self._draw_cell(
                canvas,
                draw,
                item=item,
                index=index + 1,
                staged=staged_cover,
                left=left,
                top=cell_top,
                cell_w=cell_w,
                cover_h=cover_h,
                title_font=title_font,
                meta_font=meta_font,
                rank_font=rank_font,
                line_gap=line_gap,
            )
            if staged_cover is not None:
                drawn += 1
            else:
                missing += 1

        # 脚注
        footer_y = height - pad - foot_font.size * 2 - 6
        hints = (
            "回序号下载（如 3），也可以直接贴作品 ID",
            "看后面的名次：/comic 下一页",
        )
        for offset, text in enumerate(hints):
            foot_font.draw(
                draw,
                (pad, footer_y + offset * (foot_font.size + 4)),
                text,
                _MUTED,
            )

        canvas.save(out_path, "JPEG", quality=92)
        canvas.close()
        return drawn, missing

    def _draw_cell(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        *,
        item: RankingItem,
        index: int,
        staged: Path | None,
        left: int,
        top: int,
        cell_w: int,
        cover_h: int,
        title_font: TextFont,
        meta_font: TextFont,
        rank_font: TextFont,
        line_gap: int,
    ) -> None:
        box = (left, top, left + cell_w, top + cover_h)
        image = self._load_cover(staged, cell_w, cover_h)
        if image is None:
            draw.rectangle(box, fill=_PLACEHOLDER)
        else:
            canvas.paste(image, (left, top))
            image.close()

        # 名次角标：前三是金银铜，其余中性灰
        badge = 46
        color = _RANK_COLORS.get(index, _RANK_DEFAULT)
        draw.rounded_rectangle(
            (left + 10, top + 10, left + 10 + badge, top + 10 + badge),
            radius=12,
            fill=color,
        )
        rank_text = str(index)
        rank_w = rank_font.measure(rank_text)
        rank_font.draw(
            draw,
            (
                left + 10 + (badge - rank_w) / 2,
                top + 10 + badge / 2 - rank_font.size * 0.62,
            ),
            rank_text,
            (255, 255, 255),
        )

        # 标题（最多两行）与作者/页数；标题原样画，AI 标记另外做成封面角标
        text_top = top + cover_h + 12
        raw_title = item.title or item.display_title
        title_lines = title_font.wrap(raw_title, cell_w, RANKING_CARD_TITLE_LINES)
        for line_index, line in enumerate(title_lines):
            title_font.draw(
                draw,
                (left, text_top + line_index * title_font.size),
                line,
                _TEXT,
            )
        if has_ai_mark(raw_title):
            label = "AI"
            pill_w = meta_font.measure(label) + 20
            pill_h = meta_font.size + 12
            pill_left = left + 10 + badge + 8
            draw.rounded_rectangle(
                (pill_left, top + 10, pill_left + pill_w, top + 10 + pill_h),
                radius=pill_h // 2,
                fill=_AI_BADGE_COLOR,
            )
            meta_font.draw(
                draw,
                (
                    pill_left + 10,
                    top + 10 + pill_h / 2 - meta_font.size * 0.62,
                ),
                label,
                (255, 255, 255),
            )
        meta_parts: list[str] = []
        if self.show_author and item.author.strip():
            author = item.author.strip()
            if len(author) > RANKING_CARD_AUTHOR_CHARS:
                author = author[: RANKING_CARD_AUTHOR_CHARS - 1] + "…"
            meta_parts.append(author)
        if item.page_count > 0:
            meta_parts.append(f"{item.page_count}P")
        meta = " · ".join(meta_parts)
        if meta:
            meta_font.draw(
                draw,
                (
                    left,
                    text_top + title_font.size * RANKING_CARD_TITLE_LINES + line_gap,
                ),
                meta,
                _MUTED,
            )

    @staticmethod
    def _load_cover(
        staged: Path | None, cell_w: int, cover_h: int
    ) -> Image.Image | None:
        """把封面裁成格子大小（按短边裁齐，不拉变形）；读不出来返回 None。"""
        if staged is None:
            return None
        try:
            with Image.open(staged) as raw:
                source = raw.convert("RGB")
        except (OSError, ValueError) as exc:
            logger.info(f"[{PLUGIN_NAME}] 榜单封面解码失败，按缺图处理：{exc}")
            return None
        scale = max(cell_w / source.width, cover_h / source.height)
        resized = source.resize(
            (max(1, int(source.width * scale)), max(1, int(source.height * scale))),
            Image.LANCZOS,
        )
        source.close()
        left = max(0, (resized.width - cell_w) // 2)
        top = max(0, (resized.height - cover_h) // 2)
        cropped = resized.crop((left, top, left + cell_w, top + cover_h))
        resized.close()
        return cropped


@dataclass(slots=True)
class _CensorState:
    """一轮封面打码的结论：有没有真的糊到、要不要整图兜底。"""

    hit: bool = False
    needs_full: bool = False
    reason: str = ""

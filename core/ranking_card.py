"""榜单卡片：把一页榜单画成一张可以直接发到聊天的图。

排版只用 Pillow（项目里长图拼接、封面打码已经在用它，不额外引入依赖）：标题栏 + 网格
封面 + 名次角标 + 标题/作者 + 脚注。几条硬约束都写在实现里：

- **先糊后拼**：每张封面单独跑检测与打码，再贴进网格。合成后再糊会连名次、标题一起糊掉。
- **打码开关跟封面共用一套配置**（``cover.censor.*``）：榜单里的封面和「下载完成后发的那
  张封面」风险一样，用户开一次就该两处都生效。
- **打不开检测也不裸发**：单封面流程是「检测跑不起来就不发这张图」，但一页多张图整页
  放弃代价太大，这里退化成**把这张封面整张糊掉**再拼——仍然是「绝不把没糊的图发出去」，
  而卡片上的名次、标题、脚注照常可读（整张卡片一起糊等于把一页榜单糊成一团色块）。
- **渲染必须离开事件循环**：贴图、缩放、模糊都是纯 CPU 活，统一走 ``asyncio.to_thread``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from astrbot.api import logger

from ..domain import RankingItem, RankingPage
from ..domain.constants import (
    IMAGE_SUFFIXES,
    PLUGIN_NAME,
    RANKING_BOARD_LABELS,
    RANKING_CARD_AUTHOR_CHARS,
    RANKING_CARD_GAP,
    RANKING_CARD_PADDING,
    RANKING_CARD_TITLE_LINES,
    RANKING_CARD_WIDTH,
    RANKING_COVER_ASPECT,
    RANKING_COVER_CONCURRENCY,
    RANKING_COVER_MAX_BYTES,
    RANKING_COVER_TIMEOUT,
    RANKING_IMAGE_CACHE_MAX,
    RANKING_MAX_CARD_HEIGHT,
    RANKING_MAX_COMBINED_HEIGHT,
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
# 作品 ID 用偏冷的深灰蓝：一眼能和作者/页数那行分开，又不抢标题
_ID_COLOR = (86, 96, 118)
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


def _sniff_suffix(payload: bytes) -> str:
    """按图片头猜后缀，认不出就按 PNG 存。

    打码是就地写回原文件，而 Pillow 只按**文件名后缀**决定保存格式：中间封面若统一叫
    ``.img``，每张都会在保存时抛 unknown file extension，打码一律失败。
    """
    if payload.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif payload.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif payload[:6] in {b"GIF87a", b"GIF89a"}:
        suffix = ".gif"
    elif payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        suffix = ".webp"
    elif payload[:2] == b"BM":
        suffix = ".bmp"
    elif payload[4:12] in {b"ftypavif", b"ftypavis"}:
        suffix = ".avif"
    else:
        suffix = ".png"
    return suffix if suffix in IMAGE_SUFFIXES else ".png"


@dataclass(slots=True)
class CardResult:
    """一张榜单卡片的成品与打码结论。"""

    path: Path
    censored: bool
    full_censored: bool
    drawn: int  # 实际画进卡片的封面数
    missing: int  # 没取到的封面数
    note: str = ""
    fallback_covers: int = 0  # 检测跑不起来、只能整张糊掉的封面数


def _section_title(channel: BaseChannel, page: RankingPage, board_label: str) -> str:
    """段落标题：渠道名；渠道缺这一档、改用了别的档时补上实际档位。"""
    actual = RANKING_BOARD_LABELS.get(page.board, page.board)
    if not actual or actual == board_label:
        return channel.display_name
    return f"{channel.display_name} · {actual}"


@dataclass(slots=True)
class _SectionSpec:
    """取封面之前就定下来的分段信息：缓存键只依赖这些，不依赖网络结果。"""

    channel: BaseChannel
    page: RankingPage
    items: list[RankingItem]
    title: str
    start_index: int
    seq: int


@dataclass(slots=True)
class _Section:
    """卡片里的一段：一个渠道的一页榜单（聚合模式下一个渠道一段）。"""

    title: str
    items: list[RankingItem]
    staged: list[Path | None]
    start_index: int  # 本段第一条的序号偏移（0 基，画的时候 +1）


@dataclass(slots=True)
class _Layout:
    """一页卡片的固定排版尺寸：多段共用同一套格子与字号，拼出来才对得齐。"""

    width: int
    pad: int
    gap: int
    columns: int
    cell_w: int
    cover_h: int
    cell_h: int
    header_h: int
    footer_h: int
    section_head_h: int
    line_gap: int
    max_height: int
    title_font: TextFont
    meta_font: TextFont
    id_font: TextFont
    rank_font: TextFont
    head_font: TextFont
    sub_font: TextFont
    foot_font: TextFont
    section_font: TextFont


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
        show_id: bool = True,
        censor: bool = False,
        censor_style: str = "mosaic",
        censor_block: int = 14,
        censor_padding: int = 12,
        full_censor_area: float = 0.3,
        cache_ttl: int = 0,
    ) -> None:
        self.temp_root = temp_root
        self.detector = detector
        self.columns = max(1, int(columns))
        self.page_size = max(1, int(page_size))
        self.show_author = show_author
        self.show_id = show_id
        self.censor = censor
        self.censor_style = censor_style
        self.censor_block = censor_block
        self.censor_padding = censor_padding
        self.full_censor_area = full_censor_area
        # 成型图片的复用时长（秒）：同一批榜单内容 + 同一套参数在期内直接返回原图，
        # 0 表示不缓存（与「榜单缓存时长」同一个开关，数据不缓存时图也不该缓存）
        self.cache_ttl = max(0, int(cache_ttl))

    # ---- 对外 ----
    async def render(
        self,
        channel: BaseChannel,
        page: RankingPage,
        *,
        board_label: str,
        page_number: int = 1,
        start_index: int = 0,
        download_hint: str = "",
        nav_hint: str = "",
    ) -> CardResult | None:
        """渲染单个渠道的一页榜单；没有可画的条目时返回 None。

        ``start_index`` 是这张卡片第一条之前的条数（0 基偏移），``download_hint`` 与
        ``nav_hint`` 是脚注里的「怎么下载」「怎么看后面」两句——都由命令层给（只有它知道
        这一页之后还有没有内容），卡片只负责画。
        """
        cards = await self._render_pages(
            [(channel, page)],
            board_label=board_label,
            page_number=page_number,
            start_index=start_index,
            download_hint=download_hint,
            nav_hint=nav_hint,
            combined=False,
        )
        return cards[0] if cards else None

    async def render_combined(
        self,
        pages: list[tuple[BaseChannel, RankingPage]],
        *,
        board_label: str,
        page_number: int = 1,
        start_index: int = 0,
        download_hint: str = "",
        nav_hint: str = "",
    ) -> list[CardResult]:
        """把多个渠道的一页榜单拼进同一张图（聚合模式）；放不下时按渠道拆成几张。

        返回按发送顺序排好的卡片列表：正常情况下就一张，各渠道从头往下排；
        拼出来超过高度上限时才分张，且只在渠道之间断开，不会把一段切两半。
        """
        return await self._render_pages(
            list(pages),
            board_label=board_label,
            page_number=page_number,
            start_index=start_index,
            download_hint=download_hint,
            nav_hint=nav_hint,
            combined=True,
        )

    async def _render_pages(
        self,
        pages: list[tuple[BaseChannel, RankingPage]],
        *,
        board_label: str,
        page_number: int,
        start_index: int,
        download_hint: str,
        nav_hint: str,
        combined: bool,
    ) -> list[CardResult]:
        """取封面 → 逐张打码 → 排版出图；聚合时可能产出多张。

        先按「这批榜单内容 + 这套渲染参数」算一个键，缓存期内的重复请求直接返回上一张
        成图：封面不用重取、露骨检测不用重跑（一次聚合榜光封面就要几十秒）。缓存键里
        带着标题与作品 ID，所以榜单换了内容自然换键，不会拿到别人的图。
        """
        specs: list[_SectionSpec] = []
        offset = start_index
        for section_index, (channel, page) in enumerate(pages):
            items = page.items[: self.page_size]
            if not items:
                continue
            specs.append(
                _SectionSpec(
                    channel=channel,
                    page=page,
                    items=items,
                    title=_section_title(channel, page, board_label),
                    start_index=offset,
                    seq=section_index,
                )
            )
            offset += len(items)
        if not specs:
            return []

        show_heads = len(specs) > 1
        layout = self._layout(combined=combined, show_heads=show_heads)
        head_title = (
            f"全渠道{board_label}" if combined else f"{board_label} · {specs[0].channel.display_name}"
        )
        cache_key = self._cache_key(
            specs, head_title, page_number, download_hint, nav_hint, show_heads
        )
        cache_ttl = self._cache_seconds(specs[0].page.board) if cache_key else 0
        if cache_key and cache_ttl:
            cached = await asyncio.to_thread(self._cache_read, cache_key, cache_ttl)
            if cached is not None:
                logger.info(
                    f"[{PLUGIN_NAME}] 榜单图命中缓存（{cache_key}），跳过取封面与检测"
                )
                return cached

        covers_dir = self.temp_root / "ranking"
        covers_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._cleanup_old)

        # 各段一起取：慢站点（要先翻详情页再找图址）不该把别的段的等待串起来
        semaphore = asyncio.Semaphore(RANKING_COVER_CONCURRENCY)
        staged_by_section = await asyncio.gather(
            *(
                self._stage_covers(
                    spec.channel,
                    spec.items,
                    covers_dir,
                    prefix=f"s{spec.seq}-{spec.channel.name}",
                    semaphore=semaphore,
                )
                for spec in specs
            )
        )
        sections: list[_Section] = [
            _Section(
                title=spec.title,
                items=spec.items,
                staged=staged,
                start_index=spec.start_index,
            )
            for spec, staged in zip(specs, staged_by_section)
        ]

        hit = False
        fallback = 0
        reason = ""
        for section in sections:
            state = await asyncio.to_thread(self._censor_covers, section.staged)
            hit = hit or state.hit
            fallback += state.fallback
            reason = reason or state.reason

        groups = self._split_sections(sections, layout)
        slug = "combined" if combined else specs[0].channel.name
        outputs: list[Path] = []
        cards: list[CardResult] = []
        for index, group in enumerate(groups):
            if cache_key:
                # 缓存命名的产物不再二次加工：reinforce 会另存一份再糊，见 reinforce()
                out_path = covers_dir / f"cache-{cache_key}-{index}.jpg"
            else:
                suffix = f"-{index + 1}" if len(groups) > 1 else ""
                out_path = covers_dir / f"{slug}-{specs[0].page.board}-p{page_number}{suffix}.jpg"
            drawn, missing = await asyncio.to_thread(
                self._compose,
                group,
                out_path,
                layout,
                head_title,
                page_number,
                download_hint,
                nav_hint,
                show_heads,
            )
            outputs.append(out_path)
            cards.append(
                CardResult(
                    path=out_path,
                    censored=False,
                    full_censored=False,
                    drawn=drawn,
                    missing=missing,
                    note="",
                )
            )

        await asyncio.to_thread(
            self._drop_staged,
            [path for section in sections for path in section.staged],
            set(outputs),
        )

        censored = self.censor and (hit or fallback > 0)
        for card in cards:
            card.censored = censored
            card.fallback_covers = fallback
        if self.censor and fallback:
            # 检测跑不起来（没模型/推理失败）时在拼图前就把那几张封面整张糊掉了，
            # 卡片本身不动：名次、标题、脚注都要留着给人看。整页只说明一次。
            cards[0].note = f"打码检测不可用，卡片上的 {fallback} 张封面已整张打码"
            logger.warning(
                f"[{PLUGIN_NAME}] {'全渠道' if combined else specs[0].title} 榜单打码检测"
                f"不可用，已整张糊掉 {fallback} 张封面：{reason}"
            )
        if cache_key and cache_ttl:
            await asyncio.to_thread(self._cache_write, cache_key, cards, cache_ttl)
        return cards

    # ---- 成图缓存 ----
    def _cache_key(
        self,
        specs: list["_SectionSpec"],
        head_title: str,
        page_number: int,
        download_hint: str,
        nav_hint: str,
        show_heads: bool,
    ) -> str:
        """键 = 这批榜单的全部可见内容 + 所有影响像素的参数；返回空串表示不缓存。"""
        if self.cache_ttl <= 0:
            return ""
        payload = {
            "v": 1,
            "head": head_title,
            "page": page_number,
            "hint": download_hint,
            "nav": nav_hint,
            "heads": show_heads,
            "grid": [self.columns, self.page_size, self.show_author, self.show_id],
            "censor": [
                self.censor,
                self.censor_style,
                self.censor_block,
                self.censor_padding,
                round(float(self.full_censor_area), 4),
            ],
            "sections": [
                {
                    "channel": spec.channel.name,
                    "board": spec.page.board,
                    "title": spec.title,
                    "start": spec.start_index,
                    "items": [
                        [str(item.comic_id), item.title, item.author, item.page_count]
                        for item in spec.items
                    ],
                }
                for spec in specs
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:16]

    def _cache_seconds(self, board: str) -> int:
        """这张图的复用时长：留到本档期结束为止（榜单在一档之内是同一份）。

        日榜守到当天 24 点、周榜守到下周一、月榜守到下月 1 号、总榜没有天然档期就留 7 天。
        内容本身就是缓存键的一部分，所以留久了也不会把旧榜单当新榜单发出去。
        """
        if self.cache_ttl <= 0:
            return 0
        now = datetime.now()
        if board == "day":
            end = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        elif board == "week":
            end = (now + timedelta(days=7 - now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        elif board == "month":
            end = (now.replace(day=1) + timedelta(days=32)).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
        else:
            end = now + timedelta(days=7)
        seconds = int((end - now).total_seconds())
        # 至少留配置里的缓存时长（有人把它调大时以大的为准），总长不超过上限
        return max(0, min(max(seconds, self.cache_ttl), RANKING_IMAGE_CACHE_MAX))

    def _cache_manifest(self, cache_key: str) -> Path:
        return self.temp_root / "ranking" / f"cache-{cache_key}.json"

    def _cache_read(self, cache_key: str, ttl: int) -> list[CardResult] | None:
        """缓存期内返回上次的成品；过期、清单缺失或产物被清掉都当未命中。"""
        manifest = self._cache_manifest(cache_key)
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            stored_at = float(data.get("stored_at", 0))
        except (OSError, ValueError):
            return None
        age = time.time() - stored_at
        if age < 0 or age > ttl:
            return None
        entries = data.get("cards")
        if not isinstance(entries, list) or not entries:
            return None
        cards: list[CardResult] = []
        for entry in entries:
            path = self.temp_root / "ranking" / str(entry.get("file", ""))
            if not path.is_file():
                return None
            cards.append(
                CardResult(
                    path=path,
                    censored=bool(entry.get("censored")),
                    full_censored=False,
                    drawn=int(entry.get("drawn", 0)),
                    missing=int(entry.get("missing", 0)),
                    note=str(entry.get("note", "")),
                    fallback_covers=int(entry.get("fallback_covers", 0)),
                )
            )
        return cards

    def _cache_write(
        self, cache_key: str, cards: list[CardResult], ttl: int
    ) -> None:
        """把成品清单落盘；写失败只影响下次复用，不影响这次发送。"""
        data = {
            "stored_at": time.time(),
            "ttl": ttl,
            "cards": [
                {
                    "file": card.path.name,
                    "censored": card.censored,
                    "drawn": card.drawn,
                    "missing": card.missing,
                    "note": card.note,
                    "fallback_covers": card.fallback_covers,
                }
                for card in cards
            ],
        }
        manifest = self._cache_manifest(cache_key)
        tmp = manifest.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(manifest)
        except OSError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 榜单图缓存写入失败：{exc}")

    async def reinforce(self, result: CardResult) -> CardResult:
        """发送被平台拦下后整张重糊一次再发（与单封面同一套补救思路）。

        先另存一份再糊：原图可能正躺在成图缓存里，就地糊掉会让后面命中缓存的请求
        也收到这张过度打码的图。
        """
        strong_path = result.path.with_name(
            f"{result.path.stem}-strong{result.path.suffix}"
        )
        try:
            await asyncio.to_thread(shutil.copyfile, result.path, strong_path)
        except OSError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 榜单图另存重糊副本失败，改为就地重糊：{exc}")
            strong_path = result.path
        await asyncio.to_thread(
            censor_full_image,
            strong_path,
            style=self.censor_style,
            block=max(self.censor_block, 28),
        )
        return CardResult(
            path=strong_path,
            censored=True,
            full_censored=True,
            drawn=result.drawn,
            missing=result.missing,
            note="平台拦下了这张图，已整张重糊后重发",
            fallback_covers=result.fallback_covers,
        )

    @staticmethod
    def _drop_staged(staged: list[Path | None], outputs: set[Path]) -> None:
        """删掉已贴进卡片的中间封面文件：它们的唯一用途就是这次合成。"""
        for path in staged:
            if path is None or path in outputs:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

    def _cleanup_old(self) -> None:
        """清掉过期的榜单临时图：卡片要等消息发出去才能删，所以只能按时间清。

        缓存产物留到缓存上限（内容变了会换键，旧文件到期自然清掉），其余中间封面与
        一次性成图只留一小时。
        """
        covers_dir = self.temp_root / "ranking"
        if not covers_dir.is_dir():
            return
        now = time.time()
        for path in covers_dir.iterdir():
            try:
                if not path.is_file():
                    continue
                keep = (
                    RANKING_IMAGE_CACHE_MAX
                    if path.name.startswith("cache-")
                    else CARD_FILE_TTL
                )
                if now - path.stat().st_mtime > keep:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    # ---- 封面 ----
    async def _stage_covers(
        self,
        channel: BaseChannel,
        items: list[RankingItem],
        covers_dir: Path,
        *,
        prefix: str,
        semaphore: asyncio.Semaphore,
    ) -> list[Path | None]:
        """并发取封面并落到临时文件；单张失败只记 None，不影响整张卡片。

        ``prefix`` 必须逐段唯一：聚合模式下一个目录里落好几段的封面，只用段内序号命名
        会让后一段覆盖前一段，最后整张图都用同一家的封面。``semaphore`` 由调用方给，
        让所有渠道共用同一个并发上限。
        """

        async def fetch(index: int, item: RankingItem) -> Path | None:
            async with semaphore:
                try:
                    payload = await asyncio.wait_for(
                        channel.fetch_cover(item.comic_id),
                        timeout=RANKING_COVER_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        f"[{PLUGIN_NAME}] 榜单封面取不到（{channel.name} "
                        f"{item.comic_id}）：超时 {RANKING_COVER_TIMEOUT} 秒"
                    )
                    return None
                except Exception as exc:  # noqa: BLE001 - 单张取不到就按缺图处理，不拖垮整张卡片
                    # 有的异常 str() 是空的（空消息的 ChannelError 之类），补上类型名
                    detail = str(exc) or exc.__class__.__name__
                    logger.info(
                        f"[{PLUGIN_NAME}] 榜单封面取不到（{channel.name} "
                        f"{item.comic_id}）：{detail}"
                    )
                    return None
                if not payload or len(payload) > RANKING_COVER_MAX_BYTES:
                    return None
                # 临时文件必须带真实图片后缀：打码是就地写回，后缀不是 Pillow 认得的
                # 扩展名时会直接报 unknown file extension，整批封面等于打不了码
                target = covers_dir / f"{prefix}-{index:02d}{_sniff_suffix(payload)}"
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
        """逐张检测并打码（检测器自身串行，所以一并放在一次线程调用里）。

        检测跑不起来的那张退化成**整张糊掉这张封面**；连整张糊都失败就把这张封面从
        待贴列表里摘掉（画成占位块），绝不把没糊过的图拼进卡片。
        """
        state = _CensorState()
        if not self.censor:
            return state
        detector = self.detector
        if detector is None or detector.model_path is None:
            # 开关打开但模型没到位：逐张整张糊，卡片照常出
            state.reason = "没有可用的检测模型"
            for index, path in enumerate(staged):
                if path is None:
                    continue
                if self._censor_whole_cover(path):
                    state.fallback += 1
                else:
                    staged[index] = None
            return state

        for index, path in enumerate(staged):
            if path is None:
                continue
            try:
                analysis = detector.analyze(path)
            except Exception as exc:  # noqa: BLE001 - 检测后端异常统一降级
                analysis = None
                reason = f"检测执行失败：{exc}"
            else:
                reason = "检测执行失败" if analysis.failed else ""
            if analysis is None or analysis.failed:
                if self._censor_whole_cover(path):
                    state.fallback += 1
                    state.reason = state.reason or reason
                else:
                    staged[index] = None
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
            except Exception as exc:  # noqa: BLE001 - 糊图失败时退回整张糊这张封面
                if self._censor_whole_cover(path):
                    state.fallback += 1
                    state.reason = state.reason or f"打码失败：{exc}"
                else:
                    staged[index] = None
                continue
            if censored:
                state.hit = True
        return state

    def _censor_whole_cover(self, path: Path) -> bool:
        """把一张封面整张糊掉，返回是否成功（失败的那张不能贴进卡片）。"""
        try:
            censor_full_image(path, style=self.censor_style, block=self.censor_block)
        except Exception as exc:  # noqa: BLE001 - 单张糊失败只记日志，别拖垮整张卡片
            logger.warning(f"[{PLUGIN_NAME}] 榜单封面整张打码失败：{exc}")
            return False
        return True

    # ---- 排版 ----
    def _layout(self, *, combined: bool, show_heads: bool) -> _Layout:
        """算出一页卡片的格子与字号；同一张图里的各段共用它。"""
        width = RANKING_CARD_WIDTH
        pad = RANKING_CARD_PADDING
        gap = RANKING_CARD_GAP
        columns = self.columns
        cell_w = max(1, (width - pad * 2 - gap * (columns - 1)) // columns)
        cover_h = int(cell_w * RANKING_COVER_ASPECT)
        line_gap = 6
        title_font = load_font(max(16, cell_w // 15))
        meta_font = load_font(max(14, cell_w // 19))
        id_font = load_font(max(13, cell_w // 22))
        text_h = title_font.size * RANKING_CARD_TITLE_LINES + line_gap + meta_font.size
        if self.show_id:
            # ID 单独一行：挤在作者页数同一行会被截掉，抄 ID 抄一半等于没写
            text_h += id_font.size + 2
        section_font = load_font(34)
        return _Layout(
            width=width,
            pad=pad,
            gap=gap,
            columns=columns,
            cell_w=cell_w,
            cover_h=cover_h,
            cell_h=cover_h + 12 + text_h,
            header_h=132,
            footer_h=76,
            section_head_h=(section_font.size + 24) if show_heads else 0,
            line_gap=line_gap,
            max_height=(
                RANKING_MAX_COMBINED_HEIGHT if combined else RANKING_MAX_CARD_HEIGHT
            ),
            title_font=title_font,
            meta_font=meta_font,
            id_font=id_font,
            rank_font=load_font(max(18, cell_w // 11)),
            head_font=load_font(46),
            sub_font=load_font(26),
            foot_font=load_font(24),
            section_font=section_font,
        )

    def _section_height(self, layout: _Layout, section: _Section) -> int:
        """一段占的高度：可选的小标题 + 若干行格子。"""
        rows = max(1, (len(section.items) + layout.columns - 1) // layout.columns)
        return (
            layout.section_head_h
            + rows * layout.cell_h
            + max(0, rows - 1) * layout.gap
        )

    def _split_sections(
        self, sections: list[_Section], layout: _Layout
    ) -> list[list[_Section]]:
        """按高度上限把各段分到几张图里；只在渠道之间断开，单段自己超高就独占一张。"""
        overhead = layout.pad * 2 + layout.header_h + layout.footer_h
        groups: list[list[_Section]] = []
        current: list[_Section] = []
        current_height = overhead
        for section in sections:
            height = self._section_height(layout, section)
            extra = height + (layout.gap if current else 0)
            if current and current_height + extra > layout.max_height:
                groups.append(current)
                current = []
                current_height = overhead
            current.append(section)
            current_height += height + (layout.gap if len(current) > 1 else 0)
        if current:
            groups.append(current)
        return groups

    def _compose(
        self,
        sections: list[_Section],
        out_path: Path,
        layout: _Layout,
        head_title: str,
        page_number: int,
        download_hint: str,
        nav_hint: str,
        show_heads: bool,
    ) -> tuple[int, int]:
        """把一组段落画成一张图，返回（画进去的张数, 缺图的张数）。"""
        width = layout.width
        pad = layout.pad
        gap = layout.gap
        total_items = sum(len(section.items) for section in sections)
        height = pad * 2 + layout.header_h + layout.footer_h
        for index, section in enumerate(sections):
            height += self._section_height(layout, section)
            if index:
                height += gap

        canvas = Image.new("RGB", (width, max(240, height)), _BG)
        draw = ImageDraw.Draw(canvas)

        # 标题栏
        layout.head_font.draw(draw, (pad, pad), head_title, _TEXT)
        scope = (
            f"第 {page_number} 页 · {len(sections)} 个渠道 · 共 {total_items} 部作品"
            if show_heads
            else f"第 {page_number} 页 · 共 {total_items} 部作品"
        )
        layout.sub_font.draw(
            draw,
            (pad, pad + layout.head_font.size + 14),
            scope,
            _MUTED,
        )
        draw.line(
            ((pad, pad + layout.header_h - 12), (width - pad, pad + layout.header_h - 12)),
            fill=_LINE,
            width=2,
        )

        drawn = 0
        missing = 0
        top = pad + layout.header_h
        for section_index, section in enumerate(sections):
            if section_index:
                top += gap
            if show_heads:
                # 段标题：聚合时每个渠道一段，读者要能一眼看出这一排在哪个站
                layout.section_font.draw(draw, (pad, top + 4), section.title, _TEXT)
                top += layout.section_head_h
                draw.line(
                    ((pad, top - 14), (width - pad, top - 14)), fill=_LINE, width=1
                )
            for index, item in enumerate(section.items):
                row, column = divmod(index, layout.columns)
                left = pad + column * (layout.cell_w + gap)
                cell_top = top + row * (layout.cell_h + gap)
                staged_cover = (
                    section.staged[index] if index < len(section.staged) else None
                )
                self._draw_cell(
                    canvas,
                    draw,
                    item=item,
                    index=section.start_index + index + 1,
                    staged=staged_cover,
                    left=left,
                    top=cell_top,
                    cell_w=layout.cell_w,
                    cover_h=layout.cover_h,
                    title_font=layout.title_font,
                    meta_font=layout.meta_font,
                    id_font=layout.id_font,
                    rank_font=layout.rank_font,
                    line_gap=layout.line_gap,
                )
                if staged_cover is not None:
                    drawn += 1
                else:
                    missing += 1
            top += self._section_height(layout, section) - layout.section_head_h

        # 脚注
        # 「看后面的名次」只在真有下一页时才画：最后一页还教人翻页，用户白翻一次
        hints = [hint for hint in (download_hint, nav_hint) if hint]
        footer_y = height - pad - layout.foot_font.size * len(hints) - 6
        for offset, text in enumerate(hints):
            layout.foot_font.draw(
                draw,
                (pad, footer_y + offset * (layout.foot_font.size + 4)),
                text,
                _MUTED,
            )

        if canvas.height > layout.max_height:
            # 超高时等比缩而不是裁：少看清一点也比少一截内容强
            ratio = layout.max_height / canvas.height
            scaled = canvas.resize(
                (max(1, int(canvas.width * ratio)), layout.max_height),
                Image.LANCZOS,
            )
            canvas.close()
            canvas = scaled
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
        id_font: TextFont,
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
        meta_y = text_top + title_font.size * RANKING_CARD_TITLE_LINES + line_gap
        if meta:
            meta_font.draw(draw, (left, meta_y), meta, _MUTED)
        if self.show_id and item.comic_id:
            # 序号过期、换了设备都还能照这行 ID 直接下
            id_text = self._fit_text(
                id_font, f"ID：{item.comic_id}", cell_w
            )
            if id_text:
                id_font.draw(
                    draw,
                    (left, meta_y + meta_font.size + 2),
                    id_text,
                    _ID_COLOR,
                )

    @staticmethod
    def _fit_text(font: TextFont, text: str, width: float) -> str:
        """宽度不够就从尾部折省略号：抄不出一半的 ID 没有意义。"""
        if font.measure(text) <= width:
            return text
        cut = text
        while cut and font.measure(cut + "…") > width:
            cut = cut[:-1]
        return f"{cut}…" if cut else ""

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
    """一轮封面打码的结论：有没有真的糊到、有多少张只能整张糊。"""

    hit: bool = False
    fallback: int = 0
    reason: str = ""

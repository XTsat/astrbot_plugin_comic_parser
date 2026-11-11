"""封面打码：把检测到的露骨区域糊掉，避免封面在聊天平台被拦。

只处理「框」不做整页分割：坐标来自 :mod:`core.nsfw.detector` 的 YOLO 检测结果。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
from astrbot.api import logger

from .coverage import union_coverage
from ...domain.constants import (
    COVER_CENSOR_STYLES,
    COVER_MAX_BOXES,
    DEFAULT_COVER_CENSOR_BLOCK,
    DEFAULT_COVER_CENSOR_PADDING,
    DEFAULT_COVER_CENSOR_STYLE,
    DEFAULT_COVER_FULL_CENSOR_AREA,
    PLUGIN_NAME,
)

Box = tuple[int, int, int, int]


def _to_pixel_box(
    box: Sequence[float], width: int, height: int, padding: int
) -> Box | None:
    """把模型输出的坐标换算成裁剪框，并向外扩 padding 像素。"""
    try:
        x1, y1, x2, y2 = (float(value) for value in box[:4])
    except (TypeError, ValueError):
        return None

    # 少数导出模型给的是 0-1 归一化坐标，统一换算成像素
    if max(abs(x1), abs(y1), abs(x2), abs(y2)) <= 1.5:
        x1, x2 = x1 * width, x2 * width
        y1, y2 = y1 * height, y2 * height

    left = max(0, int(round(min(x1, x2))) - padding)
    top = max(0, int(round(min(y1, y2))) - padding)
    right = min(width, int(round(max(x1, x2))) + padding)
    bottom = min(height, int(round(max(y1, y2))) + padding)

    if right - left < 2 or bottom - top < 2:
        return None
    return (left, top, right, bottom)


def coverage_ratio(boxes: Sequence[Box], width: int, height: int) -> float:
    """露骨区域占整图的比例（并集，重叠只算一次）。"""
    return union_coverage(boxes, width, height)


def _mosaic(image: Image.Image, box: Box, block: int) -> Image.Image:
    region = image.crop(box)
    width, height = region.size
    block = max(2, int(block))
    small = region.resize(
        (max(1, width // block), max(1, height // block)),
        Image.Resampling.BILINEAR,
    )
    return small.resize((width, height), Image.Resampling.NEAREST)


def _apply(image: Image.Image, box: Box, style: str, block: int) -> None:
    if style == "blur":
        region = image.crop(box).filter(
            ImageFilter.GaussianBlur(radius=max(2.0, block / 2))
        )
        image.paste(region, box)
    elif style == "bar":
        ImageDraw.Draw(image).rectangle(box, fill=(0, 0, 0))
    else:
        image.paste(_mosaic(image, box, block), box)


def _save(image: Image.Image, path: Path) -> None:
    """按原后缀写回：JPEG 统一走 92 质量，其它格式交给 Pillow 默认值。"""
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        image.save(path, quality=92)
        return
    try:
        image.save(path)
    except ValueError:
        # 后缀不是 Pillow 认得的图片扩展名（榜单卡片把取到的封面原样存成 cover-00.img），
        # Pillow 会报 unknown file extension；退回 PNG 写回，别让整次打码白跑
        image.save(path, "PNG")


def censor_cover(
    path: Path,
    boxes: Sequence[Sequence[float]],
    *,
    style: str = DEFAULT_COVER_CENSOR_STYLE,
    block: int = DEFAULT_COVER_CENSOR_BLOCK,
    padding: int = DEFAULT_COVER_CENSOR_PADDING,
    full_area: float = DEFAULT_COVER_FULL_CENSOR_AREA,
) -> tuple[bool, float]:
    """就地给封面打码。

    Args:
        path: 封面图片路径，处理结果覆盖原文件。
        boxes: 检测框列表，每个元素是 ``(x1, y1, x2, y2)``。
        style: ``mosaic`` / ``blur`` / ``bar``。
        block: 马赛克块大小，模糊样式的半径由它换算。
        padding: 每个框向外扩展的像素，避免贴边漏出。
        full_area: 检出面积占比达到该值就整张打码；``<= 0`` 关闭这条兜底。

    Returns:
        tuple[bool, float]: (是否真的改动过像素, 处理后估算的检出面积占比)。
    """
    style = style if style in COVER_CENSOR_STYLES else DEFAULT_COVER_CENSOR_STYLE

    with Image.open(path) as source:
        image = source.convert("RGB")

    width, height = image.size
    if width < 4 or height < 4:
        return False, 0.0

    pixel_boxes: list[Box] = []
    for raw in list(boxes)[:COVER_MAX_BOXES]:
        box = _to_pixel_box(raw, width, height, padding)
        if box is not None:
            pixel_boxes.append(box)

    coverage = coverage_ratio(pixel_boxes, width, height)
    if not pixel_boxes:
        return False, coverage

    if full_area > 0 and coverage >= full_area:
        _apply(image, (0, 0, width, height), style, block)
    else:
        for box in pixel_boxes:
            _apply(image, box, style, block)

    _save(image, path)

    logger.debug(
        f"[{PLUGIN_NAME}] 封面打码：样式 {style}，{len(pixel_boxes)} 处，"
        f"覆盖 {coverage:.0%}{'（整张）' if full_area > 0 and coverage >= full_area else ''}"
    )
    return True, coverage


def censor_full_image(
    path: Path,
    *,
    style: str = DEFAULT_COVER_CENSOR_STYLE,
    block: int = DEFAULT_COVER_CENSOR_BLOCK,
) -> None:
    """把整张图糊掉，就地覆盖原文件。

    用于「按检出框打的码不够糊、平台仍然把消息丢掉」的补救：QQ 的图片审核对
    局部马赛克还能认出来（实测同一张封面 14px 局部、20px 整张都被静默丢弃，
    28px 整张才放行），这时只能整张糊，不再依赖检出框是否覆盖到露骨区域。
    """
    style = style if style in COVER_CENSOR_STYLES else DEFAULT_COVER_CENSOR_STYLE

    with Image.open(path) as source:
        image = source.convert("RGB")

    width, height = image.size
    if width < 4 or height < 4:
        return

    _apply(image, (0, 0, width, height), style, block)
    _save(image, path)
    logger.debug(f"[{PLUGIN_NAME}] 封面整张打码：样式 {style}，块 {block}")

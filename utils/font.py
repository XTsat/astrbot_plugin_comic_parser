"""图片渲染的字体出口：中日韩字形 + 拉丁/数字两套，按字符自动分工。

容器里的中文字体经常是「纯回退字体」——例如 Debian 自带的 `DroidSansFallbackFull.ttf`
只有汉字，数字和字母全被画成豆腐块（实测同一份权重下 ``1``/``A``/``·`` 的掩码完全一致，
都是 .notdef 方框）。反过来 DejaVu 这类西文字体又没有汉字。所以单独挑一个字体是不够的，
这里把「找字体」做成两件事：

1. 找一个**真的能画出汉字**的字体（用两个不同汉字的掩码是否相同来判断，而不是看文件名）；
2. 找一个**真的能画出数字/字母**的字体（同样用掩码判断，能画汉字的字体若也覆盖拉丁就直接复用）。

绘制时逐字符选字体：ASCII 走拉丁，其余走中日韩。查不到中文字体时返回 ``None``，
上层据此把图片版退化成文字版，而不是硬画一张全是方框的图。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger

from ..domain.constants import (
    PLUGIN_NAME,
    RANKING_FONT_BUNDLED,
    RANKING_FONT_LATIN_PATHS,
    RANKING_FONT_PATHS,
)

if TYPE_CHECKING:  # 只为类型标注；Pillow 是必需依赖，import 放在函数里省启动时间
    from PIL import ImageDraw, ImageFont

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
# 字体解析结果只算一次：卡片渲染是高频路径，没必要每次都去 stat 一遍候选清单
_RESOLVED: dict[str, Path | None] = {}
_PAIRS: dict[tuple[int, bool], "TextFont"] = {}
_LOCK = threading.Lock()
# 判断字形时用的两个采样字符：同一字体里它们的掩码相同，说明画的是同一个 .notdef 方框
_PROBE_CJK = ("周", "日")
_PROBE_LATIN = ("0", "1")


def _data_dirs() -> list[Path]:
    """宿主可能允许用户自带字体的位置（AstrBot 的本地 T2I 也认数据目录）。"""
    dirs: list[Path] = []
    try:
        from astrbot.api.star import StarTools

        dirs.append(Path(StarTools.get_data_dir(PLUGIN_NAME)))
    except Exception as exc:  # noqa: BLE001 - 老版本/测试桩没有这个接口，不影响自带字体
        logger.debug(f"[{PLUGIN_NAME}] 取插件数据目录失败（忽略）: {exc}")
    return dirs


def _cjk_candidates() -> list[Path]:
    """中文字体候选：插件自带 → 宿主数据目录 → 系统 CJK 字体。"""
    paths: list[Path] = []
    for name in RANKING_FONT_BUNDLED:
        paths.append(_PLUGIN_ROOT / "fonts" / name)
    for directory in _data_dirs():
        for name in RANKING_FONT_BUNDLED:
            paths.append(directory / name)
            paths.append(directory / "fonts" / name)
    paths.extend(Path(item) for item in RANKING_FONT_PATHS)
    return paths


def _latin_candidates() -> list[Path]:
    paths: list[Path] = []
    for name in RANKING_FONT_BUNDLED:
        paths.append(_PLUGIN_ROOT / "fonts" / name)
    for directory in _data_dirs():
        for name in RANKING_FONT_BUNDLED:
            paths.append(directory / name)
    paths.extend(Path(item) for item in RANKING_FONT_LATIN_PATHS)
    return paths


def _glyphs_differ(font: "ImageFont.FreeTypeFont", probe: tuple[str, str]) -> bool:
    """两个不同字符画出来的掩码不同，才说明这个字体真的有这些字形。

    缺字时 FreeType 一律给 .notdef 方框，两个字符的掩码会一模一样——这比看文件名可靠
    （`DroidSansFallbackFull` 名字里带 Fallback，实际却只有汉字、没有数字）。
    """
    try:
        first = bytes(font.getmask(probe[0]))
        second = bytes(font.getmask(probe[1]))
    except Exception as exc:  # noqa: BLE001 - 掩码取不到就当这个字体不可用
        logger.debug(f"[{PLUGIN_NAME}] 字体字形探测失败: {exc}")
        return False
    return bool(first) and first != second


def _pick(kind: str, candidates: list[Path], probe: tuple[str, str]) -> Path | None:
    """在候选里挑第一个「文件存在且真的能画出这类字形」的字体。"""
    with _LOCK:
        if kind in _RESOLVED:
            return _RESOLVED[kind]

    from PIL import ImageFont

    found: Path | None = None
    for path in candidates:
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                continue
            probe_font = ImageFont.truetype(str(path), 32)
        except (OSError, ValueError):
            continue
        if _glyphs_differ(probe_font, probe):
            found = path
            break

    with _LOCK:
        _RESOLVED[kind] = found
    if found is None:
        logger.warning(
            f"[{PLUGIN_NAME}] 没有找到可用的{kind}字体，榜单图片版将降级："
            f"可把 Noto Sans SC 等字体放到插件目录的 fonts/ 下"
        )
    else:
        logger.info(f"[{PLUGIN_NAME}] 榜单卡片{kind}字体：{found}")
    return found


def resolve_font_path(bold: bool = False) -> Path | None:
    """能画汉字的字体文件；一个都没有时返回 None（图片版据此退化成文字版）。"""
    return _pick("中文", _cjk_candidates(), _PROBE_CJK)


def resolve_latin_path() -> Path | None:
    """能画数字与字母的字体文件；中文字体本身覆盖拉丁时直接复用它。"""
    cjk = resolve_font_path()
    from PIL import ImageFont

    if cjk is not None:
        try:
            probe = ImageFont.truetype(str(cjk), 32)
            if _glyphs_differ(probe, _PROBE_LATIN):
                return cjk
        except (OSError, ValueError):
            pass
    return _pick("西文", _latin_candidates(), _PROBE_LATIN)


def cjk_available() -> bool:
    """能不能画出汉字：决定图片版要不要直接退化成文字版。"""
    return resolve_font_path() is not None


class TextFont:
    """一组同字号的「汉字字体 + 拉丁字体」，逐字符分工绘制与度量。

    Pillow 没有字体回退链，只有混排这件事必须自己做：汉字走中日韩字体，ASCII 走西文字体，
    这样即使容器里只有「纯汉字回退字体」，名次与数字也不会变成方框。
    """

    def __init__(self, size: int) -> None:
        from PIL import ImageFont

        self.size = max(8, int(size))
        cjk_path = resolve_font_path()
        latin_path = resolve_latin_path()
        self.cjk = ImageFont.truetype(str(cjk_path), self.size) if cjk_path else None
        if latin_path is None:
            self.latin = self.cjk
        elif cjk_path is not None and latin_path == cjk_path:
            self.latin = self.cjk
        else:
            self.latin = ImageFont.truetype(str(latin_path), self.size)
        # 两个都没有时退回 Pillow 默认字体，至少还能画 ASCII
        if self.cjk is None and self.latin is None:
            self.cjk = ImageFont.load_default()
            self.latin = self.cjk

    @staticmethod
    def _is_latin(char: str) -> bool:
        """ASCII 与拉丁扩展走西文字体；汉字、全角标点、假名等都走中日韩字体。"""
        return ord(char) < 0x0250

    def font_for(self, char: str) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
        if self._is_latin(char):
            return self.latin or self.cjk
        return self.cjk or self.latin

    def measure(self, text: str) -> float:
        """按混排规则量宽度：逐字符累加，避免整串用一个字体量出来的偏差。"""
        total = 0.0
        for char in text:
            font = self.font_for(char)
            try:
                total += font.getlength(char)
            except AttributeError:  # 极老 Pillow 的默认字体没有 getlength
                total += self.size * 0.6
        return total

    def draw(
        self,
        drawer: "ImageDraw.ImageDraw",
        xy: tuple[float, float],
        text: str,
        fill: tuple[int, int, int],
    ) -> float:
        """逐字符绘制，返回总前进宽度（调用方据此布局）。"""
        x, y = float(xy[0]), float(xy[1])
        for char in text:
            font = self.font_for(char)
            drawer.text((x, y), char, font=font, fill=fill)
            try:
                x += font.getlength(char)
            except AttributeError:
                x += self.size * 0.6
        return x - float(xy[0])

    def wrap(self, text: str, max_width: float, max_lines: int) -> list[str]:
        """按像素宽度断行；超出 ``max_lines`` 行时把余下内容折成省略号。"""
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return []

        lines: list[str] = []
        current = ""
        consumed = 0
        for char in cleaned:
            candidate = current + char
            if self.measure(candidate) <= max_width or not current:
                current = candidate
                consumed += 1
                continue
            lines.append(current)
            if len(lines) >= max_lines:
                break
            current = char
            consumed += 1
        else:
            # 整串都塞下了，或正好断开：把最后一段收进结果
            if current and len(lines) < max_lines:
                lines.append(current)
                consumed = len(cleaned)

        if len(lines) >= max_lines and consumed < len(cleaned):
            lines[-1] = self._ellipsize(lines[-1], max_width)
        lines = self._avoid_widow(lines)
        return lines[:max_lines]

    @staticmethod
    def _avoid_widow(lines: list[str]) -> list[str]:
        """避免末行只剩一个字（孤字成行）：从上一行挪一个字下来，读起来更顺。"""
        if len(lines) < 2:
            return lines
        last = lines[-1]
        if len(last) != 1 or len(lines[-2]) < 2:
            return lines
        moved = lines[-2][-1]
        lines[-2] = lines[-2][:-1]
        lines[-1] = moved + last
        return lines

    def _ellipsize(self, text: str, max_width: float) -> str:
        """把一行缩到「放得下省略号」的长度。"""
        suffix = "…"
        while text and self.measure(text + suffix) > max_width:
            text = text[:-1]
        return (text or "") + suffix


def load_font(size: int, bold: bool = False) -> TextFont:
    """按字号拿一组混排字体（带缓存）。"""
    key = (max(8, int(size)), bold)
    with _LOCK:
        cached = _PAIRS.get(key)
    if cached is not None:
        return cached
    pair = TextFont(key[0])
    with _LOCK:
        _PAIRS[key] = pair
    return pair

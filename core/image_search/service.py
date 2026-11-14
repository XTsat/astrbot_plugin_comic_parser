"""搜图结果的归一化、跨语言优选与文本渲染。

上游的相似度是「这张图和库里某页像不像」的百分比，不是「这条对不对」的保证：同一本作品
常有多个语言版本，且每本会命中多页。所以展示前统一做三件事——按作品聚合、同书多语言
只留中文、按分数排序并截断。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...domain import PluginSettings
from .constants import (
    CHINESE_LANGS,
    HIGH_CONFIDENCE,
    LANG_LABELS,
    SOURCE_CHANNELS,
    SOURCE_LABELS,
    TITLE_LIMIT,
)

# 作品 ID 从作品页链接里抠：nhentai 是 /g/<id>，E-Hentai 是 /g/<gid>/<token>/
_NH_PATH = re.compile(r"/g/(\d+)")
_EH_PATH = re.compile(r"/g/(\d+)/([0-9a-f]{6,})")
_SLUG_TAIL = re.compile(r"/+$")


@dataclass(slots=True)
class ImageSearchHit:
    """一条搜图命中：只够认出作品并转成下载指令，不承载完整元信息。"""

    source: str  # 上游来源族（nhentai / ehentai / ...）
    comic_id: str  # 该来源的作品 ID，已收敛成对应渠道 normalize_id() 能吃的形态
    title: str = ""
    language: str = ""
    page: int = 0
    score: float = 0.0
    page_url: str = ""
    source_url: str = ""

    @property
    def subject(self) -> tuple[str, str]:
        """同书判定键：同一来源 + 同一作品 ID 视为同一本。"""
        return (self.source, self.comic_id)

    @property
    def display_title(self) -> str:
        """展示用标题：没有标题时退回「来源 + ID」，避免消息里出现空标题。"""
        title = " ".join(self.title.split())
        if len(title) > TITLE_LIMIT:
            title = title[: TITLE_LIMIT - 1] + "…"
        if title:
            return title
        source = SOURCE_LABELS.get(self.source, "")
        return f"{source} {self.comic_id}".strip() or f"未命名作品 {self.comic_id}"

    @property
    def language_label(self) -> str:
        """语言码翻成中文名；认不出来时说「其他语言」，不回显上游枚举。"""
        code = (self.language or "").strip().lower()
        return LANG_LABELS.get(code, "其他语言")

    @property
    def is_chinese(self) -> bool:
        """是否中文版本（简体/繁体都算）。"""
        return (self.language or "").strip().lower() in CHINESE_LANGS

    @property
    def channel(self) -> str:
        """能下载它的本地渠道名；空串表示本插件没有对应渠道。"""
        return SOURCE_CHANNELS.get(self.source, "")


def _comic_id(source: str, page_url: str, source_url: str) -> str:
    """从作品页/来源页链接里取出该渠道能直接吃的作品 ID。"""
    for url in (source_url, page_url):
        if not url:
            continue
        cleaned = _SLUG_TAIL.sub("", url)
        if source == "ehentai":
            match = _EH_PATH.search(cleaned)
            if match:
                return f"{match.group(1)}-{match.group(2)}"
        match = _NH_PATH.search(cleaned)
        if match:
            return match.group(1)
    return ""


def normalize_hits(payload: dict[str, Any]) -> tuple[list[ImageSearchHit], float]:
    """把上游响应归一化成命中列表与耗时（秒）。

    兼容 v2.2 的 `results[].path_segments` 与旧版 `data[]` 两种形状。
    """
    hits: list[ImageSearchHit] = []
    timing = payload.get("timing") or {}
    try:
        elapsed = float(timing.get("total_ms") or 0) / 1000.0
    except (TypeError, ValueError):
        elapsed = 0.0
    if not elapsed:
        try:
            elapsed = float(payload.get("executionTime") or 0)
        except (TypeError, ValueError):
            elapsed = 0.0

    rows = payload.get("results")
    if isinstance(rows, list):
        for row in rows:
            segments = row.get("path_segments") or []
            if not segments:
                continue
            seg = segments[0] if isinstance(segments[0], dict) else {}
            meta = seg.get("metadata") or {}
            source_meta = meta.get("source") or {}
            source = str(seg.get("source_family") or source_meta.get("key") or "unknown")
            title = str((meta.get("title") or {}).get("primary") or "")
            source_url = str(seg.get("source_url") or source_meta.get("url") or "")
            page_url = str(seg.get("page_url") or "")
            comic_id = _comic_id(source, page_url, source_url)
            if not comic_id:
                continue
            try:
                score = float(row.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            hits.append(
                ImageSearchHit(
                    source=source,
                    comic_id=comic_id,
                    title=title,
                    language=str(seg.get("language") or ""),
                    page=int(seg.get("page_no") or 0),
                    score=score,
                    page_url=page_url,
                    source_url=source_url,
                )
            )
    else:
        legacy = payload.get("data")
        if isinstance(legacy, list):
            for row in legacy:
                source = str(row.get("source") or "").lower()
                page_url = str(row.get("pagePath") or "")
                source_url = str(row.get("subjectPath") or "")
                comic_id = _comic_id(source, page_url, source_url)
                if not comic_id:
                    continue
                try:
                    score = float(row.get("similarity") or 0)
                except (TypeError, ValueError):
                    score = 0.0
                hits.append(
                    ImageSearchHit(
                        source=source,
                        comic_id=comic_id,
                        title=str(row.get("title") or ""),
                        language=str(row.get("language") or ""),
                        page=int(row.get("page") or 0),
                        score=score,
                        page_url=page_url,
                        source_url=source_url,
                    )
                )
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits, elapsed


def select_hits(
    hits: list[ImageSearchHit], settings: PluginSettings
) -> list[ImageSearchHit]:
    """按「同书聚合 → 语言优选 → 阈值 → 条数」的顺序筛出要展示的命中。

    同一本作品只留一条：开了语言优选就优先留中文版本（没有中文才退回最高分那一条），
    这样一本作品不会因为有多语言版本在列表里出现两三次。
    """
    best: dict[tuple[str, str], ImageSearchHit] = {}
    for hit in hits:
        current = best.get(hit.subject)
        if current is None:
            best[hit.subject] = hit
            continue
        if settings.image_search_prefer_chinese:
            if hit.is_chinese and not current.is_chinese:
                best[hit.subject] = hit
                continue
            if hit.is_chinese == current.is_chinese and hit.score > current.score:
                best[hit.subject] = hit
                continue
        elif hit.score > current.score:
            best[hit.subject] = hit
    selected = [
        hit
        for hit in best.values()
        if hit.score >= settings.image_search_min_similarity
    ]
    selected.sort(key=lambda hit: hit.score, reverse=True)
    limit = max(1, int(settings.image_search_max_results))
    return selected[:limit]


def render_report(
    hits: list[ImageSearchHit],
    *,
    elapsed: float,
    strict: bool,
    command_for: Callable[[ImageSearchHit], str],
    dropped: int = 0,
) -> str:
    """把选中的命中渲染成聊天文本，每条附一条可直接发送的下载指令。

    Args:
        hits: 已筛选、已排序的命中。
        elapsed: 上游耗时（秒），0 表示没拿到。
        strict: 是否严格模式（只影响抬头文案）。
        command_for: 把命中翻译成下载指令的回调；返回空串表示本插件无对应渠道。
        dropped: 因阈值/条数被截掉的条数，用于提示用户「还有更多」。
    """
    mode = "严格模式" if strict else "普通模式"
    head = f"🔍 搜图结果 · {mode}（🟢 很像　🟡 一般）"
    if elapsed >= 0.05:
        head += f" · 耗时 {elapsed:.1f}s"
    if not hits:
        if strict:
            return (
                f"{head}\n❌ 严格模式下没有相似度足够的作品。"
                "换一张更完整、更清晰的截图，或用 /comic 搜图 普通 放宽再试一次。"
            )
        return (
            f"{head}\n❌ 没有找到相似度足够的作品。"
            "换一张更完整、更清晰的截图再试；裁得太碎或整页缩放过度都会搜不到。"
        )
    lines = [head, ""]
    for index, hit in enumerate(hits, 1):
        flag = "🟢" if hit.score >= HIGH_CONFIDENCE else "🟡"
        # 相似度与语言并进标题行：命中页与来源名对「要不要下」没用，一个只说明截图截自第几页，
        # 另一个早就由下载命令里的 /nh、/eh 说过了
        detail = f" · 相似度 {hit.score:.0f}% · {hit.language_label}"
        lines.append(f"{index}. {flag} {hit.display_title}{detail}")
        command = command_for(hit)
        if command:
            lines.append(f"　 下载：{command}")
        else:
            lines.append("　 （这个来源本插件下不了，仅作参考）")
        lines.append("")
    if lines[-1] == "":
        lines.pop()
    if dropped:
        lines.append("")
        lines.append(f"另有 {dropped} 本没列出来（相似度不够或超出展示条数）。")
    return "\n".join(lines)

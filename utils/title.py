"""标题里的小标记：认出「AI 生成」这类括注，交给展示层单独摆。

上游站点常把来源写在标题的方括号里（``[AI Generated] 真正的标题``），各站写法并不统一。
这里只做**识别**、不动标题原文：标题照原样展示，标记由各展示层另外贴在行尾——
标题过长会被截断，贴在行尾的标记才不会被一起截掉。
"""

from __future__ import annotations

import re

# 括注写法在各站不统一：方括号 / 圆括号 / 中文方头括号，英文与中文写法混用
_AI_MARK = re.compile(
    r"[\[\(【]\s*ai[\s_\-]*(?:generated|生成)\s*[\]\)】]",
    re.IGNORECASE,
)

# 聊天列表行尾的标记：带 emoji 与汉字，比原文的方括号括注更打眼
AI_MARK_BADGE = "🤖AI生成"


def has_ai_mark(title: str) -> bool:
    """标题里是否带 AI 生成括注。"""
    return _AI_MARK.search(title or "") is not None

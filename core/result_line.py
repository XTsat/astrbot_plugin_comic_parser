"""列表条目的统一排版：标题一行、ID 一行，搜索 / 搜图 / 榜单共用。

三处列表以前各写一套：搜索结果把 ID 挤在标题行末尾，搜图把 ID 藏在下载命令里，
榜单又是另一种括号写法。长标题被截断时最要紧的 ID 反而先丢，用户只能看到半条。

统一成两行之后：**标题行**回答「这是哪一本」，**ID 行**回答「不靠序号怎么下、要不要下」
——页数与语言都跟在 ID 后面，因为它们是同一个判断的参考信息。ID 单独成行还让截断变安全：
行首的序号让「这一条从哪开始」一眼可辨，被砍掉半条时也好整条丢弃。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# ID 行的标签与缩进：全角空格缩进一格，与序号行错开
ID_LABEL = "ID："
INDENT = "　"
SEPARATOR = " · "

# 条目行的行首：`1、`（全角顿号）。截断时用它判断「这一行是不是新的一条」
_ITEM_HEAD = re.compile(r"^\d+、")


def starts_item(line: str) -> bool:
    """这一行是不是一条结果的开头（标题行）。"""
    return bool(_ITEM_HEAD.match(line))


def render_item(
    index: int,
    title: str,
    *,
    badge: str = "",
    tag: str = "",
    author: str = "",
    comic_id: str = "",
    page_count: int = 0,
    size_text: str = "",
    extras: Sequence[str] = (),
    mark: str = "",
) -> list[str]:
    """渲染一条结果：标题行 + ID 行。

    序号写成「1、」而不是「1.」：长消息会被平台转成图片，文转图模板把「1.」当 Markdown
    有序列表，一旦中间插进说明或空行，后面的条目就会从 1 重新编号。

    Args:
        index: 本条在列表里的序号（1 起，翻页后仍连续）。
        title: 已截断、已压平换行的标题。
        badge: 标题前的可信度标记（搜图的 🟢 / 🟡），没有就留空。
        tag: 聚合列表里的渠道标签（如 ``[JM] ``），没有就留空。
        author: 作者；空串表示上游没给，不占位。
        comic_id: 作品 ID；空串表示没有可展示的 ID，只出标题行。
        page_count: 作品页数；0 表示上游没给，不占位。
        size_text: 已格式化好的体积（如 ``约 25.4 MB``）；空串表示不占位。
            它由调用方算好传进来，因为「真实值还是估算值」只有渠道自己知道。
        extras: ID 行后面还要跟的信息（语言、相似度），按顺序排。
        mark: 标题行尾的小标记（如 AI 生成）。

    Returns:
        一到两行文本；调用方按原样拼进消息。
    """
    head = f"{index}、{badge}{tag}{title}"
    if author:
        head += f"（{author}）"
    head += mark
    if not comic_id:
        return [head]
    detail = [f"{ID_LABEL}{comic_id}"]
    if page_count > 0:
        detail.append(f"{page_count}P")
    if size_text:
        detail.append(size_text)
    detail.extend(part for part in extras if part)
    return [head, f"{INDENT}{SEPARATOR.join(detail)}"]

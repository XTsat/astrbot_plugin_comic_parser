"""体积格式化的通用工具：进度日志、结果摘要与失败说明共用同一套单位。"""

from __future__ import annotations


def format_size(num_bytes: int) -> str:
    """不足 1 MB 用 KB：小本子显示成 0.0 MB 看不出任何变化。"""
    if num_bytes < 1048576:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / 1048576:.1f} MB"


def truncate_keep_tail(text: str, limit: int, note: str, *, tail_lines: int = 2) -> str:
    """超长消息按行截断：保住开头的标题与结尾的可操作提示。

    Args:
        text: 待发送的正文。
        limit: 平台能装下的字符数上限。
        note: 截断时插进正文的那句说明（自带括号更好读）。
        tail_lines: 结尾要原样保留几行——「怎么下载」「怎么翻页」就在那里，
            从尾部硬截会把用户唯一能照做的那两句切掉。

    Returns:
        str: 未超限时原样返回，否则是「标题行 + 保留的结果行 + 说明 + 结尾提示」。
    """
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    if len(lines) <= tail_lines + 1:
        return text[: limit - len(note)].rstrip() + note
    head, tail = lines[0], lines[-tail_lines:]
    body = lines[1 : len(lines) - tail_lines]
    budget = limit - len(head) - len(note) - sum(len(line) + 1 for line in tail) - 4
    kept: list[str] = []
    used = 0
    for line in body:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join([head, *kept, note, *tail])

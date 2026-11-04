"""体积格式化的通用工具：进度日志、结果摘要与失败说明共用同一套单位。"""

from __future__ import annotations


def format_size(num_bytes: int) -> str:
    """不足 1 MB 用 KB：小本子显示成 0.0 MB 看不出任何变化。"""
    if num_bytes < 1048576:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / 1048576:.1f} MB"

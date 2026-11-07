"""覆盖率计算：把多个检测框栅格化后求并集。

直接相加各框面积会把重叠重复计入——漫画页上同一个部位常被连续检出好几个重叠框，
一相加覆盖率立刻虚高到 1.0，于是「只有几处小框」也会被判成「整张都露骨」。
这里栅格到一张粗网格上求并集，重叠只算一次。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

# 网格边长：96 对封面这种尺寸足够细（一格 ≈ 10px），又不至于让计算变慢
GRID = 96

Box = Sequence[float]


def union_coverage(boxes: Sequence[Box], width: int, height: int) -> float:
    """返回这些框覆盖整张图的比例（0-1），重叠部分只计一次。

    Args:
        boxes: 若干 ``(x1, y1, x2, y2)`` 像素坐标框。
        width: 图片宽。
        height: 图片高。

    Returns:
        float: 并集覆盖率；坐标越界会被裁掉，空列表返回 0。
    """
    if not boxes or width <= 0 or height <= 0:
        return 0.0

    try:
        import numpy as np
    except ImportError:
        # 没有 numpy 就退回面积相加（重叠会高估）；onnxruntime 本身就依赖 numpy，正常装不到这一步
        covered = 0.0
        for box in boxes:
            x1, y1, x2, y2 = (float(v) for v in box[:4])
            covered += max(0.0, x2 - x1) * max(0.0, y2 - y1)
        return min(1.0, covered / float(width * height))

    grid = np.zeros((GRID, GRID), dtype=bool)
    for box in boxes:
        x1, y1, x2, y2 = (float(v) for v in box[:4])
        gx1 = max(0, int(math.floor(min(x1, x2) / width * GRID)))
        gx2 = min(GRID, int(math.ceil(max(x1, x2) / width * GRID)))
        gy1 = max(0, int(math.floor(min(y1, y2) / height * GRID)))
        gy2 = min(GRID, int(math.ceil(max(y1, y2) / height * GRID)))
        if gx2 > gx1 and gy2 > gy1:
            grid[gy1:gy2, gx1:gx2] = True

    return float(grid.mean())

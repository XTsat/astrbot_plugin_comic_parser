"""落盘小工具：断点续跑要能认出「已经下好的图」，也要避免把写了一半的文件当成已下好。"""

from __future__ import annotations

import os
from pathlib import Path

from ..domain.constants import IMAGE_SUFFIXES


def find_existing_image(stem: Path) -> Path | None:
    """找出同一页已经落盘的图片文件（真后缀可能是 .jpg，也可能是 .webp）。

    断点续跑靠它跳过已下过的页：只认非空文件——空文件基本是上次被中途打断的残留。
    """
    candidates = [stem.with_suffix(suffix) for suffix in sorted(IMAGE_SUFFIXES)]
    candidates.append(stem)  # stem 自带后缀时 with_suffix 会把它换掉，这里补回原名
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


def write_image_atomically(path: Path, payload: bytes) -> None:
    """先写 .part 再改名：进程被中断时不会留下半张图冒充「这一页已经下好了」。"""
    tmp = path.with_name(f"{path.name}.part")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

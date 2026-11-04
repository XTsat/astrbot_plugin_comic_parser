"""落盘小工具。"""

from __future__ import annotations

import os
from pathlib import Path


def write_image_atomically(path: Path, payload: bytes) -> None:
    """先写 .part 再改名：进程被中断时不会留下半张图冒充「这一页已经下好了」。"""
    tmp = path.with_name(f"{path.name}.part")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

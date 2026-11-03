"""业务层：渠道抽象、渠道实现、统一打包加密、任务编排与准入控制。

依赖方向单向：`main.py → core → utils / domain`；`utils → domain`；`domain` 不依赖任何人。
"""

from .channel import BaseChannel
from .guard import AccessGuard
from .packer import Packer
from .registry import build_channels, command_map, describe_channels
from .task import DownloadOutcome, DownloadTask

__all__ = [
    "AccessGuard",
    "BaseChannel",
    "DownloadOutcome",
    "DownloadTask",
    "Packer",
    "build_channels",
    "command_map",
    "describe_channels",
]

"""业务层：渠道抽象、渠道实现、统一打包加密、任务编排、封面交付与准入控制。

依赖方向单向：`main.py → core → utils / domain`；`utils → domain`；`domain` 不依赖任何人。
"""

from .channel import BaseChannel
from .cover import CoverService
from .group_file import GroupFileResult, GroupFileUploader
from .guard import AccessGuard
from .packer import Packer
from .progress import ProgressHeartbeat, ProgressState
from .registry import (
    build_channels,
    command_map,
    describe_channels,
    describe_links,
    describe_websites,
    resolve_link,
)
from .search import SearchOutcome, SearchService
from .task import DownloadOutcome, DownloadTask

__all__ = [
    "AccessGuard",
    "BaseChannel",
    "CoverService",
    "DownloadOutcome",
    "DownloadTask",
    "GroupFileResult",
    "GroupFileUploader",
    "Packer",
    "ProgressHeartbeat",
    "ProgressState",
    "SearchOutcome",
    "SearchService",
    "build_channels",
    "command_map",
    "describe_channels",
    "describe_links",
    "describe_websites",
    "resolve_link",
]

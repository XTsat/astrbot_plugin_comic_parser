"""业务层：渠道抽象、渠道实现、统一打包加密、任务编排、封面交付与准入控制。

依赖方向单向：`main.py → core → utils / domain`；`utils → domain`；`domain` 不依赖任何人。
"""

from .channel import BaseChannel
from .cover import CoverResult, CoverService
from .group_file import GroupFileResult, GroupFileUploader
from .guard import AccessGuard
from .health import HealthIssue, HealthReport, check as check_health
from .janitor import CleanupResult, cleanup_temp
from .packer import Packer
from .probe import (
    ProbeOutcome,
    known_channel_names,
    pick_channel,
    probe_channel,
    render_selfcheck,
    selfcheck_channels,
    split_target_args,
)
from .progress import ProgressHeartbeat, ProgressState
from .nsfw import (
    Detector,
    ModelWeights,
    NsfwScore,
    create_detector,
    ensure_onnxruntime,
)
from .panel import PanelImage, PanelService
from .registry import (
    build_channels,
    command_map,
    describe_channels,
    describe_websites,
    resolve_link,
)
from .ranking import RankingOutcome, RankingService, RankingTurn
from .ranking_card import CardResult, RankingCardRenderer
from .search import SearchOutcome, SearchService
from .stats import (
    OUTCOME_FAIL,
    OUTCOME_OK,
    OUTCOME_PAUSED,
    ChannelStat,
    DownloadStats,
    StatsSnapshot,
)
from .task import DownloadOutcome, DownloadTask, active_work_dirs

__all__ = [
    "AccessGuard",
    "BaseChannel",
    "CoverResult",
    "ChannelStat",
    "CleanupResult",
    "ProbeOutcome",
    "CoverService",
    "Detector",
    "DownloadOutcome",
    "DownloadStats",
    "DownloadTask",
    "GroupFileResult",
    "HealthIssue",
    "HealthReport",
    "GroupFileUploader",
    "OUTCOME_FAIL",
    "OUTCOME_OK",
    "OUTCOME_PAUSED",
    "PanelImage",
    "ModelWeights",
    "PanelService",
    "NsfwScore",
    "create_detector",
    "ensure_onnxruntime",
    "Packer",
    "ProgressHeartbeat",
    "ProgressState",
    "CardResult",
    "RankingCardRenderer",
    "RankingOutcome",
    "RankingService",
    "RankingTurn",
    "SearchOutcome",
    "SearchService",
    "StatsSnapshot",
    "build_channels",
    "active_work_dirs",
    "check_health",
    "cleanup_temp",
    "known_channel_names",
    "pick_channel",
    "probe_channel",
    "render_selfcheck",
    "selfcheck_channels",
    "split_target_args",
    "command_map",
    "describe_channels",
    "describe_websites",
    "resolve_link",
]

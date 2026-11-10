"""数据定义层：常量、配置、模型与错误。本层不依赖插件内其它模块。"""

from .config import PluginSettings
from .constants import PACK_FORMATS, PLUGIN_NAME
from .errors import (
    ChannelError,
    CoverCensorError,
    ChannelUnavailableError,
    DownloadCancelled,
    DownloadError,
    DownloadPaused,
    PanelError,
    PackError,
    PluginError,
    SearchError,
    SearchQueryError,
    SearchTimeoutError,
    SearchUnsupportedError,
)
from .models import (
    ComicMeta,
    DownloadedComic,
    PackResult,
    SearchFilter,
    SearchItem,
    SearchPage,
    SearchQuery,
)

__all__ = [
    "ChannelError",
    "CoverCensorError",
    "ChannelUnavailableError",
    "ComicMeta",
    "DownloadCancelled",
    "DownloadError",
    "DownloadPaused",
    "DownloadedComic",
    "PanelError",
    "PACK_FORMATS",
    "PLUGIN_NAME",
    "PackError",
    "PackResult",
    "PluginError",
    "PluginSettings",
    "SearchError",
    "SearchFilter",
    "SearchItem",
    "SearchPage",
    "SearchQuery",
    "SearchQueryError",
    "SearchTimeoutError",
    "SearchUnsupportedError",
]

"""数据定义层：常量、配置、模型与错误。本层不依赖插件内其它模块。"""

from .config import PluginSettings
from .constants import PACK_FORMATS, PLUGIN_NAME
from .errors import (
    ChannelError,
    ChannelUnavailableError,
    DownloadCancelled,
    DownloadError,
    DownloadPaused,
    PackError,
    PluginError,
    SearchError,
    SearchTimeoutError,
    SearchUnsupportedError,
)
from .models import ComicMeta, DownloadedComic, PackResult, SearchItem, SearchPage

__all__ = [
    "ChannelError",
    "ChannelUnavailableError",
    "ComicMeta",
    "DownloadCancelled",
    "DownloadError",
    "DownloadPaused",
    "DownloadedComic",
    "PACK_FORMATS",
    "PLUGIN_NAME",
    "PackError",
    "PackResult",
    "PluginError",
    "PluginSettings",
    "SearchError",
    "SearchItem",
    "SearchPage",
    "SearchTimeoutError",
    "SearchUnsupportedError",
]

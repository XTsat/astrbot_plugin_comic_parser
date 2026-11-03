"""插件级常量：所有散落的字面量取值都应定义在这里。"""

from typing import Final

PLUGIN_NAME: Final = "astrbot_plugin_comic_parser"
PLUGIN_DISPLAY_NAME: Final = "漫画下载器"
PLUGIN_DESC: Final = (
    "多渠道漫画下载插件：按 ID 下载整本漫画，统一打包并加密发送（不含搜索与排行榜）"
)
PLUGIN_VERSION: Final = "v0.1.0"
PLUGIN_AUTHOR: Final = "XTsat"
PLUGIN_REPO: Final = "https://github.com/XTsat/astrbot_plugin_comic_parser"

# 单本漫画的整体超时（秒）：含元数据请求与全部图片下载
DEFAULT_DOWNLOAD_TIMEOUT: Final = 1800
DEFAULT_RETRY_TIMES: Final = 2
DEFAULT_MAX_CONCURRENT_TASKS: Final = 1

PACK_FORMATS: Final = ("zip", "pdf", "long_img", "none")
DEFAULT_PACK_FORMAT: Final = "zip"

# 长图打包参数：统一宽度后纵向拼接，超过单段上限则分段打包
LONG_IMG_WIDTH: Final = 1200
LONG_IMG_MAX_STRIP_HEIGHT: Final = 12000
LONG_IMG_MAX_PER_STRIP: Final = 30

IMAGE_SUFFIXES: Final = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
)

# 进度推送的节流：百分比步进与最小间隔（秒）双阈值，避免刷屏
PROGRESS_STEP_PERCENT: Final = 25
PROGRESS_MIN_INTERVAL: Final = 3.0

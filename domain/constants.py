"""插件级常量：所有散落的字面量取值都应定义在这里。"""

from typing import Final

PLUGIN_NAME: Final = "astrbot_plugin_comic_parser"
PLUGIN_DISPLAY_NAME: Final = "漫画下载器"
PLUGIN_DESC: Final = (
    "多渠道漫画下载插件：按 ID 下载整本漫画，统一打包并加密发送"
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

# 文件名模板：键是配置里的选项值，值是可以带占位符的模板。
# 带 {password} 的模板才会把密码写进文件名（写成英文括号，例如 标题[ID](密码)）——是否显示由模板决定。
FILENAME_TEMPLATES: Final = {
    "title_id": "{title}[{id}]",
    "title_id_pw": "{title}[{id}]({password})",
    "id_title": "{id}-{title}",
    "id_pw": "{id}({password})",
    "title_only": "{title}",
    "custom": "",
}
DEFAULT_FILENAME_TEMPLATE: Final = "title_id"
# 自定义模板里可用的占位符，供提示文案复用
FILENAME_PLACEHOLDERS: Final = ("{title}", "{id}", "{author}", "{password}")

# 长图打包参数：统一宽度后纵向拼接，超过单段上限则分段打包
LONG_IMG_WIDTH: Final = 1200
LONG_IMG_MAX_STRIP_HEIGHT: Final = 12000
LONG_IMG_MAX_PER_STRIP: Final = 30

IMAGE_SUFFIXES: Final = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
)

# 进度提示方式：贴表情（不刷屏）/ 文本进度 / 关闭
PROGRESS_STYLES: Final = ("emoji", "text", "none")
DEFAULT_PROGRESS_STYLE: Final = "emoji"

# 下载日志的间隔（秒）：长任务也能在日志里看出是卡住还是在走
DOWNLOAD_LOG_INTERVAL: Final = 15.0

# 文本进度的节流：百分比步进与最小间隔（秒）双阈值，避免刷屏
PROGRESS_STEP_PERCENT: Final = 25
PROGRESS_MIN_INTERVAL: Final = 3.0

# 贴表情用的 id 与 emoji_type 成对：类型 "1" 是 QQ 系统表情编号，类型 "2" 是 emoji 的
# Unicode 码点十进制（如 🎉 = U+1F389 = 127881）。这组 id 在群里逐条回读验证过；
# 私聊协议端不支持表情回应，命令层会自动退回文本进度。
PROGRESS_REACTIONS: Final = {
    "start": ("66", "1"),  # 爱心（系统表情）
    "done": ("124", "1"),  # OK（系统表情）
    "fail": ("38", "1"),  # 敲打（系统表情，锤子）
}

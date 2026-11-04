"""插件级常量：所有散落的字面量取值都应定义在这里。"""

from typing import Final

PLUGIN_NAME: Final = "astrbot_plugin_comic_parser"
PLUGIN_DISPLAY_NAME: Final = "漫画下载器"
PLUGIN_DESC: Final = (
    "多渠道漫画下载插件：按 ID 下载整本漫画，统一打包发送，支持密码加密"
)
PLUGIN_VERSION: Final = "v0.2.2"
PLUGIN_AUTHOR: Final = "XTsat"
PLUGIN_REPO: Final = "https://github.com/XTsat/astrbot_plugin_comic_parser"

# 单本漫画的整体超时（秒）：含元数据请求与全部图片下载
DEFAULT_DOWNLOAD_TIMEOUT: Final = 1800
DEFAULT_RETRY_TIMES: Final = 2
DEFAULT_MAX_CONCURRENT_TASKS: Final = 1

# 分段下载（秒）：一次请求最多连续下这么久，到点停下并保留断点，重新发命令接着下。
# 必须小于单本超时才有意义——比它大时按老规矩走硬超时，否则两个值打架行为不可预期。
DEFAULT_SEGMENT_TIMEOUT: Final = 900
# 断点保留时长（秒）：过期就在下次下载前清掉，避免改过配置的旧半成品一直占盘；0 表示不留断点
DEFAULT_RESUME_TTL: Final = 7200

# 下载前测速（探测）：候选域名/图床同时探测、按实测速度排序，镜像「连得上但极慢」时才不干等。
# 四项都可调（配置分组 probe），默认值取「够用、不用调」的档位。
DEFAULT_PROBE_ENABLED: Final = True
# 单个候选的探测超时（秒）：探测是并发跑的，等不到就当成慢节点，不拖住最快的那条。
# 不能太短——国内到境外站点的接口常常要 5s 以上才回，钉死在 5s 会让测速全线判死、白白退回盲选。
DEFAULT_PROBE_TIMEOUT: Final = 10
# 低于该速度（KB/s）的候选排到最后（不剔除：整批都慢时仍要有域名可用），0 表示不按速度筛
DEFAULT_PROBE_MIN_SPEED: Final = 150
# 测速结果缓存时长（秒）：同一批候选在有效期内复用排名，避免每本都重测，0 表示不缓存
DEFAULT_PROBE_CACHE_TTL: Final = 600

# 渠道私有常量（域名清单、单请求超时与重试默认值等）不放这里，
# 一律跟着渠道走：`core/channels/<渠道名>/constants.py`
PACK_FORMATS: Final = ("zip", "pdf", "long_img", "none")
DEFAULT_PACK_FORMAT: Final = "zip"

# 文件名模板：键是配置里的选项值，值是可以带占位符的模板。
# 带 {password} 的模板才会把密码写进文件名（写成英文括号，例如 标题[ID](密码)）——是否显示由模板决定。
FILENAME_TEMPLATES: Final = {
    "title_id": "{title}[{id}]",
    "title_id_pw": "{title}[{id}]({password})",
    "id_title": "[{id}]{title}",
    "id_title_pw": "[{id}]{title}({password})",
    "id_only": "{id}",
    "title_only": "{title}",
    "custom": "",
}
DEFAULT_FILENAME_TEMPLATE: Final = "id_title"
# 自定义模板里可用的占位符，供提示文案复用
FILENAME_PLACEHOLDERS: Final = ("{title}", "{id}", "{author}", "{password}")

# 长图打包参数：统一宽度后纵向拼接，超过单段上限则分段打包
LONG_IMG_WIDTH: Final = 1200
LONG_IMG_MAX_STRIP_HEIGHT: Final = 12000
LONG_IMG_MAX_PER_STRIP: Final = 30

IMAGE_SUFFIXES: Final = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
)

# 群文件上传：列目录要快（用户就等在命令里），上传则要给慢网络留够时间——
# 一本漫画打出来几十 MB，协议端还要转存进 QQ，卡在中间会让用户以为插件没反应
GROUP_FILE_LIST_TIMEOUT: Final = 20.0
GROUP_FILE_UPLOAD_TIMEOUT: Final = 600.0
DEFAULT_GROUP_FILE_FOLDER: Final = "漫画"

# 进度提示方式：贴表情（不刷屏）/ 文本进度 / 关闭
PROGRESS_STYLES: Final = ("emoji", "text", "none")
DEFAULT_PROGRESS_STYLE: Final = "emoji"

# 手动取消：`/comic stop`（中英文常见写法都认），`全部` 仅管理员可用（取消所有会话的下载）
STOP_WORDS: Final = frozenset({"stop", "cancel", "取消", "停止", "中止"})
STOP_ALL_WORDS: Final = frozenset({"all", "全部", "所有"})

# 取消与超时的巡检间隔（秒）：线程池里的下载只认取消令牌，得靠这个循环把它收掉
CANCEL_POLL_INTERVAL: Final = 0.5

# 进度心跳的间隔（秒）：与「下完一张图/一章」无关，到点就写一条进度日志，
# 慢网络下一章拖几分钟也不会出现「日志一片空白，像卡死了」
DOWNLOAD_LOG_INTERVAL: Final = 15.0

# 文本进度的节流：百分比步进与最小间隔（秒）双阈值，避免刷屏
PROGRESS_STEP_PERCENT: Final = 25
PROGRESS_MIN_INTERVAL: Final = 3.0
# 进度长时间零变化（张数没涨）时，仍然隔这么久发一条「还在走」的文本进度，
# 否则百来张的本子在慢节点上会几分钟一条消息都没有，用户以为掉线了
PROGRESS_STALL_INTERVAL: Final = 60.0

# 贴表情用的 id 与 emoji_type 成对：类型 "1" 是 QQ 系统表情编号，类型 "2" 是 emoji 的
# Unicode 码点十进制（如 🎉 = U+1F389 = 127881）。这组 id 在群里逐条回读验证过；
# 私聊协议端不支持表情回应，命令层会自动退回文本进度。
PROGRESS_REACTIONS: Final = {
    "start": ("66", "1"),  # 爱心（系统表情）
    "done": ("124", "1"),  # OK（系统表情）
    "fail": ("38", "1"),  # 敲打（系统表情，锤子）
}

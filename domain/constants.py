"""插件级常量：所有散落的字面量取值都应定义在这里。"""

from typing import Final

PLUGIN_NAME: Final = "astrbot_plugin_comic_parser"
PLUGIN_DISPLAY_NAME: Final = "漫画下载器"
PLUGIN_DESC: Final = (
    "多渠道漫画下载与搜索插件：贴作品链接、给 ID 或在聊天里搜索后下载整本漫画，统一打包发送，支持密码加密"
)
PLUGIN_VERSION: Final = "v0.4.1"
PLUGIN_AUTHOR: Final = "XTsat"
PLUGIN_REPO: Final = "https://github.com/XTsat/astrbot_plugin_comic_parser"

# 单本漫画的整体超时（秒）：含元数据请求与全部图片下载
DEFAULT_DOWNLOAD_TIMEOUT: Final = 1800
DEFAULT_RETRY_TIMES: Final = 2
DEFAULT_MAX_CONCURRENT_TASKS: Final = 1

# 分段下载（秒）：一次请求最多连续下这么久，到点停下并保留断点，发 /comic retry 接着下。
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

# 搜索：命令后面跟这些词时把剩余文本当关键词（`/jm sou 名字`、`/comic sou 名字`）。
# 中英文都留，避免用户按站点习惯敲 `search` 时被当成作品 ID。
SEARCH_WORDS: Final = frozenset({"sou", "search", "find", "搜索", "搜"})
DEFAULT_SEARCH_ENABLED: Final = True
# 单渠道搜索超时（秒）：聚合搜索里某个渠道卡住时，到点就跳过它，不拖住整条命令
DEFAULT_SEARCH_TIMEOUT: Final = 10
# 单渠道搜索结果展示条数上限
DEFAULT_SEARCH_PAGE_SIZE: Final = 5
# 全渠道聚合搜索一次最多展示多少条（各渠道轮流取，避免一个渠道占满）
DEFAULT_SEARCH_AGGREGATE_LIMIT: Final = 8
# 结果缓存时长（秒）：翻页/同一关键词重复搜索不重复打上游；0 表示不缓存
DEFAULT_SEARCH_CACHE_TTL: Final = 60
# 同会话搜索冷却（秒）：搜索比下载轻，但仍要防止一个人连续刷上游
DEFAULT_SEARCH_COOLDOWN: Final = 3
# 搜索超时后自动用插件全局代理重试（代理地址为空时无效）；重试有独立的时间预算，
# 不与正常搜索超时共用，避免为了等一次慢直连把代理重试也挤掉
DEFAULT_SEARCH_AUTO_PROXY: Final = True
DEFAULT_SEARCH_AUTO_PROXY_TIMEOUT: Final = 15
# 搜索条件最大长度：除了关键词还可能有多个类型前缀与排除项，防止把超长串拼进 URL
SEARCH_MAX_KEYWORD_LENGTH: Final = 96
# 搜索结果文本上限：聊天消息过长会被平台截断，超过就少显示几条
SEARCH_MAX_TEXT_CHARS: Final = 1800
# 搜索缓存最多保留多少条（同一关键词 × 渠道 × 页码）
SEARCH_CACHE_MAX_ENTRIES: Final = 256
# 搜索结果记忆时长（秒）：搜索后可以直接用序号下载、翻页，过期后同一个数字按原始 ID 处理。
# 时长要覆盖「看完结果再挑一本」的节奏，但也不宜太久，免得用户敲真实 ID 时被旧序号截走
SEARCH_RESULT_TTL: Final = 600
# 序号下载与翻页的记忆按会话保存，会话数量不可控，超限时先清过期记录
SEARCH_RESULT_MAX_SESSIONS: Final = 512
# 上游搜索一次请求最多取回多少条：上游一页通常是 20–50 条，取大值确保整页不丢；
# 展示层再按「单渠道结果条数 / 聚合结果条数」切片，因此一页上游结果能翻好几页展示页
SEARCH_UPSTREAM_FETCH_LIMIT: Final = 100
# 翻页命令词：动作与中文都认，用户不用记英文；匹配的是命令后的整个参数
SEARCH_NEXT_WORDS: Final = frozenset({"next", "下一页", "下页"})
SEARCH_PREV_WORDS: Final = frozenset({"prev", "previous", "上一页", "上页"})

# 渠道私有常量（域名清单、单请求超时与重试默认值等）不放这里，
# 一律跟着渠道走：`core/channels/<渠道名>/constants.py`
PACK_FORMATS: Final = ("zip", "pdf", "long_img", "none")
DEFAULT_PACK_FORMAT: Final = "pdf"

# 展示名：帮助、结果摘要与提示文案共用一份，免得各处自己写中文
PACK_FORMAT_LABELS: Final = {
    "zip": "ZIP 压缩包",
    "pdf": "PDF 文档",
    "long_img": "纵向长图",
    "none": "仅本地保存",
}
# 单次下载改格式：命令末尾追加这些词即可覆盖全局配置，只对这一次生效。
# 中英文都收——用户要么按配置里的值敲 zip，要么按聊天习惯敲「长图」「压缩包」。
PACK_FORMAT_ALIASES: Final = {
    "zip": "zip",
    "压缩包": "zip",
    "压缩": "zip",
    "pdf": "pdf",
    "文档": "pdf",
    "long_img": "long_img",
    "longimg": "long_img",
    "长图": "long_img",
    "none": "none",
    "本地": "none",
    "仅本地": "none",
    "保存": "none",
}

# 文件名模板：键是配置里的选项值，值是可以带占位符的模板。
# 带 {password} 的模板才会把密码写进文件名（写成英文括号，例如 标题[ID](密码)）——是否显示由模板决定。
# 预置只留常用形态：简写与完整 ID 各有一个「ID + 标题」，其余组合用自定义模板。
FILENAME_TEMPLATES: Final = {
    "id_short_title": "[{id_short}]{title}",
    "id_title": "[{id}]{title}",
    "id_title_pw": "[{id}]{title}({password})",
    "title_id": "{title}[{id}]",
    "title_id_pw": "{title}[{id}]({password})",
    "id_only": "{id}",
    "title_only": "{title}",
    "custom": "",
}
# 默认用简写 ID + 标题：EH 的 token、哔咔的 24 位 ObjectId 直接进文件名太长
DEFAULT_FILENAME_TEMPLATE: Final = "id_short_title"
# 自定义模板里可用的占位符，供提示文案复用。
# `{id}` 是完整 ID；`{id_short}` 是渠道给的简写（E-Hentai 去 token、哔咔取后 8 位，
# 其它渠道 ID 本身够短、两者相同）。简写只进文件名，不能拿来发起下载。
FILENAME_PLACEHOLDERS: Final = (
    "{title}",
    "{id}",
    "{id_short}",
    "{author}",
    "{password}",
)

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

# 重试上一条：只在 `/comic` 下生效，用来代替「回去翻自己那条命令再发一遍」
RETRY_WORDS: Final = frozenset({"retry", "again", "重试", "继续", "再来一次"})

# 查看当前在跑与排队的请求：只读，`全部` 仅管理员可用（看所有会话）
STATUS_WORDS: Final = frozenset({"status", "queue", "状态", "队列", "排队"})

# 每个会话记一条最近的下载请求；上限按会话数截断，避免长期运行堆积
LAST_REQUEST_LIMIT: Final = 500

# 这条记录要存盘：插件重载会重建实例，只放内存里的话重载中断提示刚让你发 `/comic retry`，
# 发过去却回「没有下载记录」
LAST_REQUEST_FILENAME: Final = "last_request.json"

# 取消与超时的巡检间隔（秒）：线程池里的下载只认取消令牌，得靠这个循环把它收掉
CANCEL_POLL_INTERVAL: Final = 0.5

# 断点目录名：放 temp 下的独立子目录，启动清理与重载清理都要认出它才能整体保留或带走
RESUME_DIRNAME: Final = "resume"

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

# ---- 封面（下载完成后单独发到聊天的一张图）----

# 封面是附加交付：默认不发，想收封面的人自己打开
DEFAULT_COVER_SEND: Final = False
# 封面尺寸下限：占位图、图标这类小图不配当封面
COVER_MIN_SIDE: Final = 200

# ---- 封面打码 ----

COVER_CENSOR_STYLES: Final = ("mosaic", "blur", "bar")
COVER_CENSOR_STYLE_LABELS: Final = {
    "mosaic": "马赛克",
    "blur": "高斯模糊",
    "bar": "黑条",
}
# 打码默认关闭：不装模型也能收封面；主动打开「给封面打码」才依赖本地模型
DEFAULT_COVER_CENSOR: Final = False
DEFAULT_COVER_CENSOR_STYLE: Final = "mosaic"
DEFAULT_COVER_CENSOR_PADDING: Final = 12
DEFAULT_COVER_CENSOR_BLOCK: Final = 14
# 检出区域的「并集」覆盖整图达到该比例才整张打码。默认 0.6：绝大多数页面是局部打码，
# 只有真的整幅都在露骨画面里才糊整张。0 表示关闭这条兜底。
DEFAULT_COVER_FULL_CENSOR_AREA: Final = 0.6

# 发送被平台拦下后，整张重糊用的块大小。QQ 的图片审核对局部马赛克仍有识别能力，
# 整张重糊的块小到 20 也照样被静默丢弃（实测同一张封面 14/20 全被拦、28 才放行），
# 所以这里比默认值狠一档，宁可糊得难看也要让封面发得出去
COVER_RETRY_CENSOR_BLOCK: Final = 28
# 发送失败要不要自动整张重糊再发：默认开（否则那张封面就白生成了），关掉时失败即跳过
DEFAULT_COVER_RETRY_STRONG: Final = True
# 封面说明（露骨度/检出/覆盖/阈值）默认不发：它只是调参用的，跟「发送下载完成摘要」互不相干
DEFAULT_COVER_CAPTION: Final = False

# 单个封面最多处理这么多个框，误检刷出成百上千个框时不至于把渲染拖死
COVER_MAX_BOXES: Final = 64

# ---- NSFW 检测模型（YOLO 目标检测）----

# 权重不随插件分发：几十 MB 的二进制不适合进 git，用不用得到也因人而异。
# 权重统一用 ONNX（运行时只需 onnxruntime，约 200M 依赖），由上游 EraX-NSFW-V1.0
# 的 .pt 导出；上游只提供 .pt，不能直接拿来用。
#
# 预设 = 档位（n/s/m）× 精度（fp32/int8）。两个维度相互独立：
# - 档位决定模型容量（nano < small < medium），越大越准也越慢；
# - 精度决定权重怎么存：fp32 是训练时的 32 位浮点（无损），int8 把权重压成 8 位整数
#   （体积约 1/4，但 CPU 上实测反而更慢、边缘样本会掉档），所以默认 medium + fp32。
NSFW_MODEL_PRESETS: Final = {
    "m_fp32": ("erax_nsfw_yolo11m.onnx", "medium · fp32 完整精度（约 77MB，推荐）"),
    "m_int8": ("erax_nsfw_yolo11m_int8.onnx", "medium · int8 量化（约 20MB）"),
    "s_fp32": ("erax_nsfw_yolo11s.onnx", "small · fp32 完整精度（约 36MB）"),
    "s_int8": ("erax_nsfw_yolo11s_int8.onnx", "small · int8 量化（约 10MB）"),
    "n_fp32": ("erax_nsfw_yolo11n.onnx", "nano · fp32 完整精度（约 10MB，最省）"),
    "n_int8": ("erax_nsfw_yolo11n_int8.onnx", "nano · int8 量化（约 3MB）"),
}
DEFAULT_NSFW_MODEL_PRESET: Final = "m_fp32"
# 权重发布用的独立 tag：和插件版本解耦，换权重不用动插件版本号
NSFW_ONNX_RELEASE_TAG: Final = "weights-erax-onnx-v1"
NSFW_ONNX_RELEASE_URL: Final = (
    "https://github.com/XTsat/astrbot_plugin_comic_parser/releases/download/"
    + NSFW_ONNX_RELEASE_TAG
    + "/{filename}"
)
NSFW_MODEL_VARIANT_LABELS: Final = {
    "n": "nano（约 5MB，最省资源）",
    "s": "small（约 18MB）",
    "m": "medium（约 39MB，推荐）",
}
DEFAULT_NSFW_MODEL_VARIANT: Final = "m"

# 小于这个体积基本可以断定下到的是错误页而不是权重
NSFW_MIN_MODEL_BYTES: Final = 1024 * 1024
# 单个源的总时长上限与「卡住」判定：国内直连 GitHub 常出现连得上但只有几 KB/s 的情况，
# 所以用「多久没收到数据」判死（sock_read），而不是一刀切总时长，避免慢但可用的源被误杀。
NSFW_MODEL_DOWNLOAD_TIMEOUT: Final = 1800.0
NSFW_DOWNLOAD_CONNECT_TIMEOUT: Final = 20.0
NSFW_DOWNLOAD_READ_TIMEOUT: Final = 45.0

# GitHub 直连不通或太慢时的兜底加速代理（第三方公共服务，只做公开权重的中转）。
# 顺序即降级顺序；{url} 会被替换成完整的 GitHub 下载地址。
NSFW_MIRROR_SOURCES: Final = (
    "https://ghfast.top/{url}",
    "https://gh-proxy.com/{url}",
)

# EraX-NSFW-V1.0 的类别就是纯部位名，其它导出版本可能带前缀，因此用子串匹配
NSFW_BOX_KEYWORDS: Final = ("make_love", "penis", "vagina", "anus", "nipple")

# 实测（JM 真实页面 + nhentai 封面各一组）：露骨画面的置信度常见 0.02-0.2，漫画页里
# 强信号能到 0.5-0.8；0.03 比 0.05 多捞到约 15% 的页，再低就只剩噪声了。
DEFAULT_NSFW_THRESHOLD: Final = 0.03

# 推理输入尺寸：踩过坑——默认 640 时模型常在露骨页上吐一个「覆盖整幅」的粗框，
# 实测同一张 JM 页在 960 下变成几处正常的小框（整张糊 → 局部糊）。1280 反而掉召回。
DEFAULT_NSFW_IMGSZ: Final = 960
NSFW_IMGSZ_RANGE: Final = (320, 1600)
# 默认开启，但只在「下载完成后发送封面」与「给封面打码」都开着时才真的去下（见 main.initialize）：
# 打码默认关闭，此时不需要权重，没必要为它占几十 MB
DEFAULT_NSFW_AUTO_DOWNLOAD: Final = True

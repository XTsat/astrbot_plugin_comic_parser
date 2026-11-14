"""插件级常量：所有散落的字面量取值都应定义在这里。"""

from dataclasses import dataclass
from typing import Final

PLUGIN_NAME: Final = "astrbot_plugin_comic_parser"
PLUGIN_DISPLAY_NAME: Final = "漫画下载器"
PLUGIN_DESC: Final = (
    "多渠道漫画下载与搜索插件：贴作品链接、给 ID 或在聊天里搜索后下载整本漫画，统一打包发送，支持密码加密"
)
PLUGIN_VERSION: Final = "v0.7.2"
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
# 启动时是否清空临时目录：默认关。开着会在插件加载那一刻连 temp/resume 一起删掉，等于把
# 「断点保留」作废（改配置、更新插件都会触发重载）；关着时断点只按上面的时长过期，
# 代价是异常退出留下的临时目录要手动清
DEFAULT_CLEAN_TEMP_ON_START: Final = False

# 取消下载后等下载线程真正停手的上限（秒）：线程池里的下载不会被 asyncio 取消打断，
# 不等它退出就报「已保存 N 张」，用户紧接着 /comic retry 就会和后台线程抢同一个目录
DRAIN_TIMEOUT: Final = 30.0
# 等线程退出时的轮询间隔（秒）：只是看一眼 Future 有没有结束
DRAIN_POLL_INTERVAL: Final = 0.2

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
# 「这个纯数字是不是打错的序号」只用来说明原因，不参与判定：越界的数字在这个体量内
# 当序号误用解释，再大就按作品 ID 原样往下解析（六位以上的 ID 不会被这句提示拦下）
SEARCH_INDEX_HINT_MAX: Final = 99
# 翻页命令词：动作与中文都认，用户不用记英文；匹配的是命令后的整个参数
SEARCH_NEXT_WORDS: Final = frozenset({"next", "下一页", "下页"})
SEARCH_PREV_WORDS: Final = frozenset({"prev", "previous", "上一页", "上页"})

# 搜图（以图搜图）：默认值给到开箱可用，站点可直连；本地有反代时改 base_url 即可
DEFAULT_IMAGE_SEARCH_ENABLED: Final = True
DEFAULT_IMAGE_SEARCH_BASE_URL: Final = "https://soutubot.moe"
DEFAULT_IMAGE_SEARCH_TIMEOUT: Final = 120
DEFAULT_IMAGE_SEARCH_MAX_RESULTS: Final = 5
# 站点对任何图都返几十条且不做门槛过滤，45 是站点自己给的可采信线，低于它的基本是噪声
DEFAULT_IMAGE_SEARCH_MIN_SIMILARITY: Final = 45.0
# 同一本作品常有多个语言版本：默认只留中文，避免一本作品重复出现好几次
DEFAULT_IMAGE_SEARCH_PREFER_CHINESE: Final = True
DEFAULT_IMAGE_SEARCH_STRICT: Final = False
# 命令末尾的「只取封面」词：`/jm 123456 封面` 只发这一本的封面、不下载整本
COVER_WORDS: Final = frozenset({"封面", "cover"})
# 命令末尾的「只看信息」词：`/jm 123456 简介` 只回标题作者页数标签，不下载
INFO_WORDS: Final = frozenset({"简介", "信息", "详情", "info"})
# 搜图默认直连：上游对家用出口是放行的，反而经代理（机房/共享出口）更容易被 Cloudflare 弹挑战页
DEFAULT_IMAGE_SEARCH_USE_PROXY: Final = False
DEFAULT_IMAGE_SEARCH_PROXY: Final = ""

# 说明与站点速查：`/comic help`、`/comic site` 是标准写法，中文与旧命令名都保留，
# 省得老用户改习惯；这几个词只当 `/comic` 的首词匹配，不会抢作品 ID 或搜索关键词
HELP_WORDS: Final = frozenset({"help", "?", "？", "帮助", "用法", "说明"})
# 站点速查用单数主词 site（与历史别名 comicsites、中文「漫画网址」同源），复数一并认
SITE_WORDS: Final = frozenset({"site", "sites", "网站", "网址", "官网"})

# ---- 排行榜 ----

# 榜单窗口是跨渠道的统一词汇：各站原生档位并不一致（哔咔是 24 小时/7 天/30 天，
# 紳士没有日榜，Hanime1 没有月榜），所以由渠道用 `ranking_boards` 声明自己支持哪些窗口，
# 聚合层只展示交集，缺失的窗口在文案里说明，而不是报错或悄悄换一档
RANKING_BOARDS: Final = ("day", "week", "month", "all")
RANKING_BOARD_LABELS: Final = {
    "day": "日榜",
    "week": "周榜",
    "month": "月榜",
    "all": "总榜",
}
# 命令词：`/comic 排行`、`/jm rank` 都认；窗口词也认中文
RANKING_WORDS: Final = frozenset({"rank", "ranking", "排行", "排行榜", "榜"})
RANKING_BOARD_ALIASES: Final = {
    "day": "day",
    "d": "day",
    "today": "day",
    "daily": "day",
    "日": "day",
    "日榜": "day",
    "今日": "day",
    "每日": "day",
    "week": "week",
    "w": "week",
    "weekly": "week",
    "周": "week",
    "周榜": "week",
    "本周": "week",
    "这周": "week",
    "month": "month",
    "m": "month",
    "monthly": "month",
    "月": "month",
    "月榜": "month",
    "本月": "month",
    "all": "all",
    "total": "all",
    "全部": "all",
    "总": "all",
    "总榜": "all",
    "历史": "all",
}
# 默认看周榜：六家里五家都有周榜，日榜体量太小、月榜更新太慢
DEFAULT_RANKING_BOARD: Final = "week"
DEFAULT_RANKING_ENABLED: Final = True
# 榜单超时比搜索宽：几个渠道要抓 HTML 列表页，站点在境外且限速
DEFAULT_RANKING_TIMEOUT: Final = 15
# 单渠道每页条数：榜单看前十就够，再多刷屏
DEFAULT_RANKING_PAGE_SIZE: Final = 10
# 聚合时每个渠道取前几条：全渠道一起发时每个渠道都只给头部。默认 3 —— 六家 × 3 = 18 条，
# 既不会把单条消息撑到被平台截断，榜单这件事本身看的也就是前几名
DEFAULT_RANKING_AGGREGATE_LIMIT: Final = 3
# 榜单尺寸上限：连载长篇会长期霸榜、点进去也下不完，超上限的不进榜。
# 页数挡「一册就上千页」的厚本，章节数挡「几十章追不完」的连载；300 页 / 10 章这个组合
# 既能留住合订本，又能排除超长连载。两个都是 0 表示不限制
DEFAULT_RANKING_MAX_PAGES: Final = 300
DEFAULT_RANKING_MAX_CHAPTERS: Final = 10
# 配置兜底上限：再高就等于没限制，写个能过 slider 的整数即可
RANKING_MAX_PAGES_LIMIT: Final = 10000
RANKING_MAX_CHAPTERS_LIMIT: Final = 1000
# 榜单缓存时长（秒）：榜单比搜索结果稳定得多，翻页与重复查看不必回回打上游
DEFAULT_RANKING_CACHE_TTL: Final = 300
# 同会话榜单冷却（秒）：与搜索同理，防一个人连续刷上游
DEFAULT_RANKING_COOLDOWN: Final = 3
# 输出形态：文字版可回序号下载，图片版是一张张榜单卡片，两个都要时先文字后图片
RANKING_OUTPUTS: Final = ("text", "image", "both")
DEFAULT_RANKING_OUTPUT: Final = "text"
# 一张榜单图放几个作品：3（3×1）/6（3×2）/9（3×3）。上限 9 是因为每张封面都要跑一次
# 露骨检测（单张约 0.9 秒且串行），9 张已经要 8 秒左右，再多等不起
DEFAULT_RANKING_IMAGE_PAGE_SIZE: Final = 6
RANKING_IMAGE_PAGE_SIZES: Final = (3, 6, 9)
DEFAULT_RANKING_IMAGE_COLUMNS: Final = 3
RANKING_IMAGE_COLUMNS_RANGE: Final = (2, 3)
DEFAULT_RANKING_IMAGE_SHOW_AUTHOR: Final = True
RANKING_MAX_TEXT_CHARS: Final = SEARCH_MAX_TEXT_CHARS
# 序号下载的记忆时长与搜索一致：同一句话里的「序号」只属于最近那次列表
RANKING_RESULT_TTL: Final = SEARCH_RESULT_TTL
RANKING_INDEX_HINT_MAX: Final = SEARCH_INDEX_HINT_MAX
# 榜单缓存与会话记忆的条数上限：与搜索同理，会话/窗口组合不可控，超限先清最旧的
RANKING_CACHE_MAX_ENTRIES: Final = 128
RANKING_RESULT_MAX_SESSIONS: Final = SEARCH_RESULT_MAX_SESSIONS

# ---- 榜单卡片（图片版）----

# 与长图同宽：QQ/微信缩放到手机屏后，1200 宽的小字仍然清楚
RANKING_CARD_WIDTH: Final = 1200
RANKING_CARD_PADDING: Final = 40
RANKING_CARD_GAP: Final = 26
# 封面按 1:1.42 预留格位（同人志封面的常见比例），取到的图按短边裁齐，
# 各条封面高低不齐时卡片也不会参差
RANKING_COVER_ASPECT: Final = 1.42
# 标题最多两行，超出截断：榜单里更长的标题只会挤掉作者与页数
RANKING_CARD_TITLE_LINES: Final = 2
RANKING_CARD_AUTHOR_CHARS: Final = 14
# 单张卡片高度上限：9 张一页时约 2100 像素，留够余量但别让解码爆掉
RANKING_MAX_CARD_HEIGHT: Final = 3200
# 聚合榜单会把各渠道拼进同一张图，比单渠道允许更高；仍超过时再按渠道分成几张
RANKING_MAX_COMBINED_HEIGHT: Final = 6000
# 成图复用的上限：榜单内容本身就是缓存键的一部分，所以保留久点也不会串页；
# 各档到期时间见 core.ranking_card 的 _board_cache_seconds（日榜到当天结束、
# 周榜到下周一、月榜到下月 1 号、总榜 7 天），清理按这个上限兜底
RANKING_IMAGE_CACHE_MAX: Final = 31 * 24 * 3600
# 卡面上是否画作品 ID（换设备、序号过期后还能照着 ID 下；关掉版面更干净）
DEFAULT_RANKING_IMAGE_SHOW_ID: Final = True
# 取封面的并发与超时：一张封面卡住不该让整张卡片生不出来。20 秒对「先详情页再图址页
# 再取图」的站点（紳士漫畫）偏紧，30 秒才稳定拿到
RANKING_COVER_CONCURRENCY: Final = 6
RANKING_COVER_TIMEOUT: Final = 30
RANKING_COVER_MAX_BYTES: Final = 8 * 1024 * 1024

# 中文字体：图片版唯一的硬依赖，容器里常只有 DejaVu（没有汉字），
# 一律按「插件自带 → AstrBot 数据目录 → 系统 CJK 字体」的顺序找，全找不到就退化成文字版。
# 自带字体文件名（放插件目录 fonts/ 下）：Noto Sans SC 是 OFL 协议，可以随插件分发
RANKING_FONT_BUNDLED: Final = (
    "NotoSansSC-Regular.otf",
    "NotoSansSC-Regular.ttf",
    "思源黑体.otf",
    "msyh.ttc",
    "simhei.ttf",
)
# 系统字体路径：覆盖 Debian/Ubuntu、Alpine、CentOS 与 macOS 的常见位置
RANKING_FONT_PATHS: Final = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)
# 西文/数字字体：容器里的中文字体常常没有数字与字母（纯回退字体只有汉字），
# 所以混排时这两类字符单独找字体，避免名次被画成方框
RANKING_FONT_LATIN_PATHS: Final = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arial.ttf",
)

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
    "id_short_title_pw": "[{id_short}]{title}({password})",
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
# 产物名长度上限：先按字符数给用户调，再按 UTF-8 字节兜底。单名上限是 255 字节
# （ext4 / NTFS），而中文 1 字符占 3 字节——只数「字」的话，100 个中文标题就是 300 字节，
# 落盘会直接 ENAMETOOLONG，所以字节这一道不能省。
DEFAULT_FILENAME_MAX_LENGTH: Final = 100
FILENAME_LENGTH_RANGE: Final = (20, 150)
# 200 字节而不是 255：给 ".zip" 后缀和重名时的 "_2" 留余量
FILENAME_MAX_BYTES: Final = 200

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

# 每个会话记下最近几本下载请求：`/comic retry` 默认认队尾那一本，加 `全部` 就整队重放，
# 所以要留成队列；同一本再下只挪到队尾、不重复记，条数上限只为防长期运行堆积
LAST_REQUEST_KEEP: Final = 30

# 记录涉及多少个会话：上限按会话数截断，避免长期运行堆积
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
# 检出区域的「并集」覆盖整图达到该比例才整张打码。默认 0.3：实测局部糊在覆盖率 17.9% 时
# QQ 放行、34.6% 时被静默丢弃，所以 20%-30% 以上就别再指望局部糊能过，直接整张糊省一次白跑。
# 0 表示关闭这条兜底。
DEFAULT_COVER_FULL_CENSOR_AREA: Final = 0.3

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

# ---- NSFW 检测模型（ONNX：目标检测 / 实例分割）----

# 权重不随插件分发：几十 MB 的二进制不适合进 git，用不用得到也因人而异。
# 运行时只认 ONNX（依赖 onnxruntime，约 200M）。
#
# 一份预设要说清四件事：用哪个权重文件、给用户看的文案、这份权重的类别按什么顺序排、
# 从哪几个地址下载。
# 类别必须跟着预设走：不同上游的类别集合与输出通道顺序都不一样（EraX 是五个写实部位，
# 二次元模型只有三个），顺序错位就会把部位打错码，所以推理代码里不再写死一份。
#
# EraX 那六个预设 = 档位（n/s/m）× 精度（fp32/int8）。两个维度相互独立：
# - 档位决定模型容量（nano < small < medium），越大越准也越慢；
# - 精度决定权重怎么存：fp32 是训练时的 32 位浮点（无损），int8 把权重压成 8 位整数
#   （体积约 1/4，但 CPU 上实测反而更慢、边缘样本会掉档），所以默认 medium + fp32。
@dataclass(frozen=True, slots=True)
class NsfwModelPreset:
    """一份可选权重：文件名、下拉文案、类别顺序与下载源。

    Attributes:
        filename: 落在 `models/` 下的文件名，也是自动下载的落盘名。
        summary: 下拉与状态页里给用户看的文案。
        labels: 输出通道对应的类别名，顺序必须与权重一致；空表示让检测器去读权重
            自带的 metadata（都没读到时才退回 ``DEFAULT_NSFW_LABELS``）。
        task: 权重是纯检测（``detect``）还是实例分割（``segment``）。分割导出在类别
            分数后面还挂着 mask 系数列，解析时必须少看这几列。
        sources: 下载地址候选，顺序即降级顺序，支持 ``{filename}`` 占位；空表示走
            EraX 的老路径（插件 Release + 加速镜像）。
        family: 模型家族（``erax`` / ``anime`` / ``anime_seg``），面板按它分组。
        license: 上游声明的许可协议，面板「模型下载」页原文列出，供用户自行判断。
        upstream: 上游仓库地址，方便用户自己核对版本与许可。
        note: 这一档的短板或适用场景，一句话，面板直接展示。
        approx_bytes: 还没下载时用来预估体积（下载后以实际文件大小为准）。
    """

    filename: str
    summary: str
    labels: tuple[str, ...] = ()
    task: str = "detect"
    sources: tuple[str, ...] = ()
    family: str = ""
    license: str = ""
    upstream: str = ""
    note: str = ""
    approx_bytes: int = 0


NSFW_TASK_DETECT: Final = "detect"
NSFW_TASK_SEGMENT: Final = "segment"
# Ultralytics 分割导出在类别分数之后固定挂这么多列 mask 系数（对应 32 个原型 mask）
NSFW_MASK_COEFFS: Final = 32

# EraX-NSFW-V1.0 的五个类别（训练数据偏真人照片，漫画封面召回差，所以另收二次元模型）
NSFW_ERAX_LABELS: Final = ("anus", "make_love", "nipple", "penis", "vagina")
# deepghs/anime_censor_detection（HuggingFace，仓库标签 MIT）：纯二次元 / 同人图训练。
# 短板要认：它没有 anus、也没有 make_love 这类整体场景，性交画面与后穴会漏检。
NSFW_ANIME_LABELS: Final = ("nipple_f", "penis", "pussy")
# 01miku/anime-nsfw-segm-yolo26：二次元数据训的 YOLO26-seg 权重，类别比 EraX 多两档人脸。
# 仓库标 MIT，但 ONNX metadata 里写的是 Ultralytics AGPL-3.0（与我们自己导出的 erax 同款情况）。
NSFW_ANIME_SEG_LABELS: Final = (
    "anus",
    "nipple",
    "penis",
    "vagina",
    "female face",
    "male face",
    "pubic hair",
)
# 类别顺序完全没线索时的兜底（老权重没有 metadata，硬猜只会猜错，所以按 EraX 走）
DEFAULT_NSFW_LABELS: Final = NSFW_ERAX_LABELS

# 权重发布用的独立 tag：和插件版本解耦，换权重不用动插件版本号
NSFW_ONNX_RELEASE_TAG: Final = "weights-erax-onnx-v1"
NSFW_ONNX_RELEASE_URL: Final = (
    "https://github.com/XTsat/astrbot_plugin_comic_parser/releases/download/"
    + NSFW_ONNX_RELEASE_TAG
    + "/{filename}"
)
# GitHub 直连不通或太慢时的兜底加速代理（第三方公共服务，只做公开权重的中转）。
# 顺序即降级顺序；{url} 会被替换成完整的 GitHub 下载地址。
NSFW_MIRROR_SOURCES: Final = (
    "https://ghfast.top/{url}",
    "https://gh-proxy.com/{url}",
)
# EraX 的下载源：Release 直连 → 两个加速镜像。展开后与预设的 sources 同一格式。
NSFW_ERAX_SOURCES: Final = (
    NSFW_ONNX_RELEASE_URL,
    *(mirror.format(url=NSFW_ONNX_RELEASE_URL) for mirror in NSFW_MIRROR_SOURCES),
)
def _hf_sources(repo: str, path: str) -> tuple[str, str]:
    """HuggingFace 上一份权重的（镜像, 源站）直链。

    这里**不能**用 ``{filename}`` 占位：上游路径是 ``censor_detect_v1.0_n/model.onnx``
    这种「子目录 + model.onnx」形状，与本地落盘名完全不同，拿本地名去拼只会拿到 404
    （实测 hf-mirror 直接回 404）。所以每个档位各自写死自己的上游路径。
    """
    return (
        f"https://hf-mirror.com/{repo}/resolve/main/{path}",
        f"https://huggingface.co/{repo}/resolve/main/{path}",
    )


# 三家上游的公共元数据：版本、许可与短板都写在预设里，面板直接读
_ERAX_META: Final = {
    "family": "erax",
    "license": "Apache-2.0（上游 EraX-NSFW-V1.0；ONNX 由本插件导出）",
    "upstream": "https://huggingface.co/erax-ai/EraX-NSFW-V1.0",
    "note": "写实照片向：覆盖 anus / make_love 等场景，漫画封面上召回偏低",
}
_ANIME_META: Final = {
    "family": "anime",
    "license": "MIT（上游仓库标注）",
    "upstream": "https://huggingface.co/deepghs/anime_censor_detection",
    "note": "纯二次元 / 同人图训练，召回高、体积小；没有 make_love 这类场景类别",
}
_ANIME_SEG_META: Final = {
    "family": "anime_seg",
    "license": "MIT（上游仓库标注；权重内部另注 Ultralytics AGPL-3.0）",
    "upstream": "https://huggingface.co/01miku/anime-nsfw-segm-yolo26",
    "note": "二次元分割：框最贴边，带 anus / vagina；人脸类别不会被打码",
}


def _erax_preset(filename: str, summary: str, approx_bytes: int) -> NsfwModelPreset:
    """EraX 六档共用的预设构造：类别、下载源与上游元数据只有一套。"""
    return NsfwModelPreset(
        filename=filename,
        summary=summary,
        labels=NSFW_ERAX_LABELS,
        sources=NSFW_ERAX_SOURCES,
        approx_bytes=approx_bytes,
        **_ERAX_META,
    )


_ANIME_REPO: Final = "deepghs/anime_censor_detection"


def _anime_preset(
    filename: str, summary: str, approx_bytes: int, upstream_path: str
) -> NsfwModelPreset:
    """二次元检测档：本地文件名与上游路径分开给。"""
    return NsfwModelPreset(
        filename=filename,
        summary=summary,
        labels=NSFW_ANIME_LABELS,
        sources=_hf_sources(_ANIME_REPO, upstream_path),
        approx_bytes=approx_bytes,
        **_ANIME_META,
    )


_ANIME_SEG_REPO: Final = "01miku/anime-nsfw-segm-yolo26"


def _anime_seg_preset(
    filename: str, summary: str, approx_bytes: int, upstream_path: str
) -> NsfwModelPreset:
    """二次元分割档：本地文件名与上游路径分开给。"""
    return NsfwModelPreset(
        filename=filename,
        summary=summary,
        labels=NSFW_ANIME_SEG_LABELS,
        task=NSFW_TASK_SEGMENT,
        sources=_hf_sources(_ANIME_SEG_REPO, upstream_path),
        approx_bytes=approx_bytes,
        **_ANIME_SEG_META,
    )


NSFW_MODEL_PRESETS: Final[dict[str, NsfwModelPreset]] = {
    "m_fp32": _erax_preset(
        "erax_nsfw_yolo11m.onnx", "medium · fp32 完整精度（约 77MB，推荐）", 80_651_274
    ),
    "m_int8": _erax_preset(
        "erax_nsfw_yolo11m_int8.onnx", "medium · int8 量化（约 20MB）", 20_971_520
    ),
    "s_fp32": _erax_preset(
        "erax_nsfw_yolo11s.onnx", "small · fp32 完整精度（约 36MB）", 37_748_736
    ),
    "s_int8": _erax_preset(
        "erax_nsfw_yolo11s_int8.onnx", "small · int8 量化（约 10MB）", 10_485_760
    ),
    "n_fp32": _erax_preset(
        "erax_nsfw_yolo11n.onnx", "nano · fp32 完整精度（约 10MB，最省）", 10_485_760
    ),
    "n_int8": _erax_preset(
        "erax_nsfw_yolo11n_int8.onnx", "nano · int8 量化（约 3MB）", 3_145_728
    ),
    # 二次元封面用这两档：召回比写实模型高得多，而且小得多、快得多
    "anime_n": _anime_preset(
        "anime_censor_v1.0_n.onnx",
        "二次元 · nano（约 12MB，漫画封面推荐）",
        12_104_146,
        "censor_detect_v1.0_n/model.onnx",
    ),
    "anime_s": _anime_preset(
        "anime_censor_v1.0_s.onnx",
        "二次元 · small（约 45MB）",
        44_586_353,
        "censor_detect_v1.0_s/model.onnx",
    ),
    # 二次元分割档：原生输入 640 / 1280，带的是 7 类（含人脸，人脸不打码）
    "anime_seg_n": _anime_seg_preset(
        "anime_seg_nano_x640.onnx",
        "二次元分割 · nano（原生 640 输入，约 6MB，最快）",
        5_902_685,
        "nsfw-anime-nano-x640.onnx",
    ),
    "anime_seg_m": _anime_seg_preset(
        "anime_seg_medium_x1280.onnx",
        "二次元分割 · medium（原生 1280 输入，约 48MB，更准更慢）",
        47_600_269,
        "nsfw-anime-medium-x1280.onnx",
    ),
}
DEFAULT_NSFW_MODEL_PRESET: Final = "anime_n"

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
NSFW_BOX_KEYWORDS: Final = ("make_love", "penis", "vagina", "anus", "nipple", "pussy")
# 命中这些的类别一律不打码：二次元分割档会给出「female face / male face」这类高分类，
# 把脸糊掉既没必要也会顺带糊掉周围的部位；pubic hair 的框常覆盖整片胯部，糊了反而
# 把本来该保留的画面一起盖住（真露骨时会有 penis / vagina 框兜底）
NSFW_IGNORED_KEYWORDS: Final = ("face", "pubic hair")

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

# ---- 插件面板（插件视图 views/panel）----

# 单张上传上限：面板的测试页只用来试封面尺度，卡一道免得误传整本压缩包把内存吃满
PANEL_MAX_UPLOAD_BYTES: Final = 16 * 1024 * 1024
# 内存里最多留几张图：够来回调参数，又不至于把整本漫画攒着
PANEL_MAX_IMAGES: Final = 24
# 页面能选的推理输入尺寸：640 容易吐「覆盖整幅」的粗框，1280 掉召回，960 是默认档
PANEL_IMGSZ_CHOICES: Final = (640, 960, 1280)
# 面板下载线路：把「用哪条源」做成页面上的一次选择，不用再改配置里的自定义地址
NSFW_DOWNLOAD_MODE_AUTO: Final = "auto"        # 按档位默认源（HF 镜像优先 / 插件 Release + 加速镜像）
NSFW_DOWNLOAD_MODE_MIRROR: Final = "mirror"    # 只走镜像（国内直连不通时）
NSFW_DOWNLOAD_MODE_DIRECT: Final = "direct"    # 只走上游源站
NSFW_DOWNLOAD_MODE_CUSTOM: Final = "custom"    # 用页面上填的地址
NSFW_DOWNLOAD_MODES: Final = (
    NSFW_DOWNLOAD_MODE_AUTO,
    NSFW_DOWNLOAD_MODE_MIRROR,
    NSFW_DOWNLOAD_MODE_DIRECT,
    NSFW_DOWNLOAD_MODE_CUSTOM,
)
# 哪些主机算「镜像」、哪些算「上游源站」：下载线路按主机名分流，不写死整套地址
NSFW_MIRROR_HOSTS: Final = ("hf-mirror.com", "ghfast.top", "gh-proxy.com")
NSFW_ORIGIN_HOSTS: Final = ("huggingface.co", "github.com")
# 手动上传权重的体积上限：最大的 medium 档约 80MB，给到 256MB 够用又不至于被人塞满磁盘
PANEL_MODEL_UPLOAD_MAX_BYTES: Final = 256 * 1024 * 1024

# 面板「渠道测试 / 拉封面」单次动作上限：hanime1 这类要抓两三个页面、还带图床择优，
# 网络差时能拖到分钟级；面板是一次交互，卡太久不如直接告诉用户「这渠道现在不通」
PANEL_CHANNEL_TIMEOUT: Final = 30.0
# 面板「设置」页跳过的配置组：渠道是 template_list（条目多、结构深），留给 AstrBot 设置页
PANEL_SETTINGS_SKIP_GROUPS: Final = ("channels",)

"""哔咔渠道私有常量。

这些值只服务哔咔一个渠道，且来自哔咔安卓客户端的固定实现（接口域名、app 头、签名密钥、
图片节点），不随插件版本变化，所以放在渠道文件夹里而不是 `domain/constants.py`。
"""

from typing import Final

# ---- 接口 ----
BASE_URL: Final = "https://picaapi.picacomic.com/"
# 接口路径（相对 BASE_URL，不带前导斜杠：签名算的就是这个相对路径 + query）
PATH_SIGN_IN: Final = "auth/sign-in"
# 搜索：官方 App 的「进阶搜索」，必须登录（401 不带 token）；旧接口 comics/search 仍存在但字段更少
PATH_SEARCH: Final = "comics/advanced-search"
# 排行榜（官方 App 的「排行榜」页）：tt 是时间窗，ct=VC 表示按漫画计数
PATH_LEADERBOARD: Final = "comics/leaderboard"
LEADERBOARD_CATEGORY: Final = "VC"
PATH_COMIC: Final = "comics/{comic_id}"
PATH_EPISODES: Final = "comics/{comic_id}/eps"
PATH_EPISODE_PAGES: Final = "comics/{comic_id}/order/{order}/pages"

# 客户端身份：api-key 与签名密钥是哔咔 app 的公开固定值，签名算法变了才需要用户覆盖密钥
API_KEY: Final = "C69BAF41DA5ABD1FFEDC6D2FEA56B"
DEFAULT_SECRET_KEY: Final = (
    "~d}$Q7$eIni=V)9\\RK/P.RM4;9[7|@/CA}b~OW!3?EV`:<>M7pddUBL5n|0/*Cn"
)
APP_VERSION: Final = "2.2.1.2.3.3"
APP_BUILD_VERSION: Final = "45"
APP_CHANNEL: Final = "2"
APP_PLATFORM: Final = "android"
APP_UUID: Final = "defaultUuid"
ACCEPT: Final = "application/vnd.picacomic.com.v1+json"
USER_AGENT: Final = "okhttp/3.8.1"

# ---- 网页作品页 ----
# 哔咔是纯 App 站，分享内容一般是 `pica://` 深链；带作品页的 http(s) 链接只在下面这个域名出现过。
# `/comic` 只认这种链接（其余分享形式请用 `/pica <ID>`），因为深链与纯 ID 无法从链接本身判断站点
WEB_COMIC_DOMAINS: Final = ("manhuapica.com",)

# ---- 图片节点 ----
IMAGE_SERVER_DEFAULT: Final = "https://storage1.picacomic.com"
IMAGE_SERVER_BS: Final = "https://storage-b.picacomic.com"
IMAGE_SERVER_TOBEIMG: Final = "https://img.picacomic.com"

# ---- 渠道默认配置 ----
DEFAULT_REQUEST_TIMEOUT: Final = 30
DEFAULT_RETRY_TIMES: Final = 3
DEFAULT_MAX_CONCURRENT_IMAGES: Final = 5

# 单章图片列表的分页上限：上游 pages 字段异常时防止无限翻页
MAX_PAGES: Final = 200
# 小于这个体积的响应几乎不可能是真图片（多为防盗链的 HTML 错误页）
MIN_IMAGE_BYTES: Final = 512

# 文件名简写保留的 ID 尾段长度：ObjectId 前 8 位是秒级时间戳（同一秒入库的作品会撞），
# 尾段含随机数与自增计数，区分度更高，所以取尾部而不是开头
SHORT_ID_LENGTH: Final = 8

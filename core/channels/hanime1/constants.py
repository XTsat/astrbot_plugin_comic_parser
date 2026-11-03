"""Hanime1 漫画渠道私有常量。

只有本渠道用得到的取值放这里（站点/图床域名、页面路径、该站点的超时与重试默认值），
跨渠道共用的常量仍留在 `domain/constants.py`。
"""

from typing import Final

# ---- 站点 ----
# 页面域名：一行一个、从上到下依次降级（官方站与镜像站页面结构一致）。
# 漫画区现在挂在 hanimeone.me，hanime1.me 上老作品仍可访问，所以两个都留着
DEFAULT_SITE_DOMAINS: Final = "hanimeone.me\nhanime1.me"
# 图床域名兜底值：Hanime1 的漫画区内容由 nhentai 供源，原图走 nhentai 图床。
# 默认留空＝用阅读页自己给出的图床地址（图床编号会变：i / i2 / i3）
DEFAULT_IMAGE_DOMAINS: Final = "i.nhentai.net"
# 读取代理：本机 IP 被 Cloudflare 拦下时，用它代取页面（留空＝只直连站点）。
# 默认填公开读取服务：站点对服务器 IP 很不友好，开箱即能抓页比「先撞 403 再让用户找服务」实用；
# 不想让页面经过第三方，把渠道配置里的「读取代理地址」清空即可
DEFAULT_FETCH_PROXY: Final = "https://r.jina.ai/"

# 图床备选域名：nhentai 的图床是一组镜像（i / i2 / i3 / i4），内容相同。
# 页面给的域名优先（阅读页的 data-prefix），但它可能只有 AAAA 记录——纯 IPv4 的网络
# 就连不上（i2 常见），所以下载前先探一下，谁通就用谁。
IMAGE_HOST_FALLBACKS: Final = ("i.nhentai.net", "i3.nhentai.net", "i4.nhentai.net")

# 作品详情页与阅读页：阅读页的页码从 1 开始
PATH_DETAIL: Final = "/comic/{comic_id}"
PATH_READER: Final = "/comic/{comic_id}/{page}"

# ---- 请求 ----
# 页面请求超时（秒）：站点在 Cloudflare 后面，偶尔会慢，给到 30 秒不至于误判失败
DEFAULT_REQUEST_TIMEOUT: Final = 30
# 单张图片重试次数：图床抖动常见，重试 2 次足够，再多只会拖长整本耗时
DEFAULT_IMAGE_RETRY_TIMES: Final = 2
# 并发下载图片数：图床对高并发比较敏感，5 路是稳定性与速度的折中
DEFAULT_IMAGE_CONCURRENCY: Final = 5

# 读取页数上限：站点返回的 data-pages 异常时，防止拼出天量图片地址
MAX_PAGES: Final = 2000
# 小于这个体积的响应几乎不可能是真图片（多为防盗链的 HTML 错误页）
MIN_IMAGE_BYTES: Final = 512

# 页面文字用 UTF-8，站点校验 UA，缺了会拿到 403
DEFAULT_USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# 阅读页内嵌的 extensions 是单字符类型标记，对应原图扩展名
IMAGE_EXTENSIONS: Final = {"j": ".jpg", "p": ".png", "g": ".gif", "w": ".webp"}

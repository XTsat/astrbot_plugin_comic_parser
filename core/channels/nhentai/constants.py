"""nhentai 渠道私有常量。

只有本渠道用得到的取值放这里（接口路径、站点/图床域名、该站点的请求超时与重试默认值），
跨渠道共用的常量仍留在 `domain/constants.py`。
"""

from typing import Final

# 官方 API v2：作品详情与图床清单。实测无需凭据、不触发 Cloudflare 挑战
GALLERY_API_PATH: Final = "/api/v2/galleries/{gallery_id}"
CDN_API_PATH: Final = "/api/v2/cdn"
# 搜索：官方 API v2 原生支持，匿名可用；匿名限流约 10 次/分钟，超限 429
SEARCH_API_PATH: Final = "/api/v2/search"

# 站点域名：官方站与镜像站结构一致，一行一个、从上到下依次降级
DEFAULT_SITE_DOMAINS: Final = "nhentai.net"
# 图床域名兜底：`/api/v2/cdn` 拿不到清单时用官方图床
DEFAULT_IMAGE_DOMAIN: Final = "https://i.nhentai.net"

# 浏览器 UA：页面抓取与图床都校验来源，缺了它会掉到 403
USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# 页面请求超时（秒）：接口在国内网络下偶有抖动，给到 30 秒不至于误判失败
DEFAULT_REQUEST_TIMEOUT: Final = 30
# 单张图片重试次数：图床抖动常见，重试 2 次足够，再多只会拖长整本耗时
DEFAULT_IMAGE_RETRY_TIMES: Final = 2
# 并发下载图片数：图床对高并发比较敏感，5 路是稳定性与速度的折中
DEFAULT_IMAGE_CONCURRENCY: Final = 5

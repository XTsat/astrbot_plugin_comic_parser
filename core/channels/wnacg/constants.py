"""紳士漫畫（wnacg）渠道私有常量。

只有本渠道用得到的取值放这里：镜像域名、该站点的请求超时与重试默认值、限速阈值。
跨渠道共用的常量（图片后缀、打包格式、文件名模板）仍留在 `domain/constants.py`。
"""

from typing import Final

# 站点镜像：一行一个、从上到下依次降级。
# 上游实现（keiyoushi/extensions-source 的 src/zh/wnacg）把这份清单做成**远程拉取**，
# 地址见 MIRROR_LIST_URL；这里默认值与那份清单在 2026-11-04 一致，换域名时改配置即可。
DEFAULT_SITE_DOMAINS: Final = "www.wnacg.buzz\nwww.wnacg.site"
MIRROR_LIST_URL: Final = "https://stevenyomi.github.io/source-domains/wnacg.txt"

# 必须用桌面 UA：移动 UA 会拿到移动版布局，页面结构与解析规则全不对
USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
)

# 页面请求超时（秒）：站点在 Cloudflare 后面，偶尔慢，给到 30 秒不至于误判失败
DEFAULT_REQUEST_TIMEOUT: Final = 30
# 单张图片重试次数：图床抖动常见，2 次够用
DEFAULT_IMAGE_RETRY_TIMES: Final = 2
# 并发下载图片数：图床是独立域名，4 路是稳定性与速度的折中
DEFAULT_IMAGE_CONCURRENCY: Final = 4

# 站内页面请求限速：超过 60 次/分钟 Cloudflare 直接回 429（上游注释里写明的实测结论），
# 这里取 50 次/分钟留余量
PAGE_REQUESTS_PER_MINUTE: Final = 50

# 搜索页：`q` 关键词、`p` 页码、`s` 排序（默认按创建时间倒序）。
# 注意：搜索页与详情页共用同一个 50 次/分钟限速器，搜索会占用下载的页面配额。
SEARCH_PATH: Final = "/search/index.php?s=create_time_DESC&q={keyword}&p={page}"

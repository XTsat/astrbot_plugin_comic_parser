"""E-Hentai 渠道私有常量。

只有本渠道用得到的取值放这里（站点/接口域名、该站点的请求超时与并发默认值），
跨渠道共用的常量仍留在 `domain/constants.py`。
"""

from typing import Final

# 站点域名：一行一个、从上到下依次降级。e-hentai 匿名可用，exhentai 需要登录 Cookie
DEFAULT_SITE_DOMAINS: Final = "e-hentai.org\nexhentai.org"

# 官方 API：`gdata` 取元信息。两个入口内容一致，api 子域不通时退回主站
API_HOSTS: Final = ("e-hentai.org", "api.e-hentai.org")
API_PATH: Final = "/api.php"

# 浏览器 UA：站点在 Cloudflare 后面，缺 UA 时接口直接 403（实测）
USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# 页面请求超时（秒）：站点在境外，给 30 秒不至于把偶发抖动误判成失败
DEFAULT_REQUEST_TIMEOUT: Final = 30
# 单张图片重试次数：5xx/网络抖动重试，509 与登录页不重试
DEFAULT_IMAGE_RETRY_TIMES: Final = 2
# 并发图片数：站点按 IP 记图片配额，3 路是速度与风控的折中
DEFAULT_IMAGE_CONCURRENCY: Final = 3
# 页面请求最小间隔（秒）：作品页/图片页都算页面加载，站点对翻页速率敏感
DEFAULT_REQUEST_INTERVAL: Final = 1.0
# 单次最多下载多少页：0 = 不限；大 gallery 建议配合分段续跑自己设上限
DEFAULT_MAX_PAGES: Final = 0

# 站点把这两种情况当「临时不可用」而不是内容不存在
QUOTA_STATUS: Final = 509

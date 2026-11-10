"""JM 渠道私有常量。

只有本渠道用得到的取值放这里（域名清单、请求超时与重试的默认值），
跨渠道共用的常量仍留在 `domain/constants.py`，避免各渠道互相依赖。
"""

from typing import Final

# 预填的域名清单（一行一个，从上到下依次降级）。
# API 域名是禁漫接口域名；网页域名是 18comic 系列站点，上游会不定期更换，
# 想跟随上游自动更新就把对应配置留空，交给 jmcomic 自己拉取。
DEFAULT_API_DOMAINS: Final = "\n".join(
    (
        "www.cdnhjk.net",
        "www.cdnutc.cc",
        "www.cdngwc.net",
        "www.cdngwc.club",
    )
)
DEFAULT_HTML_DOMAINS: Final = "\n".join(
    (
        "18comic.vip",
        "18comic.ink",
        "jmcomic-zzz.one",
        "jmcomic-zzz.org",
        "comic18j-moco.work",
        "comic18j-nico.site",
        "comic18j-ada.work",
    )
)

# 渠道单请求超时（秒）：与 curl_cffi 默认一致，配小了坏节点能更快放弃
DEFAULT_REQUEST_TIMEOUT: Final = 30
# 单请求重试次数：与 jmcomic 的 client.retry_times 默认一致，配置里直接写具体数字
DEFAULT_REQUEST_RETRY_TIMES: Final = 5

# 取封面的单域名超时（秒）：封面只有一张小图，实测一半图床域名会直接超时，
# 等满 30 秒的请求超时只会让面板等到没脾气，8 秒没回就换下一个域名
COVER_TIMEOUT: Final = 8

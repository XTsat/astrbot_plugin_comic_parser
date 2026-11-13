"""搜图（以图搜图）的私有常量。

只服务搜图这一件事的取值收在这里：入口词、上游端点、两档严格度系数、语言偏好、来源映射，
以及「反爬容错」的一组参数（浏览器头、节流间隔、缓存与重试退避）。
"""

from __future__ import annotations

from typing import Final

# 入口词：`/comic 搜图` 是标准写法，其余为别名；`/comic` 后只附图不带参数也认
IMAGE_SEARCH_WORDS: Final = frozenset({"搜图", "图搜", "以图搜", "img", "image"})
IMAGE_STRICT_WORDS: Final = frozenset({"严格", "strict"})
IMAGE_NORMAL_WORDS: Final = frozenset({"普通", "normal"})

# 上游搜索端点（相对 base_url）与两档系数：系数越大误报越少、也越容易搜不到
SEARCH_PATH: Final = "/api/search"
NORMAL_FACTOR: Final = "1.2"
STRICT_FACTOR: Final = "1.4"

# 上游挡在 Cloudflare 之后，四不像的请求头更容易被弹挑战页；照浏览器发就是了
BROWSER_HEADERS: Final = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": "https://soutubot.moe",
    "Referer": "https://soutubot.moe/",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}

# 出口 IP 被弹挑战的主要诱因是**短时间内的请求量**：群聊里几个人同时搜、或者同一个人反复
# 重试，几秒内连打十几次就会进挑战名单。所以本地先节流：两次上游请求之间至少隔这么久，
# 且全局串行——慢一点没关系，被拦一次要等好几分钟。
MIN_INTERVAL_SECONDS: Final = 2.5

# 同一张图重复搜（用户点错了再做一次、换严格度再试）没必要重新打上游
CACHE_TTL_SECONDS: Final = 180.0
CACHE_MAX_ENTRIES: Final = 64

# 挑战一旦触发会持续一小段时间：退避要够长才有意义，短促重试只会继续喂挑战
MAX_ATTEMPTS: Final = 3
RETRY_DELAYS: Final = (3.0, 8.0)
RETRY_STATUSES: Final = frozenset({403, 429, 500, 502, 503, 504})

# 站点自己把 45 当作可采信线：低于它基本是颜色/纹理噪声，展示时用同一个值标可信度
HIGH_CONFIDENCE: Final = 45.0

# 超过该体积先缩图再上传：上游对大图会直接拒收
MAX_UPLOAD_BYTES: Final = 8 * 1024 * 1024
IMAGE_JPEG_QUALITY: Final = 88
MIN_SCALE: Final = 0.3

# 中文版本：同一本作品有多种语言时默认只留中文，避免同一本刷屏
CHINESE_LANGS: Final = frozenset({"zh", "cn", "tw", "hk", "gb", "chs", "cht", "sc", "tc"})
LANG_LABELS: Final = {
    "jp": "日语", "ja": "日语", "zh": "简体中文", "cn": "简体中文", "chs": "简体中文",
    "sc": "简体中文", "tw": "繁体中文", "hk": "繁体中文", "cht": "繁体中文", "tc": "繁体中文",
    "gb": "英语", "en": "英语", "us": "英语", "kr": "韩语", "ko": "韩语", "ru": "俄语",
    "fr": "法语", "de": "德语", "es": "西班牙语", "pt": "葡萄牙语", "it": "意大利语",
    "th": "泰语", "vn": "越南语", "vi": "越南语",
}

# 上游来源族 → 本插件里能直接下载它的渠道名；不在表里的来源只展示、不给下载指令
SOURCE_CHANNELS: Final = {"nhentai": "nhentai", "ehentai": "ehentai"}
SOURCE_LABELS: Final = {"nhentai": "nhentai", "ehentai": "E-Hentai", "panda": "Panda"}

# 结果页文本上限：标题常带整段宣传语，原样发出去会刷屏
TITLE_LIMIT: Final = 48

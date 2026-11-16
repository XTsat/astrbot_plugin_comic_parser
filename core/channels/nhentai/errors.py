"""nhentai 渠道的用户可见错误类型。

取元信息有两条路（官方 API → 抓页面兜底），降级时必须分清两种失败：**作品没了**
（404，确定结论，换域名、抓页面、重试都不会变）和**这会儿连不上**（网络/代理/风控这类
临时问题，等一会儿重试、换线路才有意义）。分开抛不同类型，降级链路就不用靠文案去猜，
任务层也能据此决定要不要重试。
"""

from __future__ import annotations

from ....domain import ChannelError, ChannelUnreachable


class GalleryNotFound(ChannelError):
    """作品不存在或已下架：确定结论，不再继续换域名 / 抓页面重试。"""


class NhentaiUnreachable(ChannelUnreachable):
    """接口与页面这一趟都没打通（网络、代理或站点风控）：重试或换线路才有意义。"""
"""各下载渠道的实现。渠道只负责取元信息与下载图片，不涉及打包加密。"""

from .jmcomic import JMComicChannel

__all__ = ["JMComicChannel"]

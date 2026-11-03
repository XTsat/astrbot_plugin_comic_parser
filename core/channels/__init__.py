"""各下载渠道的实现。

**每个渠道一个文件夹**（`channels/<渠道名>/`）：渠道私有的全部代码都收在自己的文件夹里，
本文件只汇总导出渠道类。渠道之间互不 import，也不再往本目录放单文件渠道。
渠道只负责取元信息与下载图片，打包加密在上层统一处理。
"""

from .hanime1 import Hanime1Channel
from .jmcomic import JMComicChannel
from .nhentai import NhentaiChannel
from .picacg import PicacgChannel

__all__ = ["Hanime1Channel", "JMComicChannel", "NhentaiChannel", "PicacgChannel"]

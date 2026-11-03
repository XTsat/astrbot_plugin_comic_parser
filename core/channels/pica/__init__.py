"""哔咔（PicACG）漫画渠道。

**一个渠道 = 一个文件夹**：渠道私有的全部代码都放在这里，对外只暴露 `PicaChannel`，
上层的 `core/registry.py` / `main.py` 只认这个类，不碰本文件夹里的其它模块。

- `channel.py`：`BaseChannel` 实现（解析 ID → 取元信息 → 下载图片）
- `api.py`：哔咔接口的接入细节（HMAC 签名、登录、翻页、图片直链拼接、单图落盘）
- `constants.py`：该站点专用常量（接口域名、app 头、签名密钥、图片节点、默认值）
- `schema.json`：该渠道要贴进 `_conf_schema.json` 的 `channels.templates` 模板
- `selftest.py`：本地假站点的下载自测（不联网、不需要真账号，只验渠道能否真的落盘）

**本渠道需要哔咔账号**：没有登录态取不到章节与图片列表，账号密码填在渠道模板里。
"""

from .channel import PicaChannel

__all__ = ["PicaChannel"]

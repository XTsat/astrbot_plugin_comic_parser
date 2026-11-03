"""nhentai 渠道。

**一个渠道 = 一个文件夹**：渠道私有的全部代码都放在这里，对外只暴露 `NhentaiChannel`，
上层的 `core/registry.py` / `main.py` 只认这个类，不碰本文件夹里的其它模块。

- `channel.py`：`BaseChannel` 实现（解析 ID → 取元信息 → 下载图片）
- `site.py`：站点抓页（Cloudflare 友好）与作品页解析、图床地址构造
- `downloader.py`：图片并发下载、重试与图床风控兜底
- `progress.py`：下载会话状态、进度日志与重载中断
- `constants.py`：渠道私有常量（域名清单、超时与重试默认值）
- `schema.json`：本渠道在 `_conf_schema.json` 里那份模板的副本，方便整个文件夹被单独拿走复用；
  改配置项时两处一起改（`_conf_schema.json` 仍以它为准）
"""

from .channel import NhentaiChannel

__all__ = ["NhentaiChannel"]

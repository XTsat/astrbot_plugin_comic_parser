"""紳士漫畫（wnacg）渠道。

**一个渠道 = 一个文件夹**：渠道私有的全部代码都在这里，对外只暴露 `WnacgChannel`，
`core/registry.py` / `main.py` 只认这个类，不碰本文件夹的其它模块。

- `channel.py`：`BaseChannel` 实现（解析 ID → 取元信息 → 下载图片）
- `site.py`：站内页面抓取（多镜像降级 + 每分钟 50 次限速 + Cloudflare 兜底）与页面解析
- `downloader.py`：图片并发下载与重试
- `progress.py`：下载会话状态、进度日志与重载中断
- `constants.py`：渠道私有常量（域名清单、超时/重试/并发默认值、限速阈值）
- `schema.json`：本渠道在 `_conf_schema.json` 里那份模板的副本；改配置项时两处一起改
"""

from .channel import WnacgChannel

__all__ = ["WnacgChannel"]

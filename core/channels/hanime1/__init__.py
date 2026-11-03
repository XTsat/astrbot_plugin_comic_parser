"""Hanime1 漫画渠道。

**一个渠道 = 一个文件夹**：渠道私有的全部代码都放在这里，对外只暴露 `Hanime1Channel`，
上层的 `core/registry.py` / `main.py` 只认这个类，不碰本文件夹里的其它模块。

- `channel.py`：`BaseChannel` 实现（解析 ID → 取元信息 → 下载图片）
- `site.py`：抓页（Cloudflare 友好）、详情页/阅读页解析、图床地址构造
- `downloader.py`：图片并发下载、按页序补零命名、图床风控兜底
- `progress.py`：下载会话状态、定时进度日志、插件重载时的取消
- `constants.py`：该站点专用常量（域名、页面路径、超时/重试/并发默认值）
- `selftest.py`：本地假站点的下载自测（不联网，只验渠道能否真的落盘）

**只做漫画**：Hanime1 的视频（`/watch?v=<ID>`）不在本渠道范围内，输入视频链接会直接给出提示。
"""

from .channel import Hanime1Channel

__all__ = ["Hanime1Channel"]

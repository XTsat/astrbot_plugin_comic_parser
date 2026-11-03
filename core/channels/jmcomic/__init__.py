"""JM 漫画渠道。

**一个渠道 = 一个文件夹**：渠道私有的全部代码都放在这里，对外只暴露 `JMComicChannel`，
上层的 `core/registry.py` / `main.py` 只认这个类，不碰本文件夹里的其它模块。
新增渠道照这个形状建 `core/channels/<渠道名>/`，渠道之间互不 import，
要加只属于某个站点的解析、限速、登录逻辑，直接在本文件夹里加模块即可。

- `channel.py`：`BaseChannel` 实现（解析 ID → 取元信息 → 下载图片）
- `option.py`：把渠道配置翻译成 jmcomic 的 option，以及 jmcomic 的惰性 import
- `progress.py`：下载会话状态、进度回调与重载中断
- `constants.py`：渠道私有常量（域名清单、请求超时与重试默认值）
"""

from .channel import JMComicChannel

__all__ = ["JMComicChannel"]

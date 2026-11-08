"""插件内可预期错误。

所有异常的 message 都是**面向用户**的中文文案，命令层可以直接展示，
不需要再翻译一次；内部细节走 logger。
"""


class PluginError(Exception):
    """插件可预期错误的基类。"""


class ChannelError(PluginError):
    """渠道层面失败：ID 无法识别、上游返回异常等。"""


class ChannelUnavailableError(ChannelError):
    """渠道当前不可用：未启用、依赖缺失或配置不全。"""


class SearchError(PluginError):
    """搜索失败，message 是可直接发给用户的中文文案。"""


class SearchUnsupportedError(SearchError):
    """渠道没有实现搜索能力。"""


class SearchQueryError(SearchError):
    """搜索条件写法不合法：message 直接展示给用户。"""


class SearchTimeoutError(SearchError):
    """搜索超时：聚合搜索时单个渠道超时不应该拖垮整次结果。"""


class DownloadError(PluginError):
    """下载过程失败，或下载不完整到无法继续打包。"""


class PackError(PluginError):
    """打包/加密失败。"""


class DownloadPaused(PluginError):
    """下载到点先停下，**不是失败**：断点已保留，重新发一次命令会接着下。

    使用它的前提是掉头就走、不重试、也不清临时目录——这三件事都在任务层统一处理。
    """

    def __init__(self, message: str, *, saved: int = 0, total: int = 0) -> None:
        super().__init__(message)
        self.saved = saved
        self.total = total


class DownloadCancelled(PluginError):
    """用户手动取消（`/comic stop`）：与到点暂停一样保留断点，重发命令即可接着下。"""


class CoverCensorError(PluginError):
    """封面打码已开启但无法执行。

    此时**不发送封面**：用户明确要求打码，把未打码的图发出去违背意图，
    宁可少发一张图，也不能漏发一张没糊的图。
    """


class LabError(PluginError):
    """打码实验台没法继续：message 是直接显示在页面上的中文文案。"""

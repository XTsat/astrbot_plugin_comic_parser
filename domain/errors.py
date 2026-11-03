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


class DownloadError(PluginError):
    """下载过程失败，或下载不完整到无法继续打包。"""


class PackError(PluginError):
    """打包/加密失败。"""

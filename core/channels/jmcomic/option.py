"""jmcomic 的接入细节：惰性 import 与 option 构建。

jmcomic 是可选依赖：插件启动时不 import，只有真正用到本渠道时才懒加载，
缺失时给出可执行的提示而不是崩溃。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...channel import split_option_list
from .constants import (
    DEFAULT_API_DOMAINS,
    DEFAULT_HTML_DOMAINS,
    DEFAULT_REQUEST_RETRY_TIMES,
    DEFAULT_REQUEST_TIMEOUT,
)

if TYPE_CHECKING:  # 只为类型标注；运行时不引入渠道抽象，保持本模块可独立复用
    from ...channel import BaseChannel


def import_jmcomic() -> Any | None:
    """懒加载 jmcomic；未安装时返回 None，由调用方决定提示。"""
    try:
        import jmcomic
    except ImportError:
        return None
    return jmcomic


def build_option(
    channel: BaseChannel,
    jmcomic: Any,
    base_dir: Path,
    *,
    domains: list[str] | None = None,
    retry_times: int | None = None,
    request_timeout: int | None = None,
) -> Any:
    """把渠道配置翻译成 jmcomic 的 option。

    目录规则固定 Bd/Aid/Pindex：用 Aid 作目录名避开标题里的非法字符，
    再带章节维度 Pindex，否则多章节本子的各章会用相同页码互相覆盖。

    ``domains`` / ``retry_times`` / ``request_timeout`` 是给测速用的临时覆盖：
    测速要按「单个域名、零重试、短超时」去量，不能沿用下载时的取值，否则一个
    坏节点会用满重试次数把整轮测速拖成十几秒。
    """
    client_type = channel.opt_str("client_type", "api")
    impl = "html" if client_type.startswith("html") else "api"
    option_dict: dict[str, Any] = {
        "dir_rule": {"base_dir": str(base_dir), "rule": "Bd/Aid/Pindex"},
        "download": {
            "image": {"suffix": channel.opt_str("image_suffix", ".jpg")},
            "threading": {
                "photo": channel.opt_int("max_concurrent_photos", 3, minimum=1),
                "image": channel.opt_int("max_concurrent_images", 5, minimum=1),
            },
        },
        "client": {"impl": impl},
    }

    # 只有「自定义域名」或「测速排好序」时才用显式清单；其它交给 jmcomic 自动更新。
    # 清单顺序就是 jmcomic 的重试顺序，前一个失败自动换下一个。
    if domains is not None:
        option_dict["client"]["domain"] = {impl: list(domains)}
    elif client_type.endswith("_custom"):
        key = "html_domains" if impl == "html" else "api_domains"
        default_domains = (
            DEFAULT_HTML_DOMAINS if impl == "html" else DEFAULT_API_DOMAINS
        )
        option_dict["client"]["domain"] = {
            impl: channel.opt_list(key) or split_option_list(default_domains)
        }

    option_dict["client"]["retry_times"] = (
        channel.opt_int("retry_times", DEFAULT_REQUEST_RETRY_TIMES, minimum=1)
        if retry_times is None
        else max(0, retry_times)
    )

    # 单请求超时对 api / html 两种客户端都生效；配小了坏节点能更快放弃
    meta_data: dict[str, Any] = {
        "timeout": (
            channel.opt_int("request_timeout", DEFAULT_REQUEST_TIMEOUT, minimum=1)
            if request_timeout is None
            else max(1, request_timeout)
        )
    }
    # 代理按「渠道开关 → 插件代理 → AstrBot 本体代理」解析，开关关掉时是真直连
    proxy = channel.resolve_proxy(default_enabled=False)[0]
    if proxy:
        meta_data["proxies"] = proxy
    else:
        # 显式置空：容器里常带系统代理变量，不覆盖会让请求绕到不存在的代理上
        meta_data["proxies"] = {}
    option_dict["client"]["postman"] = {"meta_data": meta_data}

    return jmcomic.JmModuleConfig.option_class().construct(option_dict)

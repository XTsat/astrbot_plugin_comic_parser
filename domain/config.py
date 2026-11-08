"""配置解析。

WebUI 的配置是裸 dict（含用户手改的脏值），这里统一收口成强类型对象：
类型转换失败一律回落到默认值，绝不让配置问题在业务层炸成运行时异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .constants import (
    COVER_CENSOR_STYLES,
    DEFAULT_COVER_CAPTION,
    DEFAULT_COVER_CENSOR,
    DEFAULT_COVER_CENSOR_BLOCK,
    DEFAULT_COVER_CENSOR_PADDING,
    DEFAULT_COVER_CENSOR_STYLE,
    DEFAULT_COVER_FULL_CENSOR_AREA,
    DEFAULT_COVER_RETRY_STRONG,
    DEFAULT_COVER_SEND,
    DEFAULT_DOWNLOAD_TIMEOUT,
    DEFAULT_FILENAME_TEMPLATE,
    DEFAULT_GROUP_FILE_FOLDER,
    DEFAULT_MAX_CONCURRENT_TASKS,
    DEFAULT_NSFW_AUTO_DOWNLOAD,
    DEFAULT_NSFW_IMGSZ,
    DEFAULT_NSFW_MODEL_PRESET,
    DEFAULT_NSFW_THRESHOLD,
    DEFAULT_PACK_FORMAT,
    DEFAULT_PROBE_CACHE_TTL,
    DEFAULT_PROBE_ENABLED,
    DEFAULT_PROBE_MIN_SPEED,
    DEFAULT_PROBE_TIMEOUT,
    DEFAULT_PROGRESS_STYLE,
    DEFAULT_RESUME_TTL,
    DEFAULT_RETRY_TIMES,
    DEFAULT_SEARCH_AGGREGATE_LIMIT,
    DEFAULT_SEARCH_AUTO_PROXY,
    DEFAULT_SEARCH_AUTO_PROXY_TIMEOUT,
    DEFAULT_SEARCH_CACHE_TTL,
    DEFAULT_SEARCH_COOLDOWN,
    DEFAULT_SEARCH_ENABLED,
    DEFAULT_SEARCH_PAGE_SIZE,
    DEFAULT_SEARCH_TIMEOUT,
    DEFAULT_SEGMENT_TIMEOUT,
    FILENAME_TEMPLATES,
    NSFW_IMGSZ_RANGE,
    NSFW_MODEL_PRESETS,
    PACK_FORMATS,
    PROGRESS_STYLES,
)

# template_list 里每条渠道配置都带这个 key，标记它属于哪个渠道模板
TEMPLATE_KEY = "__template_key"

# 渠道内部名改过之后的老键：存量配置里仍是旧名，读的时候映射到现名，避免用户重配一遍
LEGACY_CHANNEL_KEYS = {"pica": "picacg"}


def _as_int(raw: Any, default: int, minimum: int | None = None) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def _as_bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def _as_float(
    raw: Any, default: float, minimum: float | None = None, maximum: float | None = None
) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def _as_str(raw: Any, default: str = "") -> str:
    return raw.strip() if isinstance(raw, str) else default


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name)
    return value if isinstance(value, Mapping) else {}


def _as_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


@dataclass(slots=True)
class PluginSettings:
    """全局设置。渠道私有配置留在 channels 里，由各渠道自行解释。"""

    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    debounce_interval: int = 60
    download_dir: str = "downloads"
    pack_format: str = DEFAULT_PACK_FORMAT
    pack_password: str = ""
    filename_template: str = DEFAULT_FILENAME_TEMPLATE
    filename_custom: str = ""
    auto_delete_after_send: bool = True
    upload_to_group_file: bool = True
    group_file_folder: str = DEFAULT_GROUP_FILE_FOLDER
    group_file_create_folder: bool = True
    progress_style: str = DEFAULT_PROGRESS_STYLE
    show_result_summary: bool = True
    clean_temp_on_start: bool = True
    max_concurrent_tasks: int = DEFAULT_MAX_CONCURRENT_TASKS
    download_timeout: int = DEFAULT_DOWNLOAD_TIMEOUT
    download_retry_times: int = DEFAULT_RETRY_TIMES
    segment_timeout: int = DEFAULT_SEGMENT_TIMEOUT
    resume_ttl: int = DEFAULT_RESUME_TTL
    # 下载前测速（探测）：独立成一组，四项都可调
    probe_enabled: bool = DEFAULT_PROBE_ENABLED
    probe_timeout: int = DEFAULT_PROBE_TIMEOUT
    probe_min_speed: int = DEFAULT_PROBE_MIN_SPEED
    probe_cache_ttl: int = DEFAULT_PROBE_CACHE_TTL
    # 搜索：独立成一组；超时是聚合搜索不被单个慢渠道拖住的关键
    search_enabled: bool = DEFAULT_SEARCH_ENABLED
    search_timeout: int = DEFAULT_SEARCH_TIMEOUT
    search_page_size: int = DEFAULT_SEARCH_PAGE_SIZE
    search_aggregate_limit: int = DEFAULT_SEARCH_AGGREGATE_LIMIT
    search_cache_ttl: int = DEFAULT_SEARCH_CACHE_TTL
    search_cooldown: int = DEFAULT_SEARCH_COOLDOWN
    search_auto_proxy: bool = DEFAULT_SEARCH_AUTO_PROXY
    search_auto_proxy_timeout: int = DEFAULT_SEARCH_AUTO_PROXY_TIMEOUT
    # 封面：下载完成后单独发一张图，打码复用 NSFW 检测框；
    # 封面说明跟随「发送下载完成摘要」，不另设开关
    cover_send: bool = DEFAULT_COVER_SEND
    cover_caption: bool = DEFAULT_COVER_CAPTION
    cover_censor: bool = DEFAULT_COVER_CENSOR
    cover_censor_style: str = DEFAULT_COVER_CENSOR_STYLE
    cover_censor_padding: int = DEFAULT_COVER_CENSOR_PADDING
    cover_censor_block: int = DEFAULT_COVER_CENSOR_BLOCK
    cover_full_censor_area: float = DEFAULT_COVER_FULL_CENSOR_AREA
    cover_retry_strong: bool = DEFAULT_COVER_RETRY_STRONG
    # NSFW 模型与评分：权重默认不随插件分发，由开关按需下载
    nsfw_threshold: float = DEFAULT_NSFW_THRESHOLD
    nsfw_imgsz: int = DEFAULT_NSFW_IMGSZ
    nsfw_model_preset: str = DEFAULT_NSFW_MODEL_PRESET
    nsfw_auto_download: bool = DEFAULT_NSFW_AUTO_DOWNLOAD
    nsfw_model_url: str = ""
    proxy: str = ""
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> PluginSettings:
        data: Mapping[str, Any] = config or {}
        packing = _section(data, "packing")
        download = _section(data, "download")
        access = _section(data, "access")
        probe = _section(data, "probe")
        search = _section(data, "search")
        cover = _section(data, "cover")
        # 打码与模型各嵌在「封面与模型」的一个子块里，与封面共用一张卡片；
        # 旧配置里 censor 是个 bool（打码还没分组），非 Mapping 一律按默认值兜底
        censor = cover.get("censor")
        censor = censor if isinstance(censor, Mapping) else {}
        model = cover.get("model")
        model = model if isinstance(model, Mapping) else {}

        pack_format = _as_str(
            packing.get("pack_format"), DEFAULT_PACK_FORMAT
        )
        if pack_format not in PACK_FORMATS:
            pack_format = DEFAULT_PACK_FORMAT
        progress_style = _as_str(
            download.get("progress_style"), DEFAULT_PROGRESS_STYLE
        )
        if progress_style not in PROGRESS_STYLES:
            progress_style = DEFAULT_PROGRESS_STYLE
        censor_style = _as_str(
            censor.get("style"), DEFAULT_COVER_CENSOR_STYLE
        )
        if censor_style not in COVER_CENSOR_STYLES:
            censor_style = DEFAULT_COVER_CENSOR_STYLE
        model_preset = _as_str(
            model.get("preset"), DEFAULT_NSFW_MODEL_PRESET
        ).lower()
        if model_preset not in NSFW_MODEL_PRESETS:
            model_preset = DEFAULT_NSFW_MODEL_PRESET

        filename_template = _as_str(
            packing.get("filename_template"), DEFAULT_FILENAME_TEMPLATE
        )
        if filename_template not in FILENAME_TEMPLATES:
            filename_template = DEFAULT_FILENAME_TEMPLATE

        return cls(
            whitelist=_as_str_list(access.get("whitelist")),
            blacklist=_as_str_list(access.get("blacklist")),
            debounce_interval=_as_int(
                access.get("debounce_interval"), 60, minimum=0
            ),
            download_dir=_as_str(
                packing.get("download_dir"), "downloads"
            )
            or "downloads",
            pack_format=pack_format,
            pack_password=_as_str(packing.get("pack_password")),
            filename_template=filename_template,
            filename_custom=_as_str(packing.get("filename_custom")),
            auto_delete_after_send=_as_bool(
                packing.get("auto_delete_after_send"), True
            ),
            upload_to_group_file=_as_bool(
                packing.get("upload_to_group_file"), True
            ),
            group_file_folder=_as_str(
                packing.get("group_file_folder"), DEFAULT_GROUP_FILE_FOLDER
            ),
            group_file_create_folder=_as_bool(
                packing.get("group_file_create_folder"), True
            ),
            progress_style=progress_style,
            show_result_summary=_as_bool(
                download.get("show_result_summary"), True
            ),
            clean_temp_on_start=_as_bool(
                packing.get("clean_temp_on_start"), True
            ),
            max_concurrent_tasks=_as_int(
                download.get("max_concurrent_tasks"),
                DEFAULT_MAX_CONCURRENT_TASKS,
                minimum=1,
            ),
            download_timeout=_as_int(
                download.get("timeout"),
                DEFAULT_DOWNLOAD_TIMEOUT,
                minimum=60,
            ),
            download_retry_times=_as_int(
                download.get("retry_times"),
                DEFAULT_RETRY_TIMES,
                minimum=0,
            ),
            segment_timeout=_as_int(
                download.get("segment_timeout"), DEFAULT_SEGMENT_TIMEOUT, minimum=0
            ),
            resume_ttl=_as_int(
                download.get("resume_ttl"), DEFAULT_RESUME_TTL, minimum=0
            ),
            # 「下载前测速」搬进 probe 分组；老配置的 download.domain_race 继续认，
            # 否则升级后用户原本关掉的测速会悄悄变回开启
            probe_enabled=_as_bool(
                probe.get("enable"),
                _as_bool(download.get("domain_race"), DEFAULT_PROBE_ENABLED),
            ),
            probe_timeout=_as_int(
                probe.get("timeout"), DEFAULT_PROBE_TIMEOUT, minimum=1
            ),
            probe_min_speed=_as_int(
                probe.get("min_speed"), DEFAULT_PROBE_MIN_SPEED, minimum=0
            ),
            probe_cache_ttl=_as_int(
                probe.get("cache_ttl"), DEFAULT_PROBE_CACHE_TTL, minimum=0
            ),
            search_enabled=_as_bool(search.get("enable"), DEFAULT_SEARCH_ENABLED),
            # 超时给下限 1 秒、上限 120 秒：配成 0 会让每次搜索立刻判超时
            search_timeout=min(
                _as_int(search.get("timeout"), DEFAULT_SEARCH_TIMEOUT, minimum=1), 120
            ),
            search_page_size=min(
                _as_int(search.get("page_size"), DEFAULT_SEARCH_PAGE_SIZE, minimum=1),
                20,
            ),
            search_aggregate_limit=min(
                _as_int(
                    search.get("aggregate_limit"),
                    DEFAULT_SEARCH_AGGREGATE_LIMIT,
                    minimum=1,
                ),
                30,
            ),
            search_cache_ttl=_as_int(
                search.get("cache_ttl"), DEFAULT_SEARCH_CACHE_TTL, minimum=0
            ),
            search_cooldown=_as_int(
                search.get("cooldown"), DEFAULT_SEARCH_COOLDOWN, minimum=0
            ),
            search_auto_proxy=_as_bool(
                search.get("auto_proxy"), DEFAULT_SEARCH_AUTO_PROXY
            ),
            # 自动代理重试是额外预算，给 1–120 秒；配成 0 会让重试立刻判超时
            search_auto_proxy_timeout=min(
                _as_int(
                    search.get("auto_proxy_timeout"),
                    DEFAULT_SEARCH_AUTO_PROXY_TIMEOUT,
                    minimum=1,
                ),
                120,
            ),
            cover_send=_as_bool(cover.get("send"), DEFAULT_COVER_SEND),
            cover_caption=_as_bool(cover.get("caption"), DEFAULT_COVER_CAPTION),
            cover_censor=_as_bool(censor.get("enable"), DEFAULT_COVER_CENSOR),
            cover_censor_style=censor_style,
            cover_censor_padding=_as_int(
                censor.get("padding"), DEFAULT_COVER_CENSOR_PADDING, minimum=0
            ),
            cover_censor_block=_as_int(
                censor.get("block"), DEFAULT_COVER_CENSOR_BLOCK, minimum=2
            ),
            cover_full_censor_area=_as_float(
                censor.get("full_area"),
                DEFAULT_COVER_FULL_CENSOR_AREA,
                minimum=0.0,
                maximum=1.0,
            ),
            cover_retry_strong=_as_bool(
                censor.get("retry_strong"), DEFAULT_COVER_RETRY_STRONG
            ),
            nsfw_threshold=_as_float(
                model.get("threshold"), DEFAULT_NSFW_THRESHOLD, minimum=0.01, maximum=0.99
            ),
            nsfw_imgsz=min(
                max(
                    _as_int(model.get("imgsz"), DEFAULT_NSFW_IMGSZ),
                    NSFW_IMGSZ_RANGE[0],
                ),
                NSFW_IMGSZ_RANGE[1],
            ),
            nsfw_model_preset=model_preset,
            nsfw_auto_download=_as_bool(
                model.get("auto_download"), DEFAULT_NSFW_AUTO_DOWNLOAD
            ),
            nsfw_model_url=_as_str(model.get("download_url")),
            proxy=_as_str(download.get("proxy")),
            channels=_parse_channels(data.get("channels")),
        )

    def channel_options(self, channel_name: str) -> dict[str, Any] | None:
        """取出某渠道的模板配置；返回 None 表示用户没有添加该渠道。"""
        return self.channels.get(channel_name)

    @property
    def enabled_channel_names(self) -> list[str]:
        return [
            name
            for name, options in self.channels.items()
            if _as_bool(options.get("enable"), True)
        ]


def _parse_channels(raw: Any) -> dict[str, dict[str, Any]]:
    """把 template_list 的条目收敛成 {渠道名: 条目}。

    同一渠道被添加多次时后者覆盖前者——WebUI 里靠前的条目通常是用户已经废弃的旧配置。
    """
    channels: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, list):
        return channels
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _as_str(item.get(TEMPLATE_KEY))
        if not name:
            continue
        channels[LEGACY_CHANNEL_KEYS.get(name, name)] = item
    return channels

"""渠道抽象。

一个渠道只负责三件事：把用户输入收敛成本渠道的漫画 ID、取元信息、把图片下载到本地目录。
**打包、加密、发送都在上层统一处理**，渠道不参与，新增渠道时也不该重复实现。

搜索是**可选能力**：默认实现抛 `SearchUnsupportedError`，支持的渠道覆盖 `search()` 即可，
不影响「新增渠道六处接线」的既有约定，也不会让未实现的渠道无法实例化。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, ClassVar, Final

from astrbot.api import logger

from ..domain import (
    PLUGIN_NAME,
    ChapterEntry,
    ChapterError,
    ChapterSpec,
    ChannelUnavailableError,
    ComicMeta,
    DownloadError,
    DownloadedComic,
    PluginSettings,
    RankingPage,
    RankingUnsupportedError,
    SearchItem,
    SearchPage,
    SearchQuery,
    SearchUnsupportedError,
)
from ..domain.constants import DRAIN_TIMEOUT
from ..domain.models import ChannelSelfcheck
from .estimate import http_size
from .domain_race import (
    CACHE,
    DomainTiming,
    ProbeFn,
    order_domains,
    probe_url,
    race_domains,
    summarize,
)
from .progress import ProgressState

# 进度回调：每次都交一份完整快照，命令层据此决定要不要往聊天里发
ProgressCallback = Callable[[ProgressState], Awaitable[None]]


# 平台自检的探测面：拿一个常见浏览器 UA 打站点首页。样本只要够判「通不通」，
# 不必读完——自检要快，用户等着结论
SELFCHECK_USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
SELFCHECK_SAMPLE_BYTES: Final = 16 * 1024

# 中断原因：命令层的文案必须区分「用户自己按停」与「插件被重载打断」——前者断点一定还在，
# 后者要看重载时会不会清空临时目录，照抄暂停文案会把「能接着下」说成假话
INTERRUPT_USER = "user"
INTERRUPT_RELOAD = "reload"


class CancelToken:
    """一次任务独享的取消信号。

    跑在线程池里的下载（JM 用 jmcomic）不会被 asyncio 取消打断：整本超时后线程还在
    默默下完、白占带宽，结果也无处投递。任务层放弃这次下载时置位令牌，渠道在自己的
    检查点收工——和「插件重载时取消」用的是同一套机制，只是范围缩到这一次任务。
    """

    def __init__(self) -> None:
        self.event = threading.Event()
        self.reason = INTERRUPT_USER
        # 重载时临时目录随后会被清掉，任务层事后数不出下了多少，只能由取消方在那一刻记下
        self.state: ProgressState | None = None

    def cancel(
        self, reason: str = INTERRUPT_USER, state: ProgressState | None = None
    ) -> None:
        """置位取消；先到的那次说了算，免得超时/重载互相覆盖掉真实原因。"""
        if not self.event.is_set():
            self.reason = reason
            self.state = state
        self.event.set()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()


# 代理来源：日志与提示里要能看出「这一趟走的是哪条路」，取值本身就是对用户展示的文案
PROXY_SOURCE_PLUGIN = "代理"
PROXY_SOURCE_ASTRBOT = "宿主代理"
PROXY_SOURCE_DIRECT = "直连"

# 搜索超时后的代理重试：只影响当前异步任务（含其 to_thread 子上下文），
# 不会把并发下载的代理设置一起改掉。
_SEARCH_PROXY_OVERRIDE: ContextVar[str | None] = ContextVar(
    "search_proxy_override", default=None
)


@contextmanager
def search_proxy_override(proxy: str) -> Iterator[None]:
    """在块内把渠道的代理解析强制指向 ``proxy``，用于搜索超时后的自动代理重试。"""
    token = _SEARCH_PROXY_OVERRIDE.set(proxy)
    try:
        yield
    finally:
        _SEARCH_PROXY_OVERRIDE.reset(token)


def is_search_proxy_override() -> bool:
    """当前是否处于「搜索超时后的自动代理重试」中。"""
    return _SEARCH_PROXY_OVERRIDE.get() is not None


def split_option_list(raw: Any) -> list[str]:
    """把「多行文本 / 逗号空格分隔的字符串 / list」拆成列表，保持填写顺序。

    域名这类配置的顺序就是降级顺序，因此不排序、不去重。
    """
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        parts = raw.replace(",", " ").replace("\r", " ").split("\n")
        return [token for part in parts for token in part.split() if token]
    return []


class BaseChannel(ABC):
    """渠道基类。子类只需声明类属性并实现四个抽象方法。"""

    name: ClassVar[str]  # 内部名，与 _conf_schema.json 的模板 key 一致
    command: ClassVar[str]  # 注册给指令系统的全称（站点名，如 jmcomic / nhentai）
    # 缩写别名：用户日常敲的是这些；第一个别名同时是提示文案里优先展示的命令
    command_aliases: ClassVar[tuple[str, ...]] = ()
    display_name: ClassVar[str]  # 帮助文案里的展示名
    # 站点主页（无尾斜杠）：`/comic site` 指引用户去站内搜作品、找作品 ID
    website: ClassVar[str] = ""
    # 作品页链接示意：真实域名 + `<ID>` 占位（不放具体数字）。
    # `<ID>` 不是可点击的地址，也不会拼出点不开的假链接，只是告诉用户作品 ID 在链接里的位置。
    link_template: ClassVar[str] = ""
    # 作品页链接里 ID 的填法说明：不写就按默认的「<ID> 处替换成作品 ID」展示。
    # 两段式 ID（如 E-Hentai 的 gid+token）在这里说明每一段填什么。
    link_hint: ClassVar[str] = ""
    requires: ClassVar[tuple[str, ...]] = ()  # 运行时依赖的模块名
    # 「使用代理」开关在配置里缺失时的默认值：面向境外站点的渠道默认开启
    default_use_proxy: ClassVar[bool] = False
    # 搜索超时时追加给用户的排查提示（渠道私有，不知道就留空）
    search_timeout_hint: ClassVar[str] = ""
    # 支持的榜单窗口（见 domain/constants.py 的 RANKING_BOARDS）；空元组表示本渠道没有榜单
    ranking_boards: ClassVar[tuple[str, ...]] = ()
    # 章节单位（如「章」）；空串 = 本渠道的作品不分章节，整本就是一份图。
    # 连载站（JM / 哔咔 / 绅士漫画合辑）填上它并实现 fetch_chapters，才能按章节下载
    chapter_unit: ClassVar[str] = ""

    def __init__(self, options: Mapping[str, Any], settings: PluginSettings) -> None:
        self.options: dict[str, Any] = dict(options)
        self.settings = settings
        # 上一次 fetch_cover 的失败原因：面板「从渠道拉封面」拿它告诉用户为什么没取到图。
        # 各渠道在自己的 fetch_cover 里置位，空串表示「没补充说明」，面板会退回通用文案
        self.cover_error: str = ""

    # ---- 命令名 ----
    @property
    def display_command(self) -> str:
        """提示文案里优先展示的命令：缩写别名排在前面，所以用户看到的是更短的那个。"""
        return self.command_aliases[0] if self.command_aliases else self.command

    @property
    def command_names(self) -> tuple[str, ...]:
        """全称在前、缩写在后，重复与空别名剔掉。

        注册指令与「/命令 → 渠道」映射都用这一份清单，避免两处各写一遍导致别名只在其中一边生效。
        """
        names = [self.command]
        for alias in self.command_aliases:
            if alias and alias not in names:
                names.append(alias)
        return tuple(names)

    # ---- ID 展示：文件名模板里的 `{id_short}` 用 ----
    @classmethod
    def short_id(cls, comic_id: str) -> str:
        """`{id_short}` 占位符的取值：默认原样返回，ID 本身够短的渠道无需覆盖。

        只有 ID 里带冗余段的渠道才覆盖（E-Hentai 的 token、哔咔 ObjectId 的中段）。
        简写只用于文件名展示，不参与下载、缓存与断点目录。
        """
        return comic_id

    # ---- 配置读取：渠道私有配置项一律走这三个方法，缺失/脏值自动兜底 ----
    @property
    def enabled(self) -> bool:
        return self.opt_bool("enable", True)

    def opt_bool(self, key: str, default: bool) -> bool:
        value = self.options.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
        return default

    def opt_int(self, key: str, default: int, minimum: int | None = None) -> int:
        try:
            value = int(self.options.get(key))
        except (TypeError, ValueError):
            return default
        if minimum is not None and value < minimum:
            return default
        return value

    def opt_str(self, key: str, default: str = "") -> str:
        value = self.options.get(key)
        return value.strip() if isinstance(value, str) else default

    def opt_list(self, key: str) -> list[str]:
        """兼容「多行文本 / 逗号空格分隔的字符串」与「真正的 list」三种填法。"""
        return split_option_list(self.options.get(key))

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        """渠道没被配置过时使用的默认项（内置渠道因此开箱即启用）。"""
        return {}

    # ---- 代理：渠道开关 → 插件代理 → AstrBot 本体代理（环境变量） ----
    def resolve_proxy(self, *, default_enabled: bool | None = None) -> tuple[str, str]:
        """解析本渠道这一趟要走的代理，返回（代理串，来源）。

        - 渠道「使用代理」关掉 → 直连，不看任何环境变量
        - 插件配置了全局代理 → 用它
        - 插件没配 → 用 AstrBot 本体代理（AstrBot 把它写在 `http_proxy`/`https_proxy`
          环境变量里），这样本体配过代理的人不用在插件里再填一遍
        - 都没有 → 直连
        """
        override = _SEARCH_PROXY_OVERRIDE.get()
        if override:
            return override, PROXY_SOURCE_PLUGIN
        if default_enabled is None:
            default_enabled = self.default_use_proxy
        if not self.opt_bool("use_proxy", default_enabled):
            return "", PROXY_SOURCE_DIRECT
        if self.settings.proxy:
            return self.settings.proxy, PROXY_SOURCE_PLUGIN
        for key in ("https_proxy", "http_proxy", "HTTPS_PROXY", "HTTP_PROXY"):
            value = os.environ.get(key, "").strip()
            if value:
                return value, PROXY_SOURCE_ASTRBOT
        return "", PROXY_SOURCE_DIRECT

    def proxy_summary(self, *, default_enabled: bool | None = None) -> str:
        """代理的「人话」描述，供日志与用户提示用：只说这一趟走哪条路。

        **不回显代理地址**：帮助文案会发到群里，而代理串里常带内网主机与账号密码；
        要确认具体用哪个代理时直接看配置即可。
        """
        proxy, source = self.resolve_proxy(default_enabled=default_enabled)
        return source if proxy else PROXY_SOURCE_DIRECT

    # ---- 下载前测速择优：候选并发探测，按实测速度排序 ----
    async def ordered_domains(
        self,
        candidates: Sequence[str],
        probe: ProbeFn,
        *,
        label: str,
    ) -> list[str] | None:
        """给候选域名/图床排序：最快的排第一，其余按实测速度与响应时间往后排。

        返回 None 表示「这一轮没拿到任何有效排名」——测速被关闭、只有一个候选、
        或探测全灭（网络/探测面问题）。调用方据此退回自己的老策略：按填写顺序降级，
        或干脆交给第三方库自带的域名逻辑。**测速失败永远不该阻断下载。**
        ``label`` 只用于日志，说明测的是站点域名还是图床。
        """
        domains = [item for item in candidates if item]
        if not self.settings.probe_enabled or len(domains) < 2:
            return None

        key = f"{self.name}:{label}:{'|'.join(domains)}"
        cached = CACHE.get(key, self.settings.probe_cache_ttl)
        if cached is not None:
            # 缓存只负责「谁快」；清单本身始终以当前配置为准
            return [item for item in cached if item in domains] + [
                item for item in domains if item not in cached
            ]

        try:
            timings = await race_domains(
                domains, probe, timeout=float(self.settings.probe_timeout)
            )
        except Exception as exc:  # noqa: BLE001 - 测速故障必须降级成「按配置顺序」
            logger.warning(
                f"[{PLUGIN_NAME}] {self.display_name} {label}测速失败，按配置顺序降级：{exc}"
            )
            return None

        if not any(timing.ok for timing in timings):
            logger.warning(
                f"[{PLUGIN_NAME}] {self.display_name} {label}全部探测失败：{summarize(timings)}"
            )
            return None

        ranked = order_domains(timings, min_speed_kbps=self.settings.probe_min_speed)
        CACHE.put(key, ranked)
        logger.info(
            f"[{PLUGIN_NAME}] {self.display_name} {label}测速：{summarize(timings)}"
            f" → 选用 {ranked[0]}"
        )
        return ranked

    def missing_requirement(self) -> str | None:
        """返回首个缺失的运行时依赖名；依赖齐备时返回 None。"""
        for module in self.requires:
            try:
                found = importlib.util.find_spec(module) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                return module
        return None

    def ensure_available(self) -> None:
        """命令入口的第一道闸：未启用或依赖缺失时，用面向用户的文案拒绝。"""
        if not self.enabled:
            # 内置渠道默认就在配置里，说「添加」会让人去找一个不存在的入口
            raise ChannelUnavailableError(
                f"{self.display_name} 渠道还没启用：让管理员在插件配置里打开它"
            )
        missing = self.missing_requirement()
        if missing is not None:
            # 缺哪个库只有管理员能修，用户侧不必看到模块名
            logger.warning(f"[{PLUGIN_NAME}] {self.display_name} 缺少依赖：{missing}")
            raise ChannelUnavailableError(
                f"{self.display_name} 缺少运行依赖，暂时用不了，请联系管理员安装后重载插件"
            )

    # ---- 搜索：可选能力，默认不支持 ----
    @property
    def supports_cover(self) -> bool:
        """能不能只取一张封面（不下载整本）。面板的「从渠道拉封面」按它决定是否可选。"""
        return type(self).fetch_cover is not BaseChannel.fetch_cover

    async def fetch_cover(self, comic_id: str) -> bytes | None:
        """只取封面图；默认不支持，渠道各自覆盖。

        返回原始图片字节（jpg/png/webp 都行），取不到返回 ``None``。面板会把它存进临时表
        直接拿去评分/打码，因此这里**不做任何落盘**。

        取不到时把自己那边的失败原因写进 ``self.cover_error``：面板没有别的渠道能知道
        「是域名没了、还是被风控」，笼统报一句「没取到」用户没法判断要不要开代理。
        """
        return None

    @property
    def supports_search(self) -> bool:
        """是否实现了搜索。

        用「是否覆盖了基类方法」判定，而不是另加一个手填的类属性：渠道实现了 `search()`
        却忘了打开开关这种不一致，在运行时无法自愈，直接由代码结构决定最稳。
        """
        return type(self).search is not BaseChannel.search

    async def search(
        self, query: SearchQuery, limit: int = 5, page: int = 1
    ) -> SearchPage:
        """按结构化的 ``query`` 搜索本渠道第 ``page`` 页（从 1 开始），返回至多 ``limit`` 条。

        默认抛 `SearchUnsupportedError`：未实现搜索的渠道仍然能正常下载，也不需要写 stub。
        支持筛选的渠道把条件翻译成上游语法；不支持的渠道至少要把正向条件的值当关键词搜，
        并在 `SearchPage.note` 里说明降级方式，避免用户误以为做了精确筛选。
        """
        raise SearchUnsupportedError(f"{self.display_name} 暂不支持搜索")

    # ---- 搜索结果补页数：可选能力，默认不补 ----
    @property
    def supports_page_count(self) -> bool:
        """搜索结果能否补出页数。

        有些站点的搜索接口只回标题（JM、Hanime1），列表里于是有的条目带页数、有的不带。
        实现了 `fill_page_counts()` 的渠道会由搜索服务在展示前补一次；没实现的渠道
        保持原样——它们本来就在搜索结果里给了页数，或者上游根本不提供。
        """
        return type(self).fill_page_counts is not BaseChannel.fill_page_counts

    async def fill_page_counts(self, items: list[SearchItem]) -> None:
        """就地给搜索结果补页数；补不到的条目保持 ``page_count == 0``。

        默认什么都不做。实现方要自己控制成本：只处理缺页数的条目、并发有限、
        整轮有总时限，且**任何失败都只让那一行少一个页数**，不能把搜索本身搞失败。
        """

    # ---- 抽样估体积：可选能力，默认不抽样 ----
    @property
    def supports_size_sample(self) -> bool:
        """能否为「下载前体积估算」抽样量单张体积。

        覆盖了 `sample_image_urls` 的渠道才会在打开「抽样实测体积」时真的下一两张图；
        没覆盖的渠道只按「页数 × 单张体积」估——它们的图址要么得先翻整本才拿得到，
        要么站点本身给了真实体积（如 E-Hentai）。
        """
        return type(self).sample_image_urls is not BaseChannel.sample_image_urls

    async def sample_image_urls(self, comic_id: str) -> list[str]:
        """取这本作品前几张图的地址，供抽样量体积；拿不到时返回空列表。

        默认返回空列表（= 不抽样）。实现方只该做「拿到前 N 张地址」这一件事：
        不要顺手翻完整本，也不要下载图片——量体积由上层统一做。
        """
        return []

    async def sample_image_size(self, url: str) -> int:
        """量一张图有多少字节；默认走通用的 HEAD / Range 问法。

        渠道的图床需要特殊请求头（Referer、UA）时覆盖它，把同一套头带过去。
        """
        return await http_size(url, proxy=self.resolve_proxy()[0])

    # ---- 排行榜：可选能力，默认不支持 ----
    @property
    def supports_ranking(self) -> bool:
        """是否实现了排行榜。

        除了覆盖 `ranking()`，还要求 `ranking_boards` 非空：渠道可能只想开放自己有的
        那几档（哔咔没有总榜、Hanime1 没有月榜），空元组等于「这个渠道没有榜单」。
        """
        if not self.ranking_boards:
            return False
        return type(self).ranking is not BaseChannel.ranking

    async def ranking(
        self, board: str, page: int = 1, limit: int = 10, offset: int = 0
    ) -> RankingPage:
        """取本渠道 ``board`` 时间窗第 ``page`` 页里、从第 ``offset`` 条起的至多 ``limit`` 条候选。

        返回的是**未按尺寸上限筛过**的候选（带 ``page_count`` / ``chapter_count``）：
        过滤与分页由榜单服务在本地缓冲里做，渠道这一层只管按名次顺序交数据。
        ``exhausted`` 说明这一上游页的候选是不是给完了（给完了下次要翻页）。

        ``board`` 只可能是 ``ranking_boards`` 里声明过的窗口。默认抛
        `RankingUnsupportedError`：没有榜单的渠道照常能下载与搜索，不需要写 stub。
        """
        raise RankingUnsupportedError(f"{self.display_name} 暂不支持排行榜")

    # ---- 章节：可选能力，默认不支持 ----
    @property
    def supports_chapters(self) -> bool:
        """能不能按章节下载。

        与搜索一样用「是否覆盖了基类方法」判定：只声明 ``chapter_unit`` 而不实现
        ``fetch_chapters`` 的渠道拿不到章节清单，等于没法按章节选，直接算不支持。
        """
        if not self.chapter_unit:
            return False
        return type(self).fetch_chapters is not BaseChannel.fetch_chapters

    async def fetch_chapters(self, comic_id: str) -> list[ChapterEntry]:
        """列出这本作品的章节清单（1 基序号 + 标题 + 页数），供「章节」命令展示与选择。

        ``ChapterEntry.index`` 必须从 1 连续编号：用户写「章节 3」时指的就是清单里的第 3 条，
        渠道站点自己的 order 有跳号时由渠道换算。默认抛错，不分章节的渠道不必写 stub。
        """
        raise ChapterError(f"{self.display_name} 不分章节，没有章节清单可看")

    # ---- 子类必须实现 ----
    @abstractmethod
    def normalize_id(self, raw: str) -> str:
        """把用户输入（纯 ID / 链接 / 分享文本）收敛成本渠道的漫画 ID。"""

    @abstractmethod
    def match_link(self, text: str) -> str | None:
        """从完整作品链接里取出本渠道的作品 ID；不是本渠道的链接时返回 None。

        `/comic` 靠它把链接路由到渠道，所以只认带主机名的 http(s) 地址：
        裸 ID、站名式的分享文本一律不认，那些走各渠道自己的命令。
        没有网页作品页的渠道直接 `return None`——不做这个判断就无法参与链接路由。
        """

    @abstractmethod
    async def fetch_meta(self, comic_id: str, work_dir: Path) -> ComicMeta:
        """取元信息。work_dir 是该任务专属的临时目录。"""

    @abstractmethod
    async def download(
        self,
        comic_id: str,
        work_dir: Path,
        on_progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
        chapters: ChapterSpec | None = None,
    ) -> DownloadedComic:
        """下载漫画到 work_dir（或它的子目录）并返回落盘结果。

        ``cancel`` 是本次任务的取消信号：任务超时或被放弃时置位，下载应在下一个检查点
        停手（线程池里的渠道尤其需要，asyncio 取消打不断它们）。

        ``chapters`` 为空表示整本。支持章节的渠道自己用 :func:`domain.select_entries`
        （或 ``ChapterSpec.indices``）在自己的章节清单上筛，并把实际挑中的范围写进
        ``DownloadedComic.chapters_label``；不分章节的渠道忽略它即可，越界报错由
        命令层在调用前挡住。
        """

    def cancel_downloads(self) -> list[str]:
        """插件卸载时取消进行中的下载，返回可写进日志的状态摘要。

        默认不做事；下载跑在线程里的渠道（如 JM）应覆盖它，避免重载后线程继续白跑。
        """
        return []

    async def drain(
        self, token: CancelToken | None = None, timeout: float = DRAIN_TIMEOUT
    ) -> bool:
        """取消之后等本渠道的后台下载真正停下；返回是否在 timeout 内停稳。

        默认实现认为没有后台线程（协程被打断就等于停了）。线程池里的渠道（如禁漫天堂）
        必须覆盖它：asyncio 的取消只终止协程，线程会继续往工作目录写图，任务层据此
        判断「已保存 N 张」能不能当成最终结果报给用户。
        """
        return True

    # ---- 平台自检：这个渠道现在能不能真的用 ----
    def _selfcheck_blocked(self, reason: str, hint: str) -> ChannelSelfcheck:
        """自检在发请求之前就被拦住（缺依赖 / 没启用）：直接给结论，别打站点。

        仍然带上线路：用户看到「未启用 · 代理」时能顺带确认代理配置本身是对的，
        不必等启用之后才发现填错了。
        """
        try:
            proxy, source = self.resolve_proxy()
        except Exception:  # noqa: BLE001 - 读线路失败不该让自检失败
            source = ""
        return ChannelSelfcheck(
            channel=self.name,
            display_command=self.display_command,
            enabled=self.enabled,
            proxy_source=source if proxy else source,
            reason=reason,
            hint=hint,
        )

    async def _selfcheck_first_reachable(
        self,
        urls: Sequence[str],
        *,
        proxy: str,
        headers: Mapping[str, str],
        reject_markers: Sequence[bytes] = (),
        started: float | None = None,
        timeout: float = 15.0,
    ) -> tuple[DomainTiming, str]:
        """按降级顺序逐个试候选地址，第一个通的就返回。

        为什么必须逐个试：站点常有多条域名，第一条打不开不代表渠道不能用——
        真实下载本来就会按这个顺序降级。只试第一条会把「换个域名就好」误报成「挂了」。

        每个候选都套自己的超时：`probe_url` 本身没有上限，一个死域名会把整次自检
        拖到 30 秒以上（实测过），用户等来的结论还是错的。

        Returns:
            ``(探测结果, 实际试到的那个 URL)``；全都不通时返回最后一个的结果。
        """
        started = time.monotonic() if started is None else started
        last: DomainTiming | None = None
        url = urls[0] if urls else ""
        for candidate in urls:
            url = candidate
            try:
                last = await asyncio.wait_for(
                    probe_url(
                        self.name,
                        candidate,
                        proxy=proxy,
                        headers=dict(headers),
                        sample_bytes=SELFCHECK_SAMPLE_BYTES,
                        reject_markers=reject_markers,
                    ),
                    timeout,
                )
            except asyncio.TimeoutError:
                last = DomainTiming(self.name, False, 0, 0.0, f"超过 {timeout:g}s")
            if last.ok:
                return last, candidate
        if last is None:
            last = DomainTiming(self.name, False, 0, 0.0, "没有可试的地址")
        return last, url

    def _selfcheck_result(
        self,
        timing: DomainTiming,
        url: str,
        proxy_source: str,
        started: float,
    ) -> ChannelSelfcheck:
        """把一次探测结果收成自检结论：各渠道的自检只负责挑探测面，其余共用这一份。"""
        return ChannelSelfcheck(
            channel=self.name,
            display_command=self.display_command,
            enabled=True,
            ok=timing.ok,
            latency_ms=timing.latency_ms,
            elapsed=time.monotonic() - started,
            url=url,
            proxy_source=proxy_source,
            reason="" if timing.ok else (timing.note or "连不上"),
        )

    async def selfcheck(self, timeout: float = 15.0) -> ChannelSelfcheck:
        """实际打一次本渠道的入口，回答「现在能不能用」。

        与 `fetch_meta` 的区别：这里**不需要作品 ID**，打的是站点入口（首页/接口探针），
        用来区分「站点连不上」「被风控拦了」「缺依赖/没启用」这几种完全不同的原因——
        用户报「下不动」时，先看清是哪一种才知道该开代理、换域名还是装依赖。

        默认实现打 `website` 首页；各渠道覆盖它换成更贴近真实用法的探针
        （例如 nhentai 打 API、哔咔打接口域名），并且一律用**本渠道自己的**
        域名清单与代理设置，与真正下载走同一条线路。

        Args:
            timeout: 单次请求上限（秒）。

        Returns:
            ChannelSelfcheck: ``ok`` 为 False 时 ``reason`` 是可直接展示的中文。
        """
        started = time.monotonic()
        missing = self.missing_requirement()
        if missing is not None:
            return ChannelSelfcheck(
                channel=self.name,
                display_command=self.display_command,
                enabled=self.enabled,
                reason=f"缺少运行依赖 {missing}",
                hint=f"pip install {missing}，装好后重载插件",
            )
        if not self.enabled:
            return ChannelSelfcheck(
                channel=self.name,
                display_command=self.display_command,
                enabled=False,
                reason="渠道未启用",
                hint="在插件配置里打开它",
            )

        url = f"{self.website.rstrip('/')}/" if self.website else ""
        if not url:
            return ChannelSelfcheck(
                channel=self.name,
                display_command=self.display_command,
                enabled=True,
                reason="不知道该访问哪个地址",
                hint="这个渠道没有声明 website",
            )

        proxy, proxy_source = self.resolve_proxy()
        timing = await probe_url(
            self.name,
            url,
            proxy=proxy,
            headers={"User-Agent": SELFCHECK_USER_AGENT, "Accept": "text/html,*/*"},
            sample_bytes=SELFCHECK_SAMPLE_BYTES,
        )
        elapsed = time.monotonic() - started
        return ChannelSelfcheck(
            channel=self.name,
            display_command=self.display_command,
            enabled=True,
            ok=timing.ok,
            latency_ms=timing.latency_ms,
            elapsed=elapsed,
            url=url,
            proxy_source=proxy_source,
            reason="" if timing.ok else (timing.note or "连不上"),
        )

    def wrap_failure(self, exc: Exception) -> DownloadError:
        """把渠道内部异常翻译成面向用户的下载失败文案。

        原始异常（英文、带域名与状态码）只进日志：群成员读到它也做不了什么，
        真正能行动的是「换代理 / 换域名 / 过一会儿再试」这三句。
        """
        logger.warning(f"[{PLUGIN_NAME}] {self.display_name} 下载失败：{exc}")
        return DownloadError(
            f"{self.display_name} 下载失败：多半是图床限流或网络不通，"
            "开启该渠道的代理后重发一次命令；一直失败请检查站点域名是否还有效"
        )

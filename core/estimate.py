"""下载前的整本体积估算：真实值优先，拿不到就按「页数 × 单张体积」估。

各站点给的东西不一样：E-Hentai 的 `gdata` 直接给 `filesize`（真值），其余五个渠道的
搜索/详情接口都只有页数，**没有任何体积字段**。所以这里的策略分三层：

1. **真实值**：渠道自己在 `ComicMeta.size_bytes` 里填了就用它，原样展示、不加「约」；
2. **抽样实测**（配置项 `estimate_sample_enabled`）：真下前几张图，用平均单张体积乘页数，
   比固定值准，代价是多下一两张（有总时限，超时就退回第 3 层）；
3. **固定值**：`页数 × 单张体积`，单张体积按渠道给默认值（配置项 `estimate_image_kb` 可覆盖）。

**为什么值得做**：用户在「简介」里看不到大小，就没法判断这一本值不值得下——尤其手机流量
或小磁盘的部署。估算误差两三成可以接受，因为它的用途是「量级判断」，不是精确账单。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from astrbot.api import logger

from ..domain import PLUGIN_NAME, ComicMeta, PluginSettings
from ..domain.constants import (
    DEFAULT_ESTIMATE_IMAGE_KB,
    ESTIMATE_IMAGE_KB_BY_CHANNEL,
)
from ..utils import format_size

# 抽样实测的整轮总时限（秒）：估算只是「顺手给个参考」，不能为它多等一整个下载超时
SAMPLE_BUDGET: float = 6.0
# 单张取体积的超时（秒）：只要响应头，不该比一张图真的下完还慢
SIZE_TIMEOUT: float = 8.0

# 取前 N 张图地址的回调：渠道实现，返回可供抽样的图片地址（按页序，从第 1 张起）。
SampleUrlsFn = Callable[[str], Awaitable[list[str]]]
# 取一个地址的字节数：由调用方注入（渠道各自的下载器/抓页器），便于离线自测替身
SizeFn = Callable[[str], Awaitable[int]]


def per_image_kb(channel: str, settings: PluginSettings) -> int:
    """这个渠道的单张体积基准（KB）。

    配置项是「一个数字管所有渠道」的粗调，但各站清晰度差得很远（JM 的长图约 260 KB，
    E-Hentai 原图 5.4 MB），统一用一个数会让一半渠道错得离谱。所以：

    - 用户把配置改成**非默认值** → 那是他的显式选择，一律以它为准；
    - 配置还是默认值 → 用该渠道自己的经验值，没列到的渠道才回落到配置值。
    """
    configured = int(getattr(settings, "estimate_image_kb", 0) or 0)
    if configured and configured != DEFAULT_ESTIMATE_IMAGE_KB:
        return max(1, configured)
    return max(1, ESTIMATE_IMAGE_KB_BY_CHANNEL.get(channel, configured or DEFAULT_ESTIMATE_IMAGE_KB))


def estimate_bytes(meta: ComicMeta, settings: PluginSettings) -> tuple[int, bool]:
    """返回（字节数，是否是估算值）。

    真实值直接返回 ``(size_bytes, False)``；抽样推算值（``size_sampled_bytes``）仍是估算，
    所以照样返回 ``True``；两者都没有但有页数时按单张体积估；页数也没有时返回 ``(0, True)``，
    由展示层整段省略。
    """
    if meta.size_bytes > 0:
        return meta.size_bytes, False
    if meta.size_sampled_bytes > 0:
        return meta.size_sampled_bytes, True
    if meta.page_count <= 0:
        return 0, True
    kb = per_image_kb(meta.channel, settings)
    return meta.page_count * kb * 1024, True


def size_line(meta: ComicMeta, settings: PluginSettings) -> str:
    """「体积」这一行的正文；估不出来时返回空串（调用方据此省略整行）。"""
    size, estimated = estimate_bytes(meta, settings)
    if size <= 0:
        return ""
    return f"约 {format_size(size)}" if estimated else format_size(size)


async def refine_with_samples(
    meta: ComicMeta,
    settings: PluginSettings,
    *,
    urls_for: SampleUrlsFn | None,
    size_of: SizeFn | None,
    budget: float = SAMPLE_BUDGET,
) -> int:
    """抽样实测：真下前几张量平均单张体积，写回 ``meta.size_sampled_bytes`` 并返回样本字节总数。

    只在三种条件下动手：配置里打开了抽样、渠道能给出图片地址、当前还不知道真实体积。
    任何一步失败（取不到地址、抓图超时、样本为 0）都**保持原样**——估算退回固定值，
    调用方从返回值就能看出「这次没测成」，不必区分是哪种失败。

    Args:
        meta: 作品元信息；成功时它的 ``size_sampled_bytes`` 会被写成整本推算值。
        settings: 全局配置（抽样开关、张数、单张体积）。
        urls_for: 取图片地址的回调；``None`` 表示该渠道不支持抽样。
        size_of: 取单个地址字节数的回调；``None`` 时同样不抽样。
        budget: 整轮总时限（秒）。

    Returns:
        实测到的样本总字节数；0 表示这次没测成、``meta`` 未被改动。
    """
    if not getattr(settings, "estimate_sample_enabled", False):
        return 0
    if meta.size_bytes > 0 or meta.page_count <= 0:
        return 0
    if urls_for is None or size_of is None:
        return 0
    want = max(1, int(getattr(settings, "estimate_sample_count", 1) or 1))

    async def sample() -> int:
        urls = await urls_for(meta.comic_id)
        if not urls:
            return 0
        picked = urls[:want]
        sizes = await asyncio.gather(
            *(size_of(url) for url in picked), return_exceptions=True
        )
        total = 0
        for size in sizes:
            if isinstance(size, int) and size > 0:
                total += size
        return total

    try:
        total = await asyncio.wait_for(sample(), timeout=max(1.0, budget))
    except asyncio.TimeoutError:
        logger.info(
            f"[{PLUGIN_NAME}] {meta.channel} 抽样体积超过 {budget:.0f} 秒，"
            "本次按固定单张体积估算"
        )
        return 0
    except Exception as exc:  # 抽样只是估算，任何失败都不该影响「看简介」本身
        logger.warning(
            f"[{PLUGIN_NAME}] {meta.channel} 抽样体积失败（已忽略）：{exc}"
        )
        return 0

    if total <= 0:
        return 0
    # 平均单张 × 总页数：样本可能不足（页数少于抽样张数），按实际取到的张数算平均。
    # 结果写进 size_sampled_bytes 而不是 size_bytes：它是推算的，展示时仍要标「约」
    counted = min(want, meta.page_count)
    average = total / max(1, counted)
    meta.size_sampled_bytes = int(average * meta.page_count)
    logger.info(
        f"[{PLUGIN_NAME}] {meta.channel} {meta.comic_id} 抽样 {counted} 张"
        f"（{format_size(total)}），估算整本约 {format_size(meta.size_sampled_bytes)}"
    )
    return total


async def http_size(
    url: str,
    *,
    proxy: str = "",
    headers: dict[str, str] | None = None,
    timeout: float = SIZE_TIMEOUT,
) -> int:
    """量一个图片地址有多少字节，按「代价从低到高」试三种问法。

    1. **HEAD**：只看响应头，一次请求就够，图床支持时最省；
    2. **Range**：`Range: bytes=0-0` 的响应带 `Content-Range: bytes 0-0/<总长>`，
       同样只看头；图床普遍不支持 HEAD（直接 405/403），这条才是主力；
    3. **真下这一张**：前两种都问不出来（图床既不给头也不认 Range）时，把这张读完量长度。
       样本只有一两张，且抽样本身有总时限，不会拖住「看简介」。

    Returns:
        字节数；三种问法都拿不到时返回 0（调用方当作「这张没测到」跳过）。
    """
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - AstrBot 运行时自带 aiohttp
        return 0

    session_kwargs: dict[str, object] = {"trust_env": False}
    if proxy:
        session_kwargs["proxy"] = proxy
    base_headers = dict(headers or {})
    deadline = aiohttp.ClientTimeout(total=timeout)

    try:
        async with aiohttp.ClientSession(**session_kwargs) as session:
            # ① HEAD：最省，图床支持时一次请求就够
            try:
                async with session.head(
                    url, headers=base_headers, timeout=deadline, allow_redirects=True
                ) as response:
                    if response.status < 400:
                        length = int(response.headers.get("Content-Length") or 0)
                        if length > 0:
                            return length
            except (asyncio.TimeoutError, OSError, aiohttp.ClientError):  # 不支持 HEAD
                pass  # 交给下面的问法，不在这里放弃

            # ② Range：只请求第 0 个字节，从 Content-Range 里读总长
            ranged = dict(base_headers)
            ranged["Range"] = "bytes=0-0"
            try:
                async with session.get(
                    url, headers=ranged, timeout=deadline, allow_redirects=True
                ) as response:
                    if response.status < 400:
                        content_range = response.headers.get("Content-Range") or ""
                        total = content_range.rsplit("/", 1)[-1].strip()
                        if total.isdigit():
                            return int(total)
                        length = int(response.headers.get("Content-Length") or 0)
                        if length > 0:
                            return length
            except (asyncio.TimeoutError, OSError, aiohttp.ClientError):  # 不认 Range
                pass  # 退回下面真下这张

            # ③ 真下这一张：样本只有一两张，量长度即可，不必落盘
            try:
                async with session.get(
                    url, headers=base_headers, timeout=deadline, allow_redirects=True
                ) as response:
                    if response.status >= 400:
                        return 0
                    received = 0
                    async for chunk in response.content.iter_chunked(16384):
                        received += len(chunk)
                    return received
            except (asyncio.TimeoutError, OSError, aiohttp.ClientError):
                return 0
    except (asyncio.TimeoutError, OSError):
        return 0

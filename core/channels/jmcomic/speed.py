"""JM 渠道的域名测速：先比响应时间，再拿一张真图比吞吐。

两段都跑在**同一个域名**上、各自独立完成，所以可以直接并发——不需要先有「赢家」，
也就不必为了测速串行跑两轮。

为什么非测不可：jmcomic 只在请求**失败**时才换下一个域名，图片 URL 里的域名又是站点
在响应里直接给出的绝对地址，图片阶段根本不参与换域名。「连得上但很慢」的镜像因此在
它眼里完全正常，只能一路等到超时。所以开局就把最快的域名排到第一位才是解法。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

from ....domain.constants import PLUGIN_NAME
from ...domain_race import DomainTiming, ProbeFn, probe_url
from .option import build_option

if TYPE_CHECKING:  # 只为类型标注，避免运行期多绕一层 import
    from ...channel import BaseChannel


def _first_image_url(client: Any, album: Any) -> str | None:
    """从专辑里挖出第一张图的真实下载地址；挖不到返回 None（只按响应时间排）。"""
    episodes = getattr(album, "episode_list", None)
    if not episodes:
        return None
    photo = client.get_photo_detail(episodes[0][0])
    for image in photo:
        url = str(getattr(image, "download_url", "") or "")
        if url:
            return url
    return None


def _measure(
    option: Any, comic_id: str, domain: str, budget_ms: int
) -> tuple[int, str | None]:
    """在单个域名上取专辑详情（顺带挖一张图片地址），返回（响应毫秒, 图片地址）。

    第一段就吃掉大半预算时不再取图片样本：测速是给用户省时间的，不能自己先卡住。
    """
    started = time.monotonic()
    client = option.new_jm_client(domain_list=[domain])
    album = client.get_album_detail(comic_id)
    latency_ms = int((time.monotonic() - started) * 1000)
    if latency_ms * 2 > budget_ms:
        return latency_ms, None
    try:
        return latency_ms, _first_image_url(client, album)
    except Exception as exc:  # noqa: BLE001 - 拿不到样本就退化成「只比响应时间」
        logger.info(
            f"[{PLUGIN_NAME}] JM 域名 {domain} 取图片样本失败，只按响应时间排序：{exc}"
        )
        return latency_ms, None


def build_probe(
    channel: BaseChannel,
    jmcomic: Any,
    comic_id: str,
    work_dir: Path,
    *,
    timeout: int,
    proxy: str,
) -> ProbeFn:
    """构造 JM 的探测回调：专辑详情（响应时间）+ 首图样本（吞吐）各一次请求。"""
    album_id = jmcomic.JmcomicText.parse_to_jm_id(comic_id)
    # 测速用「零重试 + 短超时」的 option：坏节点必须立刻判慢，不能靠重试把时间耗光
    option = build_option(
        channel, jmcomic, work_dir, retry_times=0, request_timeout=timeout
    )
    headers: dict[str, str] = dict(jmcomic.JmModuleConfig.APP_HEADERS_TEMPLATE)
    headers.update(jmcomic.JmModuleConfig.APP_HEADERS_IMAGE)
    budget_ms = timeout * 1000

    async def probe(domain: str) -> DomainTiming:
        try:
            latency_ms, image_url = await asyncio.to_thread(
                _measure, option, album_id, domain, budget_ms
            )
        except Exception as exc:  # noqa: BLE001 - 单域名失败只影响它自己的排名
            return DomainTiming(domain, False, 0, 0.0, f"接口不可用：{exc}")
        if not image_url:
            return DomainTiming(domain, True, latency_ms)
        sample = await probe_url(domain, image_url, proxy=proxy, headers=headers)
        if not sample.ok:
            return DomainTiming(domain, True, latency_ms, 0.0, f"图片样本 {sample.note}")
        return DomainTiming(domain, True, latency_ms, sample.speed_kbps)

    return probe

"""下载前的域名测速择优：同时打一批候选域名，把最快的排到最前面。

站点镜像常有「连得上但极慢」的节点，而渠道自带的换域名逻辑只在**请求失败**时才降级——
慢不算失败，于是只能在超时上干等。所以真正要解决的是「开局先挑对域名」：这里并发探测
全部候选（不排队等），量出响应时间与小样本下载速度，按实测速度排序，再交给渠道当成
降级顺序使用。

测速永远是**尽力而为**：探测面打不开、测速关闭、只有一个候选时，调用方拿到原顺序，
下载照旧，不会因为测速本身失败而阻断任务。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

# 单域名吞吐样本上限：128KB 足够看出「快/慢」量级，又不至于把测速本身变成一次下载
SAMPLE_BYTES: Final = 128 * 1024
# 低于这个字节数的响应体算「样本不足」：只按响应时间排，不拿它算速度
MIN_SAMPLE_BYTES: Final = 32 * 1024
# 同时探测的候选数上限：清单再长也不至于把本机连接数打满
MAX_PROBES: Final = 8
# 出现第一个可用候选后，给其它候选的追赶窗口（秒）：窗口内没跑赢就判定它更慢
GRACE_SECONDS: Final = 1.5


def format_speed(kbps: float) -> str:
    """把 KB/s 说成人话，日志与失败说明共用同一套单位。"""
    if kbps >= 1024:
        return f"{kbps / 1024:.1f}MB/s"
    if kbps > 0:
        return f"{kbps:.0f}KB/s"
    return "速度未知"


@dataclass(slots=True)
class DomainTiming:
    """一个候选域名的探测结果：可用性、响应时间、实测速度。"""

    domain: str
    ok: bool
    latency_ms: int = 0
    speed_kbps: float = 0.0  # 0 表示样本不足，测不出速度
    note: str = ""

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"{self.domain}（{self.note or '不可用'}）"
        if self.speed_kbps > 0:
            return f"{self.domain}（{self.latency_ms}ms · {format_speed(self.speed_kbps)}）"
        return f"{self.domain}（{self.latency_ms}ms）"


# 探测回调：输入候选域名，输出该候选的探测结果；由各渠道按自己的探测面实现
ProbeFn = Callable[[str], Awaitable[DomainTiming]]


async def race_domains(
    domains: Sequence[str],
    probe: ProbeFn,
    *,
    timeout: float,
    concurrency: int = MAX_PROBES,
) -> list[DomainTiming]:
    """并发探测全部候选，返回与输入同序的结果列表。

    ``timeout`` 是**单个候选**的上限；一旦有候选成功返回，其余候选只有 ``GRACE_SECONDS``
    的追赶窗口，跑不过就被判为慢并取消——否则一个死节点会把整轮测速拖到超时上限，
    用户等来的时间就白省了。
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def guarded(domain: str) -> DomainTiming:
        async with semaphore:
            try:
                return await asyncio.wait_for(probe(domain), timeout)
            except asyncio.TimeoutError:
                return DomainTiming(domain, False, 0, 0.0, f"超过 {timeout:g}s")
            except Exception as exc:  # noqa: BLE001 - 探测失败只降级，绝不冒泡打断下载
                return DomainTiming(domain, False, 0, 0.0, f"探测失败：{exc}")

    tasks: dict[asyncio.Task[DomainTiming], str] = {
        asyncio.create_task(guarded(domain)): domain for domain in domains
    }
    results: dict[str, DomainTiming] = {}
    pending: set[asyncio.Task[DomainTiming]] = set(tasks)
    try:
        # 先等出第一个能用的候选（全灭时才等到全部结束）
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                results[tasks[task]] = task.result()
            if any(item.ok for item in results.values()):
                break
        if pending:
            done, pending = await asyncio.wait(pending, timeout=GRACE_SECONDS)
            for task in done:
                results[tasks[task]] = task.result()
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return [
        results.get(domain) or DomainTiming(domain, False, 0, 0.0, "未在测速窗口内返回")
        for domain in domains
    ]


def order_domains(
    timings: Sequence[DomainTiming], *, min_speed_kbps: float = 0.0
) -> list[str]:
    """按实测结果排序：快 → 慢 → 不可用（不可用的仍留在末尾当降级备选）。

    「低于阈值」只影响排序，不剔除：整批都慢的时候仍要有候选可用。
    """

    def bucket(timing: DomainTiming) -> int:
        if not timing.ok:
            return 2
        if min_speed_kbps > 0 and 0 < timing.speed_kbps < min_speed_kbps:
            return 1
        return 0

    def sort_key(timing: DomainTiming) -> tuple[int, float, int]:
        level = bucket(timing)
        if level == 0 and timing.speed_kbps > 0:
            return (level, -timing.speed_kbps, timing.latency_ms)
        # 没有速度数据时按响应时间排；连响应时间都没有的排最后
        return (level, 0.0, timing.latency_ms or 10**6)

    return [item.domain for item in sorted(timings, key=sort_key)]


def summarize(timings: Sequence[DomainTiming]) -> str:
    """一行式测速摘要，供日志与用户提示复用。"""
    return " · ".join(item.summary for item in timings)


class RankingCache:
    """测速结果缓存：同一批候选在有效期内只测一次，避免每本都重复探测。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, list[str]]] = {}

    def get(self, key: str, ttl: int) -> list[str] | None:
        item = self._items.get(key)
        if item is None:
            return None
        stored_at, ranked = item
        if ttl <= 0 or time.monotonic() - stored_at > ttl:
            self._items.pop(key, None)
            return None
        return list(ranked)

    def put(self, key: str, ranked: Sequence[str]) -> None:
        # 键里已含候选清单本身，配置一改自然换键，旧键靠 TTL 自然过期
        self._items[key] = (time.monotonic(), list(ranked))

    def clear(self) -> None:
        self._items.clear()


CACHE: Final = RankingCache()


# ---- 基于 HTTP 的探测面：多数渠道的候选域名都能用同一套量法 ----


async def probe_url(
    domain: str,
    url: str,
    *,
    proxy: str = "",
    headers: Mapping[str, str] | None = None,
    sample_bytes: int = SAMPLE_BYTES,
    reject_markers: Sequence[bytes] = (),
) -> DomainTiming:
    """量一个 URL：首字节耗时作为响应时间，读完样本字节的开流速度作为速度。

    ``reject_markers`` 用来识别「HTTP 200 但内容是被拦页面」的假成功——站点躲在
    Cloudflare 后面时，挑战页本身就是 200，光看状态码会把最慢的节点排到第一。
    """
    try:
        import aiohttp
    except ImportError:
        return DomainTiming(domain, False, 0, 0.0, "缺少 aiohttp")

    started = time.monotonic()
    session_kwargs: dict[str, Any] = {"trust_env": False}
    if proxy:
        session_kwargs["proxy"] = proxy

    try:
        async with aiohttp.ClientSession(**session_kwargs) as session:
            async with session.get(
                url, headers=dict(headers or {}), allow_redirects=True
            ) as resp:
                latency = time.monotonic() - started
                if resp.status >= 400:
                    return DomainTiming(
                        domain, False, int(latency * 1000), 0.0, f"HTTP {resp.status}"
                    )
                received = 0
                head = b""
                async for chunk in resp.content.iter_chunked(16384):
                    received += len(chunk)
                    if reject_markers and len(head) < 8192:
                        head += chunk
                        if any(marker in head for marker in reject_markers):
                            return DomainTiming(
                                domain, False, int(latency * 1000), 0.0, "返回被拦页面"
                            )
                    if received >= sample_bytes:
                        break
    except asyncio.TimeoutError:
        return DomainTiming(domain, False, 0, 0.0, "请求超时")
    except Exception as exc:  # noqa: BLE001 - 网络栈异常类型五花八门，统一算探测失败
        return DomainTiming(domain, False, 0, 0.0, f"请求失败：{exc}")

    elapsed = time.monotonic() - started
    latency_ms = int(latency * 1000)
    if received < MIN_SAMPLE_BYTES:
        # 响应体太小（错误页、空 JSON）算出来的「速度」毫无意义，只交回响应时间
        return DomainTiming(domain, True, latency_ms, 0.0, "样本不足")
    # 速度只算「拿到响应之后」那一段，否则小站点会因 TLS 握手被误判成慢
    body_seconds = max(elapsed - latency, 0.001)
    return DomainTiming(domain, True, latency_ms, round(received / body_seconds / 1024, 1))


def http_probe(
    url_for: Callable[[str], str],
    *,
    proxy: str = "",
    headers: Mapping[str, str] | None = None,
    sample_bytes: int = SAMPLE_BYTES,
    reject_markers: Sequence[bytes] = (),
) -> ProbeFn:
    """把「候选 → 探测 URL」的映射包装成 ``race_domains`` 能吃的探测回调。"""

    async def probe(domain: str) -> DomainTiming:
        return await probe_url(
            domain,
            url_for(domain),
            proxy=proxy,
            headers=headers,
            sample_bytes=sample_bytes,
            reject_markers=reject_markers,
        )

    return probe

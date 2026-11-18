"""下载统计：累计计数落盘，另记本次运行的增量。

**为什么落盘**：只放内存时，插件一重载（改配置、更新插件都会触发）数字就归零，
管理员永远看不到「这个月下了多少」。落盘后累计数跨重载保留，同时单独算出「本次运行」
的增量——两个视角都有用，混成一个数会让人分不清「重启前的」还是「刚下的」。

**计数口径**（写在这里，因为它是这个文件唯一容易出错的地方）：

- 一次任务收尾记一次，成功 / 失败 / 暂停各算一次。暂停与取消不是失败，但确实占过一次队列，
  所以要单独一列——只数成功会把「十个请求全失败」显示成「零次下载」。
- 未知结局按失败记：宁可多报一次失败，也不能静默吞掉。
- 全局合计**由各渠道相加得出**，不另存一份：两份数各写一次必然有漂移的一天，
  而用户看到的又恰好是那个对不上的版本。

落盘格式带 ``version``；版本不认识、文件损坏、字段类型不对，一律当作「没有历史」重新开始，
绝不让统计文件把插件加载拖崩。写入走 ``.tmp`` + ``replace`` 的原子替换。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..domain.constants import PLUGIN_NAME, STATS_VERSION
from ..utils.format import format_size

# 一次下载的结局：只有 ok 会产出文件，paused 是「断点还在，可以 retry」
OUTCOME_OK = "ok"
OUTCOME_FAIL = "fail"
OUTCOME_PAUSED = "paused"
_OUTCOMES = (OUTCOME_OK, OUTCOME_FAIL, OUTCOME_PAUSED)


@dataclass(slots=True)
class ChannelStat:
    """单个渠道的累计数字。"""

    ok: int = 0
    fail: int = 0
    paused: int = 0
    images: int = 0
    failed_images: int = 0
    bytes: int = 0
    seconds: float = 0.0

    @property
    def total(self) -> int:
        """真正跑过的次数：暂停不算失败，但确实占过一次队列。"""
        return self.ok + self.fail + self.paused

    def snapshot(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "fail": self.fail,
            "paused": self.paused,
            "total": self.total,
            "images": self.images,
            "failed_images": self.failed_images,
            "bytes": self.bytes,
            "seconds": round(self.seconds, 1),
        }

    def to_payload(self) -> dict[str, Any]:
        """落盘用的紧凑结构：键名与读回时一一对应。"""
        return {
            "ok": self.ok,
            "fail": self.fail,
            "paused": self.paused,
            "images": self.images,
            "failed_images": self.failed_images,
            "bytes": self.bytes,
            "seconds": round(self.seconds, 3),
        }

    @classmethod
    def from_payload(cls, raw: object) -> "ChannelStat | None":
        """从落盘数据还原；类型不对就当这条没有，不让脏数据污染累计数。"""
        if not isinstance(raw, dict):
            return None
        return cls(
            ok=_as_int(raw.get("ok")),
            fail=_as_int(raw.get("fail")),
            paused=_as_int(raw.get("paused")),
            images=_as_int(raw.get("images")),
            failed_images=_as_int(raw.get("failed_images")),
            bytes=_as_int(raw.get("bytes")),
            seconds=_as_float(raw.get("seconds")),
        )


def _as_int(raw: object) -> int:
    try:
        return max(0, int(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_float(raw: object) -> float:
    try:
        return max(0.0, float(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


@dataclass(slots=True)
class StatsSnapshot:
    """一份给展示层用的快照：累计 + 本次运行 + 按渠道。"""

    started_at: float
    updated_at: float
    ok: int = 0
    fail: int = 0
    paused: int = 0
    images: int = 0
    failed_images: int = 0
    bytes: int = 0
    seconds: float = 0.0
    channels: dict[str, ChannelStat] = field(default_factory=dict)
    # 本次运行（插件加载之后）的增量；口径与累计一致
    session: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.ok + self.fail + self.paused

    @property
    def uptime(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def snapshot(self) -> dict[str, Any]:
        """面板接口用的结构；累计字段保持顶层，本次运行放 ``session``。"""
        return {
            "since": int(self.started_at),
            "updated": int(self.updated_at),
            "uptime_seconds": int(self.uptime),
            "ok": self.ok,
            "fail": self.fail,
            "paused": self.paused,
            "total": self.total,
            "images": self.images,
            "failed_images": self.failed_images,
            "bytes": self.bytes,
            "seconds": round(self.seconds, 1),
            "session": dict(self.session),
            "channels": {
                name: stat.snapshot()
                for name, stat in sorted(
                    self.channels.items(), key=lambda item: (-item[1].total, item[0])
                )
            },
        }


def _human_span(seconds: float) -> str:
    """时长说人话：一小时以内报分钟，再长报小时。"""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时" if not minutes else f"{hours} 小时 {minutes} 分钟"
    days, hours = divmod(hours, 24)
    return f"{days} 天" if not hours else f"{days} 天 {hours} 小时"


def _human_stamp(timestamp: float) -> str:
    """把时间戳写成年月日：用户要判断的是「这份数据有多旧」。"""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(timestamp))
    except (OSError, ValueError, OverflowError):
        return "未知时间"


class DownloadStats:
    """线程安全的下载计数，可选落盘。

    Args:
        path: 统计文件路径；``None`` 表示纯内存（自测与不需要持久化时用）。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = Path(path) if path is not None else None
        self._channels: dict[str, ChannelStat] = {}
        self._started_at = time.time()
        self._updated_at = self._started_at
        # 加载那一刻的基线：本次运行 = 当前值 - 基线，重载后自动归零
        self._baseline: dict[str, ChannelStat] = {}
        self._load()
        self._baseline = self._copy_channels()

    # ---- 记录 ----

    def record(
        self,
        channel: str,
        outcome: str,
        *,
        images: int = 0,
        failed_images: int = 0,
        size_bytes: int = 0,
        elapsed: float = 0.0,
    ) -> None:
        """记一次任务收尾。

        Args:
            channel: 渠道内部名（``jmcomic`` / ``nhentai``…）。
            outcome: ``ok`` / ``fail`` / ``paused``；未知值按 ``fail`` 记。
            images: 这次下到的图片张数（失败时为 0）。
            failed_images: 单张失败张数。
            size_bytes: 产物字节数（仅成功时非 0）。
            elapsed: 耗时（秒）。
        """
        if outcome not in _OUTCOMES:
            outcome = OUTCOME_FAIL
        images = max(0, int(images))
        failed_images = max(0, int(failed_images))
        size_bytes = max(0, int(size_bytes))
        elapsed = max(0.0, float(elapsed))

        with self._lock:
            stat = self._channels.setdefault(channel, ChannelStat())
            setattr(stat, outcome, getattr(stat, outcome) + 1)
            stat.images += images
            stat.failed_images += failed_images
            stat.bytes += size_bytes
            stat.seconds += elapsed
            self._updated_at = time.time()
            self._save_locked()

    def reset(self) -> None:
        """清零并重新计时：管理员想从「现在」开始看时用。"""
        with self._lock:
            now = time.time()
            self._channels.clear()
            self._started_at = now
            self._updated_at = now
            self._baseline = {}
            self._save_locked()

    # ---- 读取 ----

    def snapshot(self) -> StatsSnapshot:
        with self._lock:
            channels = self._copy_channels()
            return StatsSnapshot(
                started_at=self._started_at,
                updated_at=self._updated_at,
                ok=sum(item.ok for item in channels.values()),
                fail=sum(item.fail for item in channels.values()),
                paused=sum(item.paused for item in channels.values()),
                images=sum(item.images for item in channels.values()),
                failed_images=sum(item.failed_images for item in channels.values()),
                bytes=sum(item.bytes for item in channels.values()),
                seconds=sum(item.seconds for item in channels.values()),
                channels=channels,
                session=self._session_totals(channels),
            )

    def render(self) -> str:
        """聊天里的正文：先给累计，再给本次运行，最后按渠道列明细。"""
        snap = self.snapshot()
        if not snap.total:
            return "ℹ️ 还没有下载记录；下过一本之后再看就有数了"

        lines = [
            f"📈 累计 {snap.total} 次下载 · 成功 {snap.ok} / 失败 {snap.fail}"
            + (f" / 暂停 {snap.paused}" if snap.paused else ""),
            f"已下 {snap.images} 张"
            + (f"（失败 {snap.failed_images} 张）" if snap.failed_images else "")
            + f" · 产物 {format_size(snap.bytes)} · 累计耗时 {_human_span(snap.seconds)}",
        ]

        session = snap.session
        session_total = int(session.get("total", 0))
        if session_total:
            lines.append(
                f"本次运行 {session_total} 次（成功 {session.get('ok', 0)}"
                f" / 失败 {session.get('fail', 0)}"
                + (f" / 暂停 {session.get('paused', 0)}" if session.get("paused") else "")
                + "）"
            )
        else:
            lines.append("本次运行还没有新的下载")

        ordered = sorted(
            snap.channels.items(), key=lambda item: (-item[1].total, item[0])
        )
        for name, stat in ordered:
            parts = [f"成功 {stat.ok}"]
            if stat.fail:
                parts.append(f"失败 {stat.fail}")
            if stat.paused:
                parts.append(f"暂停 {stat.paused}")
            tail = " · ".join(parts)
            if stat.ok:
                tail += f" · {stat.images} 张 / {format_size(stat.bytes)}"
            lines.append(f"· {name}：{tail}")

        if self._path is not None:
            lines.append(
                f"统计自 {_human_stamp(snap.started_at)} 起，存于 {self._path.name}"
            )
        else:
            lines.append("本次运行统计，重启即归零")
        return "\n".join(lines)

    # ---- 内部 ----

    def _copy_channels(self) -> dict[str, ChannelStat]:
        return {
            name: ChannelStat(
                ok=stat.ok,
                fail=stat.fail,
                paused=stat.paused,
                images=stat.images,
                failed_images=stat.failed_images,
                bytes=stat.bytes,
                seconds=stat.seconds,
            )
            for name, stat in self._channels.items()
        }

    def _session_totals(self, channels: dict[str, ChannelStat]) -> dict[str, int]:
        """本次运行 = 当前值 - 加载时的基线；基线里没有的渠道按 0 算。"""
        totals = {key: 0 for key in (*_OUTCOMES, "total", "images", "bytes")}
        for name, stat in channels.items():
            base = self._baseline.get(name)
            totals["ok"] += stat.ok - (base.ok if base else 0)
            totals["fail"] += stat.fail - (base.fail if base else 0)
            totals["paused"] += stat.paused - (base.paused if base else 0)
            totals["images"] += stat.images - (base.images if base else 0)
            totals["bytes"] += stat.bytes - (base.bytes if base else 0)
        totals["total"] = totals["ok"] + totals["fail"] + totals["paused"]
        return totals

    def _load(self) -> None:
        """读回落盘的累计数；文件不存在、版本不认识、内容损坏都当作「没有历史」。"""
        if self._path is None or not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"[{PLUGIN_NAME}] 统计文件读取失败，按无历史处理: {exc}")
            return
        if not isinstance(raw, dict):
            logger.warning(f"[{PLUGIN_NAME}] 统计文件结构不对，按无历史处理")
            return

        version = raw.get("version")
        if version != STATS_VERSION:
            # 版本不认识就不猜字段含义：留着旧文件不删，下次写入时自然被覆盖
            logger.warning(
                f"[{PLUGIN_NAME}] 统计文件版本是 {version!r}，当前 {STATS_VERSION}，按无历史处理"
            )
            return

        channels = raw.get("channels")
        if not isinstance(channels, dict):
            return
        for name, item in channels.items():
            if not isinstance(name, str) or not name:
                continue
            stat = ChannelStat.from_payload(item)
            if stat is not None and stat.total:
                self._channels[name] = stat

        self._started_at = _as_float(raw.get("started_at")) or self._started_at
        self._updated_at = _as_float(raw.get("updated_at")) or self._started_at
        if self._channels:
            logger.info(
                f"[{PLUGIN_NAME}] 已读回下载统计："
                f"{sum(item.total for item in self._channels.values())} 次"
            )

    def _save_locked(self) -> None:
        """落盘（调用方必须已持锁）。

        文件只有几百字节，且只在一次下载收尾时写，不值得为它引入异步队列；
        写失败只记日志——统计写不进去绝不能影响下载本身。
        """
        if self._path is None:
            return
        payload = {
            "version": STATS_VERSION,
            "started_at": round(self._started_at, 3),
            "updated_at": round(self._updated_at, 3),
            "channels": {
                name: stat.to_payload() for name, stat in self._channels.items()
            },
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 统计写入失败（不影响下载）: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "ChannelStat",
    "DownloadStats",
    "OUTCOME_FAIL",
    "OUTCOME_OK",
    "OUTCOME_PAUSED",
    "StatsSnapshot",
]

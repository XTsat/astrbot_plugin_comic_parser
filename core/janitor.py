"""手动清理：把「本来就该过期」的临时产物立刻清掉。

插件自己有一套惰性清理——断点按「断点保留时长」在下次下载前过期、封面与榜单图在下次出图
前按 TTL 清。问题在于这些清理都要等**下一次用到**才跑：服务器如果几天不出封面，`temp/covers`
就停在那儿；磁盘吃紧时也没有「现在立刻腾空间」的入口，只能去开「启动时清理临时目录」再
重载插件（那会连断点一起删掉）。

这个模块只做一件事：把上述 TTL 判定**现在**跑一遍。删的全是「按现有配置已经过期」的东西，
因此不会删掉任何还能用的断点，也不会碰长期保存目录（仅本地保存模式的产物是用户资产）。

**不删**：正在下载的目录（由 `active_work_dirs` 提供）、未过期的断点、`temp/panel`
（面板自己按 LRU 管，且它的文件与内存里的暂存表一一对应，外部删掉会让「下载打码结果」失效）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

from ..domain.constants import PLUGIN_NAME, RESUME_DIRNAME
from ..utils.format import format_size
from .packer import Packer

# 封面临时图与榜单中间图的 TTL：与 core/cover.py、core/ranking_card.py 里的判定保持一致。
# 两处各写一个常量是因为那两个模块 import 了 Pillow，而清理命令不该为此拖进图像栈
COVER_TTL: float = 3600.0
CARD_TTL: float = 3600.0
# 榜单成图缓存的上限（与 domain.constants.RANKING_IMAGE_CACHE_MAX 同值，见其注释）
RANKING_CACHE_MAX: float = 31 * 24 * 3600.0
# `temp/info` 下的作品信息抓取目录：命令结束时就会删，正常不会留下，
# 留下说明那次被强杀了，一小时后清掉
INFO_TTL: float = 3600.0


@dataclass(slots=True)
class CleanupResult:
    """一次清理的结果：删了什么、省了多少空间。"""

    removed_files: int = 0
    removed_dirs: int = 0
    freed_bytes: int = 0
    # 逐项说明，聊天里直接列出来（如「过期断点 2 个，128 MB」）
    groups: list[str] = field(default_factory=list)
    skipped_active: int = 0

    @property
    def removed_total(self) -> int:
        return self.removed_files + self.removed_dirs

    def render(self) -> str:
        if not self.removed_total:
            return "✅ 没有过期的东西可清：断点、封面临时图、榜单图都在有效期内"
        lines = [f"🧹 已清理 {self.removed_total} 项，释放 {format_size(self.freed_bytes)}"]
        lines.extend(f"· {item}" for item in self.groups)
        if self.skipped_active:
            lines.append(f"（跳过 {self.skipped_active} 个正在下载的目录）")
        lines.append("仅本地保存目录（downloads）不受影响")
        return "\n".join(lines)


def _newest_mtime(path: Path) -> float:
    """目录里最新一次写入的时间：断点是否过期要看里面最后一张图，不是父目录的 mtime。"""
    newest = path.stat().st_mtime
    for child in path.rglob("*"):
        try:
            if child.is_file():
                newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
    return newest


def _tree_size(path: Path) -> int:
    """目录（或单个文件）占多少字节；读不到的部分算 0，不为了报个数把清理搞失败。"""
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _remove(path: Path) -> bool:
    existed = path.exists()
    Packer.cleanup(path)
    return existed


def cleanup_temp(
    temp_root: Path,
    *,
    resume_ttl: float,
    active_dirs: set[Path] | frozenset[Path] = frozenset(),
    now: float | None = None,
) -> CleanupResult:
    """清掉已经过期的临时产物。

    Args:
        temp_root: 插件的临时目录根（``data_dir/temp``）。
        resume_ttl: 断点保留时长（秒）；``<= 0`` 表示不留断点，全部算过期。
        active_dirs: 正在下载的工作目录，一个都不碰。
        now: 当前时间戳，单测注入用。

    Returns:
        CleanupResult: 删了多少、省了多少、按类分组的说明。
    """
    result = CleanupResult()
    if not temp_root.is_dir():
        return result

    now = time.time() if now is None else now
    active = {Path(item).resolve() for item in active_dirs}

    def expired(path: Path, ttl: float) -> bool:
        try:
            newest = _newest_mtime(path) if path.is_dir() else path.stat().st_mtime
        except OSError:
            return False
        return ttl <= 0 or now - newest > ttl

    def take(path: Path) -> int:
        """删一个（文件或目录）并记账；返回释放的字节数。"""
        size = _tree_size(path)
        if path.is_dir():
            result.removed_dirs += 1
        else:
            result.removed_files += 1
        result.freed_bytes += size
        _remove(path)
        return size

    # ---- 过期断点 ----
    resume_root = temp_root / RESUME_DIRNAME
    if resume_root.is_dir():
        count = 0
        size = 0
        for entry in resume_root.iterdir():
            try:
                if entry.resolve() in active:
                    result.skipped_active += 1
                    continue
            except OSError:
                continue
            if not expired(entry, float(resume_ttl)):
                continue
            size += take(entry)
            count += 1
        if count:
            result.groups.append(f"过期断点 {count} 个，{format_size(size)}")

    # ---- 封面临时图 ----
    covers = temp_root / "covers"
    if covers.is_dir():
        count = 0
        size = 0
        for entry in covers.iterdir():
            if entry.is_file() and expired(entry, COVER_TTL):
                size += take(entry)
                count += 1
        if count:
            result.groups.append(f"过期封面临时图 {count} 张，{format_size(size)}")

    # ---- 榜单中间图与成图缓存 ----
    ranking = temp_root / "ranking"
    if ranking.is_dir():
        count = 0
        size = 0
        for entry in ranking.iterdir():
            if not entry.is_file():
                continue
            # 成图缓存按缓存上限留，其余中间图一小时
            ttl = RANKING_CACHE_MAX if entry.name.startswith("cache-") else CARD_TTL
            if expired(entry, ttl):
                size += take(entry)
                count += 1
        if count:
            result.groups.append(f"过期榜单图 {count} 张，{format_size(size)}")

    # ---- 被强杀留下的信息抓取目录 ----
    info = temp_root / "info"
    if info.is_dir():
        count = 0
        size = 0
        for entry in info.iterdir():
            try:
                if entry.resolve() in active:
                    result.skipped_active += 1
                    continue
            except OSError:
                continue
            if expired(entry, INFO_TTL):
                size += take(entry)
                count += 1
        if count:
            result.groups.append(f"残留信息目录 {count} 个，{format_size(size)}")

    if result.removed_total:
        logger.info(
            f"[{PLUGIN_NAME}] 清理临时目录："
            f"{result.removed_total} 项，{format_size(result.freed_bytes)}"
        )
    return result


__all__ = [
    "CARD_TTL",
    "COVER_TTL",
    "CleanupResult",
    "INFO_TTL",
    "RANKING_CACHE_MAX",
    "cleanup_temp",
]

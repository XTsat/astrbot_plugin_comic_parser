"""准入控制：会话白/黑名单 + 同会话防抖。

这部分是「底板逻辑」——与具体渠道无关，换渠道不需要改这里。
"""

from __future__ import annotations

import time

from ..domain import PluginSettings


class AccessGuard:
    """判定一次请求是否放行。"""

    def __init__(self, settings: PluginSettings) -> None:
        self._whitelist: set[str] = set()
        self._blacklist: set[str] = set()
        self._interval = 0
        self._cache: dict[str, dict[str, float]] = {}
        self.reload(settings)

    def reload(self, settings: PluginSettings) -> None:
        self._whitelist = set(settings.whitelist)
        self._blacklist = set(settings.blacklist)
        self._interval = max(0, settings.debounce_interval)

    def check_session(self, session_id: str) -> str | None:
        """返回拒绝原因；放行时返回 None。白名单优先级高于黑名单。"""
        if self._whitelist and session_id not in self._whitelist:
            return "❌ 本会话不在白名单内，没有下载权限"
        if session_id in self._blacklist:
            return "❌ 本会话已被禁用下载"
        return None

    def is_duplicate(self, session_id: str, key: str) -> bool:
        """同会话内重复请求同一目标时返回 True（防抖间隔内）。"""
        if self._interval <= 0:
            return False

        now = time.time()
        bucket = self._cache.setdefault(session_id, {})
        expire = now - self._interval
        for existing, timestamp in list(bucket.items()):
            if timestamp < expire:
                bucket.pop(existing, None)

        if key in bucket:
            return True
        bucket[key] = now
        return False

    def forget(self, session_id: str, key: str) -> None:
        """任务失败时撤销防抖记录，让用户能立即重试。"""
        bucket = self._cache.get(session_id)
        if bucket is not None:
            bucket.pop(key, None)

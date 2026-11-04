"""群文件交付：把打包好的成品传进当前群的群文件。

上传成功后群里本来就会出现一条文件消息，因此命令层不再重复发一遍文件；本模块只负责
「找文件夹 → 必要时新建 → 上传 → 把成败与落点说清楚」，发送与清理仍归 `main.py`。

文件夹按配置的名字精确匹配（忽略大小写与首尾空白）。找不到时按配置决定是否新建：新建
成功就传进去，建不成（机器人不是管理员、协议端不支持）或配置不允许，就传群文件根目录
——用户要的是「有对应文件夹就进去，没有就放在外面」，新建只是让「有」这半边更容易成立。

文件夹 id 按 (群号, 文件夹名) 缓存在内存里：找一次用一辈子，避免每次发送都去列一遍目录。
缓存的 id 在 QQ 侧被删/改名后上传会失败，这一条失败会清掉缓存重找一次，不会一直撞墙。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..domain.constants import (
    GROUP_FILE_LIST_TIMEOUT,
    GROUP_FILE_UPLOAD_TIMEOUT,
    PLUGIN_NAME,
)

ROOT_LIST_ACTION = "get_group_root_files"
CREATE_FOLDER_ACTION = "create_group_file_folder"
UPLOAD_ACTION = "upload_group_file"

# 各协议端字段命名不一（NapCat 用 folder_id/folder_name，个别端用 id/name）
FOLDER_ID_KEYS = ("folder_id", "folderId", "id")
FOLDER_NAME_KEYS = ("folder_name", "folderName", "name")

ActionCaller = Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class GroupFileResult:
    """一次群文件上传的结果。`folder` 为空串表示落进了群文件根目录。"""

    ok: bool
    folder: str = ""
    created: bool = False
    error: str = ""

    @property
    def location(self) -> str:
        if not self.folder:
            return "群文件根目录"
        return f"群文件「{self.folder}」" + ("（新建）" if self.created else "")


class GroupFileUploader:
    """群文件上传器：持有文件夹 id 缓存，实例随插件存活（重载即清空）。"""

    def __init__(self) -> None:
        self._folders: dict[tuple[str, str], str] = {}

    async def upload(
        self,
        call_action: ActionCaller,
        group_id: str,
        path: Path,
        folder_name: str = "",
        create_folder: bool = True,
    ) -> GroupFileResult:
        """把 `path` 传到群 `group_id` 的群文件里。

        只有「文件已不存在」和「上传本身失败」算失败；文件夹相关的任何问题都降级为传根目录。
        """
        if not path.is_file():
            return GroupFileResult(False, error="文件已不存在，无法上传")

        wanted = (folder_name or "").strip()
        folder_id, folder_label, created = "", "", False
        cache_key = (group_id, wanted.casefold())
        from_cache = False

        if wanted:
            folder_id = self._folders.get(cache_key, "")
            from_cache = bool(folder_id)
            if folder_id:
                folder_label = wanted
            else:
                folder_id, folder_label, created = await self._prepare_folder(
                    call_action, group_id, wanted, create_folder
                )
                if folder_id:
                    self._folders[cache_key] = folder_id

        result = await self._upload_once(
            call_action, group_id, path, folder_id, folder_label, created
        )
        if result.ok or not from_cache:
            return result

        # 缓存的文件夹多半已在 QQ 侧被删或改名：清掉缓存重找一次，找不到就传根目录
        self._folders.pop(cache_key, None)
        logger.info(f"[{PLUGIN_NAME}] 群文件夹「{wanted}」已失效，重新确认一次")
        folder_id, folder_label, created = await self._prepare_folder(
            call_action, group_id, wanted, create_folder
        )
        if folder_id:
            self._folders[cache_key] = folder_id
        return await self._upload_once(
            call_action, group_id, path, folder_id, folder_label, created
        )

    # ---- 文件夹 ----

    async def _prepare_folder(
        self, call_action: ActionCaller, group_id: str, name: str, create: bool
    ) -> tuple[str, str, bool]:
        """确定要传进哪个文件夹，返回 (folder_id, 文件夹名, 是否本次新建)。"""
        found = await self._find_folder(call_action, group_id, name)
        if found is not None:
            return found[0], found[1], False

        if not create:
            logger.info(f"[{PLUGIN_NAME}] 群文件里没有「{name}」文件夹，改传群文件根目录")
            return "", "", False

        created = await self._create_folder(call_action, group_id, name)
        if created is not None:
            logger.info(f"[{PLUGIN_NAME}] 已在群文件里新建文件夹「{created[1]}」")
            return created[0], created[1], True

        logger.info(f"[{PLUGIN_NAME}] 没能新建群文件夹「{name}」，改传群文件根目录")
        return "", "", False

    async def _find_folder(
        self, call_action: ActionCaller, group_id: str, name: str
    ) -> tuple[str, str] | None:
        """在群文件根目录里找同名文件夹，返回 (folder_id, 实际文件夹名)。"""
        folders = await self._list_root_folders(call_action, group_id)
        if folders is None:
            return None
        target = name.casefold()
        for folder in folders:
            folder_name = _first_str(folder, FOLDER_NAME_KEYS)
            if folder_name.casefold() != target:
                continue
            folder_id = _first_str(folder, FOLDER_ID_KEYS)
            if not folder_id:
                # 没有 id 就没法把文件指定进去，等同于没找到这个文件夹
                logger.warning(
                    f"[{PLUGIN_NAME}] 群文件夹「{folder_name}」缺少 folder_id，改传群文件根目录"
                )
                return None
            return folder_id, folder_name
        return None

    async def _create_folder(
        self, call_action: ActionCaller, group_id: str, name: str
    ) -> tuple[str, str] | None:
        """建一个群文件夹；机器人不是管理员或协议端不支持时返回 None。"""
        params: dict[str, Any] = {
            "group_id": _as_group_id(group_id),
            "name": name,
        }
        try:
            resp = await asyncio.wait_for(
                call_action(CREATE_FOLDER_ACTION, **params),
                timeout=GROUP_FILE_LIST_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[{PLUGIN_NAME}] 新建群文件夹超时，改传群文件根目录")
            return None
        except Exception as exc:  # 非管理员、协议端不认这个动作都走这里
            logger.warning(f"[{PLUGIN_NAME}] 新建群文件夹失败，改传群文件根目录: {exc}")
            return None

        failure = _failure_text(resp)
        if failure:
            logger.warning(f"[{PLUGIN_NAME}] 新建群文件夹失败，改传群文件根目录: {failure}")
            return None

        folder_id = _first_str(_payload(resp), FOLDER_ID_KEYS)
        if folder_id:
            return folder_id, name
        # 有的端只回成功不回 id：重新列一次目录把它捞出来（QQ 侧建好即刻可见）
        return await self._find_folder(call_action, group_id, name)

    async def _list_root_folders(
        self, call_action: ActionCaller, group_id: str
    ) -> list[Mapping[str, Any]] | None:
        """列群文件根目录；只要文件夹清单，不需要按文件数分页的端专属参数。"""
        params: dict[str, Any] = {"group_id": _as_group_id(group_id)}
        try:
            resp = await asyncio.wait_for(
                call_action(ROOT_LIST_ACTION, **params), timeout=GROUP_FILE_LIST_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(f"[{PLUGIN_NAME}] 读取群文件目录超时，改传群文件根目录")
            return None
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取群文件目录失败，改传群文件根目录: {exc}")
            return None

        payload = _payload(resp)
        for key in ("folders", "folder_list", "folderList"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        return []

    # ---- 上传 ----

    async def _upload_once(
        self,
        call_action: ActionCaller,
        group_id: str,
        path: Path,
        folder_id: str,
        folder_label: str,
        created: bool,
    ) -> GroupFileResult:
        params: dict[str, Any] = {
            "group_id": _as_group_id(group_id),
            "file": str(path),
            "name": path.name,
        }
        if folder_id:
            params["folder"] = folder_id

        try:
            resp = await asyncio.wait_for(
                call_action(UPLOAD_ACTION, **params), timeout=GROUP_FILE_UPLOAD_TIMEOUT
            )
        except asyncio.TimeoutError:
            return GroupFileResult(
                False,
                folder=folder_label,
                created=created,
                error=f"上传超过 {int(GROUP_FILE_UPLOAD_TIMEOUT)}s 未完成",
            )
        except Exception as exc:  # 协议端拒绝、路径读不到、权限不足都在这里
            logger.warning(f"[{PLUGIN_NAME}] 上传群文件失败: {exc}")
            return GroupFileResult(
                False, folder=folder_label, created=created, error=_describe(exc)
            )

        failure = _failure_text(resp)
        if failure:
            logger.warning(f"[{PLUGIN_NAME}] 上传群文件失败: {failure}")
            return GroupFileResult(
                False, folder=folder_label, created=created, error=failure
            )
        return GroupFileResult(True, folder=folder_label, created=created)


def _payload(resp: Any) -> Mapping[str, Any]:
    if not isinstance(resp, Mapping):
        return {}
    data = resp.get("data")
    if isinstance(data, Mapping):
        nested = data.get("data")
        return nested if isinstance(nested, Mapping) else data
    return resp


def _first_str(item: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return ""


def _failure_text(resp: Any) -> str:
    """有的协议端不抛异常，只在响应里标失败。"""
    if not isinstance(resp, Mapping):
        return ""
    status = str(resp.get("status", "")).strip().lower()
    retcode = resp.get("retcode")
    failed = status in {"failed", "error"} or (
        isinstance(retcode, int) and not isinstance(retcode, bool) and retcode != 0
    )
    if not failed:
        return ""
    wording = resp.get("wording") or resp.get("msg") or resp.get("message")
    if isinstance(wording, str) and wording.strip():
        return wording.strip()
    return f"协议端返回失败（retcode={retcode}）"


def _as_group_id(group_id: str) -> Any:
    """QQ 群号在多数协议端要数字，非数字的原样透传（交给协议端报错）。"""
    try:
        return int(group_id)
    except (TypeError, ValueError):
        return group_id


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__

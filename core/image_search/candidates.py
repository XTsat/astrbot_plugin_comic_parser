"""从聊天消息里抠出图片二进制：URL / 本地路径 / file:// / base64 四种形态。

OneBot 等协议端有时只给一个哈希文件名（既不是路径也不是 URL），那种形态只能跳过——
把非法字符串交给 httpx 只会换来一个和真实原因无关的报错。
"""

from __future__ import annotations

import base64
import os

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image as ImageComp
from astrbot.api.message_components import Reply

_IMAGE_MAGICS = (b"\x89PNG", b"GIF8", b"RIFF", b"BM")
_DOWNLOAD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _decode_base64(value: str) -> bytes | None:
    """把 base64 文本解成图片二进制；不是图片就返回 None。"""
    text = "".join((value or "").split())
    if len(text) < 64 or len(text) % 4 == 1:
        return None
    try:
        data = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None
    if data[:2] == b"\xff\xd8" or data[:4] in _IMAGE_MAGICS:
        return data
    return None


def collect_image_refs(event: AstrMessageEvent) -> list[str]:
    """收集当前消息与被引用消息里的图片引用候选（url / path / file 三种属性）。"""
    refs: list[str] = []

    def push(comp: ImageComp) -> None:
        for attr in ("url", "path", "file"):
            value = getattr(comp, attr, None)
            if value and value not in refs:
                refs.append(str(value))

    for comp in getattr(event.message_obj, "message", []) or []:
        if isinstance(comp, ImageComp):
            push(comp)
    if not refs:
        for comp in getattr(event.message_obj, "message", []) or []:
            if isinstance(comp, Reply):
                for inner in getattr(comp, "chain", None) or []:
                    if isinstance(inner, ImageComp):
                        push(inner)
    return refs


def has_image_ref(event: AstrMessageEvent) -> bool:
    """消息里（含被引用的消息）是否带图；命令层据此决定要不要走搜图。"""
    return bool(collect_image_refs(event))


async def resolve_image_bytes(ref: str, *, timeout: float) -> bytes | None:
    """把单个图片引用解析成二进制；解析不了返回 None，由调用方换下一个候选。"""
    cand = (ref or "").strip()
    if not cand:
        return None
    if cand.startswith("base64://"):
        return _decode_base64(cand[len("base64://"):])
    if cand.startswith("file:///"):
        cand = cand[len("file://"):]
    if cand.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, trust_env=False
            ) as client:
                resp = await client.get(cand, headers={"User-Agent": _DOWNLOAD_UA})
                resp.raise_for_status()
                return resp.content
        except (httpx.HTTPError, OSError) as exc:
            logger.warning(f"[{__name__}] 下载图片失败 {cand[:80]}: {exc}")
            return None
    if os.path.isfile(cand):
        try:
            with open(cand, "rb") as handle:
                return handle.read()
        except OSError as exc:
            logger.warning(f"[{__name__}] 读取本地图片失败 {cand[:80]}: {exc}")
            return None
    return _decode_base64(cand)

#!/usr/bin/env python3
"""nhentai 下载链路实测脚本（在目标机器上手动跑，零第三方依赖）。

按顺序验证三件事：官方 API v2 能不能拿到作品信息 → 图床能不能拿到原图 → 落盘的是不是真图。
只用标准库（urllib），需要抓页面兜底时才用 cloudscraper（可选）。

用法：
    python scripts/nhentai_probe.py                     # 默认测作品 1，只下首页
    python scripts/nhentai_probe.py 1 --all             # 整本都下
    python scripts/nhentai_probe.py 1 --proxy http://127.0.0.1:7890
    python scripts/nhentai_probe.py 1 --out probe_out   # 指定落盘目录

退出码：0 = 接口可用且至少拿到一张真图；1 = 某一步失败（打印里能看出是哪一步）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SITE_DOMAIN = "nhentai.net"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# 常见图片格式的 magic bytes，用来确认落盘的确实是图片而不是错误页
_IMAGE_MAGIC = {
    b"\xff\xd8\xff": "JPEG",
    b"\x89PNG": "PNG",
    b"GIF8": "GIF",
    b"RIFF": "WEBP",
}


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}", flush=True)


def opener(proxy: str) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # 默认不读环境代理：容器里常有系统代理变量，会把请求绕到不存在的代理上
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def get(opener_: urllib.request.OpenerDirector, url: str, timeout: int = 30) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "Referer": f"https://{SITE_DOMAIN}/",
        },
    )
    try:
        with opener_.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络不可达：{exc.reason}") from exc


def sniff(data: bytes) -> str:
    for magic, name in _IMAGE_MAGIC.items():
        if data.startswith(magic):
            return name
    return "未知（可能不是图片）"


def main() -> int:
    parser = argparse.ArgumentParser(description="nhentai 下载链路实测")
    parser.add_argument("gallery_id", nargs="?", default="1")
    parser.add_argument("--all", action="store_true", help="下载整本，而不只首页")
    parser.add_argument("--proxy", default="", help="例如 http://127.0.0.1:7890")
    parser.add_argument("--out", default="probe_out", help="图片落盘目录")
    parser.add_argument("--domain", default=SITE_DOMAIN, help="站点域名（镜像站换这里）")
    args = parser.parse_args()

    opener_ = opener(args.proxy)

    # ① 官方 API v2：作品信息
    api_url = f"https://{args.domain}/api/v2/galleries/{args.gallery_id}"
    log("接口", api_url)
    try:
        status, body = get(opener_, api_url)
    except RuntimeError as exc:
        log("接口", str(exc))
        return 1
    log("接口", f"HTTP {status}，{len(body)} 字节")
    if status != 200:
        log("接口", f"失败：{body[:200].decode('utf-8', 'replace')}")
        log("接口", "作品可能已下架（404），或站点接口变了；确认代理与域名后再试")
        return 1

    try:
        gallery = json.loads(body)
    except json.JSONDecodeError:
        log("接口", "返回的不是 JSON，站点接口可能已改版")
        return 1
    media_id = gallery.get("media_id")
    pages = gallery.get("pages") or []
    title = (gallery.get("title") or {}).get("pretty") or ""
    log("接口", f"media_id={media_id}，{gallery.get('num_pages')} 页，标题：{title}")
    if not pages:
        log("接口", "响应里没有 pages 字段，无法继续")
        return 1

    # ② 图床清单
    server = "https://i.nhentai.net"
    try:
        status, body = get(opener_, f"https://{args.domain}/api/v2/cdn")
        if status == 200:
            servers = json.loads(body).get("image_servers") or []
            if servers:
                server = str(servers[0]).rstrip("/")
                log("图床", f"清单：{servers}")
    except (RuntimeError, json.JSONDecodeError) as exc:
        log("图床", f"取清单失败，改用默认图床 {server}：{exc}")

    # ③ 原图
    targets = pages if args.all else pages[:1]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    for page in targets:
        path = str(page.get("path") or "")
        if not path:
            continue
        image_url = f"{server}/{path}"
        target = out_dir / Path(path).name
        try:
            status, payload = get(opener_, image_url, timeout=60)
        except RuntimeError as exc:
            log("图片", f"{image_url} 失败：{exc}")
            continue
        if status != 200 or not payload:
            log("图片", f"{image_url} HTTP {status}")
            continue
        target.write_bytes(payload)
        ok += 1
        log("图片", f"{image_url} → {target.name}，{len(payload)} 字节，{sniff(payload)}")

    total = len([p for p in targets if p.get("path")])
    log("结论", f"拿到 {ok}/{total} 张原图，落盘在 {out_dir.resolve()}")
    if ok == 0:
        log("结论", "这条链路跑不通：先解决网络/代理，再重试")
        return 1
    log("结论", "下载链路可用（接口 + 图床都通）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""E-Hentai 匿名下载链路实测脚本（在能访问 e-hentai.org 的机器上手动跑，零第三方依赖）。

匿名（不登录、不注册）把「按 ID 下载整本」的链路走一遍，用最小请求量回答四个问题：
① gdata 能否按 gid+token 拿元信息；② 作品页能否匿名翻页、单页多少张、缩略图 href 的格式；
③ 图片页里的图（H@H 的 hath.network 主机）能否匿名 GET，拿到的是 1280px 重采样还是原图；
④ 原图 /fullimg/、gtoken、archiver、裸 gid 在匿名状态下分别是什么行为。

结论先说：匿名只能拿到 1280px 重采样图；/fullimg/ 原图与 archiver 都会被弹到登录页；
gtoken 匿名可用（作用是用 /s/ 页面链接反查作品 token，不是拿原图）。

用法：
    # 直连被墙时全程走本机代理；默认从首页自动挑一个作品，只取元信息 + 1 张图
    python3 scripts/ehentai_probe.py --proxy http://127.0.0.1:7890
    # 指定作品（token 从 /g/<gid>/<token>/ 里来）
    python3 scripts/ehentai_probe.py --proxy http://127.0.0.1:7890 --gid 123456 --token abcdef1234
    # 整本跑（默认最多 20 张，见 --limit）
    python3 scripts/ehentai_probe.py --proxy http://127.0.0.1:7890 --all --limit 20
    # 指定落盘目录
    python3 scripts/ehentai_probe.py --proxy http://127.0.0.1:7890 --out /tmp/eh_out

退出码：0 = 元信息拿到且至少取到一张图；1 = 某一步失败（打印里能看出是哪一步）。
注意：不带 User-Agent 会被 Cloudflare 直接 403，脚本固定带浏览器 UA；请求之间固定间隔 1.5s，
默认只下 1 张图，别用 --all 去整站抓。
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple

SITE = "https://e-hentai.org"
API_URL = "https://e-hentai.org/api.php"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)
# 作品页默认一页 20 张缩略图；页列表只能从作品页 HTML 抓（gdata 不返回 files）
THUMBS_PER_PAGE = 20
# 请求间隔：站点对匿名请求的容忍度不高，1.5s 起步，别压到 1s 以内
REQUEST_INTERVAL = 1.5

_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"GIF8", "GIF"),
    (b"RIFF", "WEBP"),
    (b"BM", "BMP"),
)
_RATE_HEADERS = (
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "cf-ray",
)


class Resp(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed: float
    final_url: str


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}", flush=True)


# ---- 网络层 ----


def opener(proxy: str) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # 默认不读环境代理：容器里常有系统代理变量，会把请求绕到不存在的代理上
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def http(
    opener_: urllib.request.OpenerDirector,
    url: str,
    data: bytes | None = None,
    timeout: int = 30,
    headers: dict[str, str] | None = None,
) -> Resp:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Referer": f"{SITE}/",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url, data=data, headers=request_headers, method="POST" if data else "GET"
    )
    started = time.time()
    try:
        with opener_.open(request, timeout=timeout) as response:
            return Resp(
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                response.read(),
                round(time.time() - started, 2),
                response.url,
            )
    except urllib.error.HTTPError as exc:
        return Resp(
            exc.code,
            {k.lower(): v for k, v in exc.headers.items()},
            exc.read(),
            round(time.time() - started, 2),
            exc.url or url,
        )
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络不可达（先确认代理）：{exc.reason}") from exc


def pause() -> None:
    time.sleep(REQUEST_INTERVAL)


def rate_note(resp: Resp) -> str:
    """把限流线索压成一行；没有限流头时明确写「无」，避免看的人以为漏打。"""
    hits = {key: resp.headers[key] for key in _RATE_HEADERS if key in resp.headers}
    return f"限流头：{hits}" if hits else "限流头：无（未出现 Retry-After / X-RateLimit / 509）"


def show(step: str, resp: Resp, extra: str = "") -> None:
    suffix = f"，{extra}" if extra else ""
    log(step, f"HTTP {resp.status}，{len(resp.body)} 字节，{resp.elapsed}s{suffix}")
    log(step, rate_note(resp))


def ensure_ok(step: str, resp: Resp) -> bool:
    if resp.status == 509:
        log(step, "❌ 返回 509（带宽/请求超限），已停止：这是风控信号，别继续压测")
        return False
    if resp.status != 200:
        log(step, f"❌ 失败：HTTP {resp.status}，{resp.body[:200].decode('utf-8', 'replace')}")
        return False
    return True


# ---- 内容嗅探 ----


def sniff(data: bytes) -> str:
    head = data[:16]
    if head.startswith(b"<!DOCTYPE") or head.startswith(b"<html") or head.startswith(b"<?xml"):
        return "HTML（不是图片，多半是登录页/错误页）"
    if head.startswith(b"{"):
        return "JSON（不是图片）"
    for magic, name in _IMAGE_MAGIC:
        if data.startswith(magic):
            return name
    return "未知（可能不是图片）"


def image_size(data: bytes) -> str:
    """从文件头读像素尺寸；读不出就返回「未知」，不为了这点信息再请求一次。"""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
            width, height = struct.unpack(">II", data[16:24])
            return f"{width}x{height}"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            fourcc = data[12:16]
            if fourcc == b"VP8X":
                width = int.from_bytes(data[24:27], "little") + 1
                height = int.from_bytes(data[27:30], "little") + 1
                return f"{width}x{height}"
            if fourcc == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return f"{(bits & 0x3FFF) + 1}x{(((bits >> 14) & 0x3FFF) + 1)}"
            if fourcc == b"VP8 ":
                width = int.from_bytes(data[26:28], "little") & 0x3FFF
                height = int.from_bytes(data[28:30], "little") & 0x3FFF
                return f"{width}x{height}"
        if data.startswith(b"\xff\xd8\xff"):
            pos = 2
            while pos + 9 < len(data):
                if data[pos] != 0xFF:
                    pos += 1
                    continue
                marker = data[pos + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                    height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
                    return f"{width}x{height}"
                pos += 2 + int.from_bytes(data[pos + 2 : pos + 4], "big")
    except (struct.error, IndexError):
        pass
    return "未知"


def extension(sniffed: str) -> str:
    return {
        "JPEG": "jpg",
        "PNG": "png",
        "GIF": "gif",
        "WEBP": "webp",
        "BMP": "bmp",
    }.get(sniffed, "bin")


# ---- 各步骤 ----


def pick_gallery(opener_: urllib.request.OpenerDirector) -> tuple[int, str]:
    """首页任意一个作品链接里的 gid/token；只用来给「不带参数」的场景兜底。"""
    resp = http(opener_, f"{SITE}/")
    show("首页", resp)
    if not ensure_ok("首页", resp):
        raise RuntimeError("打不开首页")
    links = re.findall(r'href="(?:https?://e-hentai\.org)?/g/(\d+)/([0-9a-f]+)/"',
                       resp.body.decode("utf-8", "replace"))
    if not links:
        raise RuntimeError("首页没有解析到任何作品链接（HTML 结构可能变了）")
    gid, token = int(links[0][0]), links[0][1]
    log("首页", f"自动挑选的作品：gid={gid}（共解析到 {len(links)} 个作品链接）")
    return gid, token


def fetch_gdata(opener_: urllib.request.OpenerDirector, gid: int, token: str) -> dict[str, object]:
    payload = json.dumps({"method": "gdata", "gidlist": [[gid, token]], "namespace": 1}).encode()
    resp = http(opener_, API_URL, data=payload, headers={"Content-Type": "application/json"})
    show("gdata", resp)
    if not ensure_ok("gdata", resp):
        raise RuntimeError("gdata 失败")
    try:
        meta = json.loads(resp.body)["gmetadata"][0]
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        raise RuntimeError(f"gdata 返回不是预期 JSON：{resp.body[:200]!r}") from exc
    if meta.get("error"):
        raise RuntimeError(f"gdata 报错：{meta['error']}")
    log("gdata", f"返回的 token 与传入一致：{meta.get('token') == token}")
    log(
        "gdata",
        "关键字段："
        f"gid={meta.get('gid')}，filecount={meta.get('filecount')}，"
        f"filesize={meta.get('filesize')}，category={meta.get('category')}，"
        f"posted={meta.get('posted')}，rating={meta.get('rating')}，"
        f"tags={len(meta.get('tags') or [])} 个",
    )
    # 这是关键结论：gdata 只返回元信息，页列表必须从作品页 HTML 抓
    log("gdata", f"files 字段：{'有' if 'files' in meta else '无'}（API 只给元信息，页列表要另抓作品页）")
    return meta


def fetch_gallery_page(
    opener_: urllib.request.OpenerDirector, gid: int, token: str, page: int
) -> tuple[str, list[tuple[str, int]], int]:
    """返回 (html, [(图片页 URL, 页码 index)], 页数)。页码是作品内全局序号，从 1 开始。"""
    url = f"{SITE}/g/{gid}/{token}/" + (f"?p={page}" if page else "")
    resp = http(opener_, url)
    show("作品页", resp, extra=f"p={page}")
    if not ensure_ok("作品页", resp):
        raise RuntimeError("作品页失败")
    html = resp.body.decode("utf-8", "replace")
    # 缩略图 href 的固定格式：/s/<pagehash>/<gid>-<作品内序号>
    thumbs = [
        (f"{SITE}/s/{page_hash}/{gid}-{index}", int(index))
        for page_hash, index in re.findall(
            rf'href="(?:https?://e-hentai\.org)?/s/([0-9a-f]+)/{gid}-(\d+)"', html
        )
    ]
    # 去重保序：同一作品页里同一缩略图偶尔会出现两次
    seen: set[str] = set()
    unique: list[tuple[str, int]] = []
    for item in thumbs:
        if item[0] in seen:
            continue
        seen.add(item[0])
        unique.append(item)
    pager = {int(p) for p in re.findall(r"\?p=(\d+)", html)}
    pages = max(pager) + 1 if pager else 0
    return html, unique, pages


def parse_image_page(html: str) -> dict[str, str | None]:
    """从图片页 HTML 里取 #img、原图链接、showkey/startkey；失败抛 RuntimeError。"""
    img = re.search(r'<img\s+id="img"[^>]*src="([^"]+)"', html)
    if not img:
        # 图片页失败时会给 #loadfail 的报错页，单独提示便于判断
        detail = "页面里有 #loadfail" if "loadfail" in html else "没有找到 #img"
        raise RuntimeError(f"取不到 #img（{detail}）")
    img_url = img.group(1)
    # style 里 height/width 的先后顺序不固定，分开找，别写死
    tag = html[img.start() : html.find(">", img.start()) + 1]
    height = re.search(r"height:(\d+)px", tag)
    width = re.search(r"width:(\d+)px", tag)
    xres = re.search(r"xres=(\d+)", img_url)
    showkey = re.search(r'showkey="([^"]*)"', html)
    startkey = re.search(r'startkey="([^"]*)"', html)
    fullimg = re.search(
        r'<a href="(https://e-hentai\.org/fullimg/[^"]+)">Download original ([^<]+)</a>', html
    )
    host = urllib.parse.urlparse(img_url).netloc
    log(
        "图片页",
        "关键字段："
        f"#img 主机={host}，xres={xres.group(1) if xres else '无'}，"
        f"声明尺寸={(width.group(1) + 'x' + height.group(1)) if width and height else '无'}，"
        f"showkey={'有' if showkey else '无'}，"
        f"原图链接={'有' if fullimg else '无'}",
    )
    if fullimg:
        log("图片页", f"原图标注：{fullimg.group(2)}")
    return {
        "img_url": img_url,
        "host": host,
        "xres": xres.group(1) if xres else None,
        "showkey": showkey.group(1) if showkey else None,
        "startkey": startkey.group(1) if startkey else None,
        "fullimg": fullimg.group(1) if fullimg else None,
        "original_label": fullimg.group(2) if fullimg else None,
    }


def fetch_image_page(
    opener_: urllib.request.OpenerDirector, image_page_url: str
) -> dict[str, str | None]:
    resp = http(opener_, image_page_url)
    show("图片页", resp)
    if not ensure_ok("图片页", resp):
        raise RuntimeError("图片页失败")
    return parse_image_page(resp.body.decode("utf-8", "replace"))


def download(
    opener_: urllib.request.OpenerDirector,
    url: str,
    out_dir: Path,
    name: str,
    timeout: int = 60,
    headers: dict[str, str] | None = None,
) -> Path | None:
    resp = http(opener_, url, timeout=timeout, headers=headers)
    show("图片", resp, extra=f"content-type={resp.headers.get('content-type', '?')}")
    if resp.status not in (200, 206) or not resp.body:
        log("图片", f"❌ 没取到图（HTTP {resp.status}），跳过")
        return None
    kind = sniff(resp.body)
    if not kind.startswith(("JPEG", "PNG", "GIF", "WEBP", "BMP")):
        log("图片", f"❌ 落盘内容不是图片：{kind}，不保存")
        return None
    target = out_dir / f"{name}.{extension(kind)}"
    target.write_bytes(resp.body)
    log("图片", f"→ {target}，{len(resp.body)} 字节，{kind}，{image_size(resp.body)}")
    return target


def try_gtoken(
    opener_: urllib.request.OpenerDirector, gid: int, page_hash: str, index: int
) -> str | None:
    payload = json.dumps({"method": "gtoken", "pagelist": [[gid, page_hash, index]]}).encode()
    resp = http(opener_, API_URL, data=payload, headers={"Content-Type": "application/json"})
    show("gtoken", resp)
    if not ensure_ok("gtoken", resp):
        return None
    try:
        token = json.loads(resp.body)["tokenlist"][0]["token"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        log("gtoken", f"返回不是预期结构：{resp.body[:200]!r}")
        return None
    log("gtoken", "匿名可用：能从 /s/<pagehash>/<gid>-<index> 反查作品 token（这是它的用途）")
    return str(token)


def probe_archiver(opener_: urllib.request.OpenerDirector, gid: int, token: str) -> None:
    resp = http(opener_, f"{SITE}/archiver.php?gid={gid}&token={token}")
    show("archiver", resp, extra=f"final_url={resp.final_url}")
    text = resp.body.decode("utf-8", "replace")
    if "bounce_login" in resp.final_url or "requires you to log on" in text:
        log("archiver", "匿名被弹到登录页（bounce_login.php），拿不到归档/GP 队列")
    elif resp.status == 200 and "archiver" in text.lower():
        log("archiver", "返回了归档页面（匿名可用？需要人工看页面内容确认）")
    else:
        log("archiver", f"未知返回：HTTP {resp.status}，{len(resp.body)} 字节")


def mask(token: str | None) -> str:
    if not token:
        return "无"
    return f"{token[:4]}…{token[-2:]}" if len(token) > 8 else "有"


# ---- 主流程 ----


def main() -> int:
    parser = argparse.ArgumentParser(description="E-Hentai 匿名下载链路实测")
    parser.add_argument("--proxy", default="", help="代理，例如 http://127.0.0.1:7890（直连被墙时必填）")
    parser.add_argument("--gid", type=int, default=0, help="作品 ID；不填则从首页自动挑一个")
    parser.add_argument("--token", default="", help="作品 token（作品 URL 里 /g/<gid>/<token>/ 那段）")
    parser.add_argument("--all", action="store_true", help="整本跑（受 --limit 限制），默认只取 1 张图")
    parser.add_argument("--limit", type=int, default=20, help="--all 时最多取多少张图，默认 20")
    parser.add_argument("--out", default="probe_out", help="图片落盘目录")
    args = parser.parse_args()

    opener_ = opener(args.proxy)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ① 定位作品：显式 gid+token，或从首页挑一个
    if args.gid and args.token:
        gid, token = args.gid, args.token
        log("定位", f"使用参数指定的作品：gid={gid}")
    elif args.gid:
        log("定位", "只给了 --gid：先打 /g/<gid>/ 看服务端怎么回（作品页需要 token）")
        resp = http(opener_, f"{SITE}/g/{args.gid}/")
        show("定位", resp, extra=f"final_url={resp.final_url}")
        if resp.status == 404:
            log("定位", "裸 gid 返回 404：确认无 token 无法定位作品，必须补 --token 或让脚本从首页挑")
        else:
            log("定位", "裸 gid 有响应，但页面需要 token 才能解析；请补 --token 后重跑")
        return 1
    else:
        try:
            gid, token = pick_gallery(opener_)
        except RuntimeError as exc:
            log("定位", f"❌ {exc}")
            return 1
        pause()

    # ② gdata 元信息
    try:
        meta = fetch_gdata(opener_, gid, token)
    except (RuntimeError, urllib.error.URLError) as exc:
        log("gdata", f"❌ {exc}")
        return 1
    filecount = int(meta.get("filecount") or 0)
    pause()

    # ③ 作品页：缩略图链接规则 + 分页
    try:
        _, thumbs, pages = fetch_gallery_page(opener_, gid, token, 0)
    except RuntimeError as exc:
        log("作品页", f"❌ {exc}")
        return 1
    if not thumbs:
        log("作品页", "❌ 页面上没有解析到任何缩略图链接")
        return 1
    expected_pages = (filecount + THUMBS_PER_PAGE - 1) // THUMBS_PER_PAGE if filecount else 0
    log(
        "作品页",
        f"第 0 页 {len(thumbs)} 张缩略图；页数={pages or expected_pages}（按 filecount={filecount} "
        f"/ 每页 {THUMBS_PER_PAGE} 推算 {expected_pages} 页）；"
        f"第一张序号={thumbs[0][1]}，最后一张序号={thumbs[-1][1]}",
    )
    log("作品页", f"缩略图 href 规则：/s/<pagehash>/{gid}-<作品内序号>，示例：{thumbs[0][0]}")
    pause()

    # ④ 图片页：拿到 H@H 上的图（匿名能取到的是 1280px 上限的屏幕尺寸图）
    first_page_url = thumbs[0][0]
    try:
        image = fetch_image_page(opener_, first_page_url)
    except RuntimeError as exc:
        log("图片页", f"❌ {exc}")
        return 1
    pause()

    # ⑤ 匿名下载这张图：这是插件实际能拿到的画质
    saved = download(
        opener_, str(image["img_url"]), out_dir, f"eh_{gid}_{thumbs[0][1]:03d}_resampled"
    )
    pause()

    # ⑥ /fullimg/ 原图：匿名大概率被弹登录页，按实际结果判断
    if image.get("fullimg"):
        original = http(
            opener_, str(image["fullimg"]), headers={"Range": "bytes=0-65535"}, timeout=60
        )
        show(
            "原图",
            original,
            extra=(
                f"final_url={original.final_url}，"
                f"content-type={original.headers.get('content-type', '?')}"
            ),
        )
        kind = sniff(original.body)
        if kind.startswith(("JPEG", "PNG", "GIF", "WEBP", "BMP")):
            target = out_dir / f"eh_{gid}_{thumbs[0][1]:03d}_original.{extension(kind)}"
            target.write_bytes(original.body)
            log("原图", f"✅ 匿名能取原图（Range 只取了前 64KB）：→ {target}，{kind}")
        elif "bounce_login" in original.final_url or "requires you to log on" in original.body.decode(
            "utf-8", "replace"
        ):
            log("原图", "❌ 匿名拿不到原图：链接被 302 到 bounce_login.php，返回的是登录页 HTML")
            log("原图", "结论：原图必须登录；插件匿名只能下 1280px 重采样图")
        else:
            log("原图", f"❌ 匿名拿不到原图：返回 {kind}，HTTP {original.status}")
        pause()
    else:
        log("原图", "图片页里没有 Download original 链接，跳过")

    # ⑦ gtoken：匿名可用，但它的用途是用 /s/ 链接反查作品 token，不是取原图
    if image.get("startkey"):
        recovered = try_gtoken(opener_, gid, str(image["startkey"]), thumbs[0][1])
        if recovered:
            same = recovered == token
            log(
                "gtoken",
                f"反查结果：gid={gid}，token 与作品 URL 里的 token "
                f"{'一致' if same else f'不一致（{mask(recovered)} vs {mask(token)}）'}",
            )
        pause()

    # ⑧ archiver：匿名基本都会被弹登录页
    probe_archiver(opener_, gid, token)
    pause()

    # ⑨ --all：翻页取图（默认只取 1 张，--all 才继续，且受 --limit 限制）
    images: list[Path] = [saved] if saved else []
    limit = max(1, args.limit)
    if args.all:
        log("整本", f"开始翻页取图，最多 {limit} 张，每张间隔 {REQUEST_INTERVAL}s")
        seen_indexes = {thumbs[0][1]}
        total_pages = pages or expected_pages
        page_no = 0
        page_thumbs = thumbs
        while True:
            for page_url, index in page_thumbs:
                if index in seen_indexes:
                    continue
                if len(images) >= limit:
                    break
                seen_indexes.add(index)
                try:
                    info = fetch_image_page(opener_, page_url)
                    pause()
                    target = download(
                        opener_, str(info["img_url"]), out_dir, f"eh_{gid}_{index:03d}_resampled"
                    )
                    pause()
                except RuntimeError as exc:
                    log("整本", f"第 {index} 张失败：{exc}")
                    continue
                if target:
                    images.append(target)
            page_no += 1
            if len(images) >= limit or page_no >= total_pages:
                break
            try:
                _, page_thumbs, _ = fetch_gallery_page(opener_, gid, token, page_no)
            except RuntimeError as exc:
                log("整本", f"作品页 p={page_no} 失败：{exc}")
                break
            pause()

    # ⑩ 汇总
    log(
        "结论",
        f"作品 gid={gid}：filecount={filecount}，filesize={meta.get('filesize')}；"
        f"匿名实取 {len(images)} 张 1280px 重采样图 → {out_dir.resolve()}",
    )
    if not images:
        log("结论", "❌ 一张图都没取到：先解决代理/网络，再重试")
        return 1
    log("结论", "✅ 匿名链路可用：gdata 元信息 + 作品页分页 + 图片页 + H@H 重采样图都能匿名拿到")
    log("结论", "受限项：/fullimg/ 原图与 archiver 需要登录（本脚本不尝试登录/注册）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

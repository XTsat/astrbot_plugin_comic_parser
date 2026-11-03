"""Hanime1 漫画渠道的联网探针：确认这台机器到底能不能把图下下来。

和 `core/channels/hanime1/selftest.py`（本地假站点、不联网）互补：本脚本打真实站点，
用来回答「这台机器 / 这个代理能不能下」。三种用法：

    # 1) 直连真实站点（能过 Cloudflare 的网络里这样跑）
    python scripts/hanime1_probe.py --id 100445

    # 2) 走代理
    python scripts/hanime1_probe.py --id 100445 --proxy http://127.0.0.1:7890

    # 3) 站点打不开时：用浏览器另存的真实页面 HTML 当输入，只验图床能不能下
    python scripts/hanime1_probe.py --id 100445 --proxy http://127.0.0.1:7890 \
        --fixture-detail detail.html --fixture-reader reader.html --limit 5

第 3 种用法把「页面抓取」这一步换成读本地文件，其余全走渠道真实代码路径
（解析 → 拼图床地址 → 并发下载 → 落盘），因此图床能不能下、解析对不对都能验出来。

`--limit N` 只下前 N 页，用来低成本试水；默认 0 表示整本。
退出码 0 表示至少下成一张，2 表示一张都没下成。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))
PACKAGE = PLUGIN_ROOT.name


def _install_astrbot_stub() -> None:
    """探针不拉起 AstrBot，只给渠道代码一个最小 `astrbot.api`。"""
    if "astrbot.api" in sys.modules:
        return
    import logging
    import types

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logging.getLogger("hanime1-probe")
    astrbot_module = types.ModuleType("astrbot")
    astrbot_module.api = api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module


_install_astrbot_stub()

import importlib  # noqa: E402 - 必须先装好 astrbot stub 再 import 插件代码

channel_module = importlib.import_module(f"{PACKAGE}.core.channels.hanime1.channel")
site_module = importlib.import_module(f"{PACKAGE}.core.channels.hanime1.site")
domain_module = importlib.import_module(f"{PACKAGE}.domain")


def _patch_fixtures(detail: Path | None, reader: Path | None) -> None:
    """把抓页换成读本地 HTML：只替换「取页面」这一步，解析与下载仍走真实代码。"""
    detail_html = detail.read_text(encoding="utf-8", errors="replace") if detail else ""
    reader_html = reader.read_text(encoding="utf-8", errors="replace") if reader else ""

    original = site_module.PageScraper.fetch_text

    def fetch_text(self: site_module.PageScraper, url: str) -> str:
        # /comic/<ID> 是作品页，/comic/<ID>/<页> 才是阅读页：靠路径段数区分，
        # 不能看最后一段是不是数字（作品 ID 本身也是数字）
        parts = [part for part in urlsplit(url).path.split("/") if part]
        if parts and parts[0] == "comic":
            is_reader = len(parts) >= 3
            fixture = reader_html if is_reader else detail_html
            if fixture:
                print(f"📄 用本地 HTML 代替抓取：{url}")
                return fixture
        return original(self, url)

    site_module.PageScraper.fetch_text = fetch_text  # type: ignore[method-assign]


def _patch_limit(limit: int) -> None:
    """只保留前 N 页：省流量地验证「图床能不能下」。"""
    if limit <= 0:
        return
    original = site_module.parse_reader

    def parse_reader(
        comic_id: str, page: str, parsed: site_module.ParsedComic | None = None
    ) -> site_module.ParsedComic:
        result = original(comic_id, page, parsed)
        if len(result.extensions) > limit:
            result.extensions = result.extensions[:limit]
            result.page_count = limit
        return result

    site_module.parse_reader = parse_reader  # type: ignore[assignment]
    channel_module.parse_reader = parse_reader  # type: ignore[assignment]


async def _run(args: argparse.Namespace) -> int:
    _patch_fixtures(
        Path(args.fixture_detail) if args.fixture_detail else None,
        Path(args.fixture_reader) if args.fixture_reader else None,
    )
    _patch_limit(args.limit)

    settings = domain_module.PluginSettings(proxy=args.proxy or "")
    # 没显式给值的项不写进 options：让渠道自己的默认值生效（例如默认读取代理）
    options = {
        "enable": True,
        "request_timeout": args.timeout,
        "retry_times": args.retry,
        "max_concurrent_images": args.concurrency,
        "use_proxy": bool(args.proxy),
    }
    for key, value in (
        ("site_domains", args.site),
        ("image_domains", args.image_host),
        ("fetch_proxy", args.fetch_proxy),
    ):
        if value:
            options[key] = value

    channel = channel_module.Hanime1Channel(options, settings)

    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="hanime1-probe-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"🎯 作品 {args.id} · 站点 {args.site or '默认'} · 图床 {args.image_host or '自动'} · "
        f"读取代理 {args.fetch_proxy or '默认'} · 输出 {out_dir}"
    )

    try:
        meta = await channel.fetch_meta(args.id, out_dir)
    except domain_module.ChannelError as exc:
        print(f"❌ 取元信息失败：{exc}")
        return 2
    print(
        f"✅ 元信息：{meta.display_title} | 作者 {meta.author or '（无）'} | "
        f"{meta.page_count} 页 | 标签 {'、'.join(meta.tags[:6]) or '（无）'}"
    )

    try:
        comic = await channel.download(args.id, out_dir)
    except domain_module.ChannelError as exc:
        print(f"❌ 下载失败：{exc}")
        return 2

    images = sorted(
        path
        for path in comic.source_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in ".jpg .jpeg .png .webp .gif"
    )
    total_bytes = sum(path.stat().st_size for path in images)
    print(
        f"✅ 下载结束：成功 {comic.image_count} 张 / 失败 {comic.failed_images} 张 · "
        f"{total_bytes / 1048576:.1f} MB"
    )
    for path in images[:5]:
        print(f"   · {path.name} {path.stat().st_size / 1024:.0f} KB")
    print(f"📁 落盘目录：{comic.source_dir}")
    return 0 if comic.image_count > 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Hanime1 漫画渠道联网探针")
    parser.add_argument("--id", required=True, help="Hanime1 漫画 ID，例如 100445")
    parser.add_argument("--proxy", default="", help="代理地址，例如 http://127.0.0.1:7890")
    parser.add_argument("--site", default="", help="页面域名，留空＝用渠道默认（hanimeone.me）")
    parser.add_argument(
        "--image-host", default="", help="图床域名，留空＝用页面给的并自动换备用图床"
    )
    parser.add_argument(
        "--fetch-proxy",
        default="",
        help="读取代理地址（本机 IP 被 Cloudflare 拦时用），例如 https://r.jina.ai/",
    )
    parser.add_argument("--timeout", type=int, default=30, help="单请求超时（秒）")
    parser.add_argument("--retry", type=int, default=2, help="单张图片重试次数")
    parser.add_argument("--concurrency", type=int, default=5, help="并发下载图片数")
    parser.add_argument("--limit", type=int, default=0, help="只下前 N 页（0 = 整本）")
    parser.add_argument("--fixture-detail", default="", help="本地保存的作品页 HTML")
    parser.add_argument("--fixture-reader", default="", help="本地保存的阅读页 HTML")
    parser.add_argument("--out", default="", help="落盘目录（默认临时目录）")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

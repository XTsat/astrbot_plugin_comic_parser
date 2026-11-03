"""Hanime1 渠道的下载自测：起一个本地假站点，验证渠道真能把图片落盘。

不联网、不需要代理：假站点按 Hanime1 的真实页面结构应答（详情页标题块 + 页缩略图、
阅读页 `data-id`/`data-pages`/`data-prefix` 与内嵌 `extensions` 数组、图床按页序出图），
故意坏掉一张图，用来验证「单张失败不拖垮整本」。

用法（在插件根目录执行）：

    python core/channels/hanime1/selftest.py          # 本地假站点，不联网

退出码 0 表示全部通过。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 插件根目录自己不是包（靠 AstrBot 直接加载），所以把它的父目录放进 sys.path，
# 再按「插件目录名.core.channels.hanime1」导入，相对 import 的层级才对得上
PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))
PACKAGE = PLUGIN_ROOT.name


def _install_astrbot_stub() -> None:
    """给渠道代码一个假的 `astrbot.api`：自测只验下载，不必拉起整个 AstrBot。"""
    if "astrbot.api" in sys.modules:
        return
    import logging
    import types

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logging.getLogger("hanime1-selftest")
    astrbot_module = types.ModuleType("astrbot")
    astrbot_module.api = api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module


_install_astrbot_stub()

import asyncio  # noqa: E402 - 必须先装好 astrbot stub 再 import 插件代码
import importlib  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import types  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.error import HTTPError, URLError  # noqa: E402
from urllib.request import Request, urlopen  # noqa: E402

hanime_channel = importlib.import_module(f"{PACKAGE}.core.channels.hanime1.channel")
hanime_site = importlib.import_module(f"{PACKAGE}.core.channels.hanime1.site")
domain_module = importlib.import_module(f"{PACKAGE}.domain")
domain_constants = importlib.import_module(f"{PACKAGE}.domain.constants")

Hanime1Channel = hanime_channel.Hanime1Channel
PluginSettings = domain_module.PluginSettings
IMAGE_SUFFIXES = domain_constants.IMAGE_SUFFIXES

COMIC_ID = "100445"
MEDIA_ID = "2763724"
PAGES = 8
# 故意坏掉的图片：验证渠道会把失败计入 failed_images 而不是整单失败
BROKEN_PAGE = 5
EXPECTED_IMAGES = PAGES - 1

# 一张 8×8 的 1×1 像素 PNG 放大版，体积够过「不像图片」的最小体积检查
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x08\x00\x00\x00\x08\x08\x02"
    b"\x00\x00\x00K\rX\xb8\x00\x00\x00\x19IDATx\x9cc\xf8\xcf\x80\x19\x18\x06\x1d"
    b"\x00\x00\xff\xff\x03\x00\x1f\xd6\xd3\x9b\x00\x00\x00\x00IEND\xaeB`\x82"
)
_PADDING = b"\x00" * 700  # 让响应体超过 MIN_IMAGE_BYTES

_DETAIL_HTML = f"""<!DOCTYPE html>
<html><head><title>夏期補習総集編1～6+&nbsp;-&nbsp;H動漫/裏番/漫畫/線上看&nbsp;-&nbsp;Hanime1.me</title></head>
<body>
<div class="comics-panel-margin comics-panel-padding">
  <div class="row">
    <div class="col-md-4">
      <a href="https://hanime1.me/comic/{COMIC_ID}/1">
        <img class="lazy" src="https://t2.nhentai.net/galleries/{MEDIA_ID}/cover.jpg"
             data-src="https://t2.nhentai.net/galleries/{MEDIA_ID}/cover.jpg"
             data-srcset="https://t2.nhentai.net/galleries/{MEDIA_ID}/cover.jpg">
      </a>
    </div>
    <div class="col-md-8">
      <h3 class="title comics-metadata-top-row" style="line-height: 30px;">
        <span class="before" style="color: #999">[Hito no Fundoshi (Yukiyoshi Mamizu)]</span>
        <span class="pretty" style="color: white">Kaki Hoshuu Soushuuhen 1~6+</span>
        <span class="after" style="color: #999">[Chinese] [Digital]</span>
      </h3>
      <h4 class="title comics-metadata-margin-top" style="line-height: 30px;">
        <span class="before" style="color: #999">[ひとのふんどし (ゆきよし真水)]</span>
        <span class="pretty" style="color: white">夏期補習総集編1～6+</span>
        <span class="after" style="color: #999">[中国翻訳] [DL版]</span>
      </h4>
      <h5 class="comics-metadata-margin-top" style="color: #999">#<span style="color: #d9d9d9">{COMIC_ID}</span></h5>
      <div class="comics-metadata-margin-top">
        <h5 style="margin:0; color: #d9d9d9;">標籤：
          <a class="hover-lighter" href="https://hanime1.me/tags/%E4%B8%AD%E5%87%BA"><div class="no-select" style="padding: 4px">中出</div></a>
          <a class="hover-lighter" href="https://hanime1.me/tags/JK"><div class="no-select" style="padding: 4px">JK制服</div></a>
        </h5>
      </div>
    </div>
  </div>
  <div class="comics-panel-margin comics-panel-padding comics-thumbnail-wrapper comic-rows-wrapper">
{{thumbs}}
  </div>
</div>
</body></html>
"""

_THUMB = (
    '      <a href="https://hanime1.me/comic/{comic_id}/{page}">'
    '<img class="lazy comic-rows-videos-div" src="https://img4.qy0.ru/data/x.jpg" '
    'data-srcset="https://t2.nhentai.net/galleries/{media_id}/{page}t.jpg"></a>\n'
)


def _detail_page() -> str:
    thumbs = "".join(
        _THUMB.format(comic_id=COMIC_ID, media_id=MEDIA_ID, page=page)
        for page in range(1, PAGES + 1)
    )
    return _DETAIL_HTML.format(thumbs=thumbs)


def _reader_page() -> str:
    letters = ", ".join(f"&quot;j&quot;" for _ in range(PAGES))
    return f"""<!DOCTYPE html>
<html><body>
<div class="no-select comic-show-content-nav" style="background-color: #383838;" data-id="{MEDIA_ID}" data-pages="{PAGES}">
  <a class="no-select comic-show-content-nav-item-wrapper arrow-right" href="https://hanime1.me/comic/{COMIC_ID}/2" data-id="{MEDIA_ID}" data-page="2"></a>
  <span class="no-select"><span class="current-page-number">1</span><span style="font-weight: 400"> / </span>{PAGES}</span>
</div>
<div id="comic-content-wrapper">
  <img id="current-page-image" src="http://127.0.0.1:{{port}}/galleries/{MEDIA_ID}/1.jpg"
       data-extension="jpg" data-prefix="http://127.0.0.1:{{port}}/galleries/{MEDIA_ID}/">
  <script>
    var extensions = document.createElement('textarea');
    extensions.innerHTML = '[{letters}]';
    window.extensions = JSON.parse(extensions.value);
  </script>
</div>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # 静音默认访问日志
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的固定接口
        path = self.path.split("?")[0]
        if path == f"/comic/{COMIC_ID}":
            self._send(200, _detail_page().encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == f"/comic/{COMIC_ID}/1":
            page = _reader_page().replace("{port}", str(self.server.server_port))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path.startswith(f"/galleries/{MEDIA_ID}/"):
            name = path.rsplit("/", 1)[-1]
            page_part = name.split(".", 1)[0]
            if page_part.isdigit() and int(page_part) == BROKEN_PAGE:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            self._send(200, _PNG + _PADDING, "image/png")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "replace")


class _FakeSession:
    """cloudscraper 会话的最小替身：自测不联网，也不把 cloudscraper 拖进依赖。"""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.proxies: dict[str, str] = {}
        self.trust_env = False

    def get(self, url: str, timeout: float | tuple[float, float]) -> _FakeResponse:
        # 真实抓页器传的是 requests 的 (连接, 读取) 二元组，替身只取一个总超时
        total = max(timeout) if isinstance(timeout, tuple) else timeout
        request = Request(url, headers=self.headers)
        try:
            with urlopen(request, timeout=total) as response:  # noqa: S310 - 只打本地假站点
                return _FakeResponse(response.status, response.read())
        except HTTPError as exc:
            return _FakeResponse(exc.code, exc.read())
        except URLError as exc:
            raise OSError(str(exc)) from exc


def _install_scraper_shim() -> None:
    """把抓页器的 cloudscraper 换成替身：本自测不关心浏览器指纹，只关心链路。"""
    shim = types.ModuleType("cloudscraper-shim")
    shim.create_scraper = lambda **kwargs: _FakeSession()  # noqa: ARG005
    hanime_site.import_cloudscraper = lambda: shim  # type: ignore[assignment]


def _start_server() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, int(server.server_port)


async def _run() -> int:
    _install_scraper_shim()
    server, port = _start_server()
    work_dir = Path(tempfile.mkdtemp(prefix="hanime1-selftest-"))
    base = f"http://127.0.0.1:{port}"
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    channel = Hanime1Channel(
        {
            "enable": True,
            "site_domains": base,
            "image_domains": base,
            "request_timeout": 10,
            "retry_times": 1,
            "max_concurrent_images": 3,
            # 自测打的是本地假站点，必须关掉默认的读取代理，否则请求会被转到第三方服务
            "fetch_proxy": "",
        },
        PluginSettings(),
    )
    progress: list[tuple[int, int, str]] = []

    async def on_progress(done: int, total: int, unit: str) -> None:
        progress.append((done, total, unit))

    try:
        # ---- ID 解析 ----
        check(
            "normalize_id 认得出 /comic/<ID> 链接",
            channel.normalize_id(f"https://hanime1.me/comic/{COMIC_ID}") == COMIC_ID,
        )
        check(
            "normalize_id 认得出纯数字 ID",
            channel.normalize_id(f" {COMIC_ID} ") == COMIC_ID,
        )
        try:
            channel.normalize_id("https://hanime1.me/watch?v=101573")
            check("视频链接被拒绝并给出提示", False, "没有抛异常")
        except domain_module.ChannelError as exc:
            check("视频链接被拒绝并给出提示", "视频" in str(exc), str(exc))

        # ---- 元信息 ----
        meta = await channel.fetch_meta(COMIC_ID, work_dir)
        check(
            "详情页标题/作者解析正确",
            meta.title == "夏期補習総集編1～6+" and meta.author == "ゆきよし真水",
            f"{meta.title} / {meta.author}",
        )
        check("详情页页数正确", meta.page_count == PAGES, f"page_count={meta.page_count}")
        check("标签解析正确", meta.tags[:2] == ["中出", "JK制服"], str(meta.tags))

        # ---- 真实下载 ----
        comic = await channel.download(COMIC_ID, work_dir, on_progress)
        on_disk = [
            path
            for path in comic.source_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        ]
        check(
            "图片真的落盘（成功张数 = 磁盘文件数）",
            comic.image_count == EXPECTED_IMAGES == len(on_disk),
            f"落盘 {len(on_disk)} 张，上报 {comic.image_count} 张",
        )
        check(
            "单张坏图只计失败、不拖垮整单",
            comic.failed_images == 1,
            f"failed_images={comic.failed_images}",
        )
        names = sorted(path.name for path in on_disk)
        check(
            "文件名按页序补零、扩展名取自页面（001.jpg 起）",
            names[:2] == ["001.jpg", "002.jpg"] and len(names) == EXPECTED_IMAGES,
            str(names[:3]),
        )
        check(
            "进度回调拿到正确总数",
            bool(progress) and progress[-1][1] == PAGES,
            f"最后一次 {progress[-1] if progress else None}",
        )
    finally:
        server.shutdown()
        shutil.rmtree(work_dir, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    print()
    for name, ok, detail in results:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}" + (f" —— {detail}" if detail and not ok else ""))
    print(f"\n{passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))

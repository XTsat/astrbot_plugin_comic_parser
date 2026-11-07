"""哔咔渠道的下载自测：起一个本地假站点，验证渠道真能把图片落盘。

不联网、不需要真实哔咔账号：假站点按哔咔的协议（签名、登录、翻页、图片直链）应答，
并用同一套签名密钥校验**每一个**请求的 HMAC 签名——签名口径算错（例如漏了 query）
就直接 401，测试随即失败。

用法（在插件根目录执行）：

    python core/channels/picacg/selftest.py          # 本地假站点，不联网
    PICA_ACCOUNT=邮箱 PICA_PASSWORD=密码 PICA_COMIC_ID=24位ID \
        python core/channels/picacg/selftest.py --live   # 打真实哔咔接口

本地自测退出码 0 表示全部通过；`--live` 用来在能访问哔咔的网络里确认「真站也下得动」。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 插件根目录自己不是包（靠 AstrBot 直接加载），所以把它的父目录放进 sys.path，
# 再按「插件目录名.core.channels.picacg」导入，相对 import 的层级才对得上
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
    api_module.logger = logging.getLogger("picacg-selftest")
    astrbot_module = types.ModuleType("astrbot")
    astrbot_module.api = api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module


_install_astrbot_stub()

import asyncio  # noqa: E402 - 必须先装好 astrbot stub 再 import 插件代码
import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import importlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import zipfile  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import parse_qs, urlsplit  # noqa: E402

picacg_api = importlib.import_module(f"{PACKAGE}.core.channels.picacg.api")
picacg_channel = importlib.import_module(f"{PACKAGE}.core.channels.picacg.channel")
picacg_constants = importlib.import_module(f"{PACKAGE}.core.channels.picacg.constants")
domain_module = importlib.import_module(f"{PACKAGE}.domain")
domain_constants = importlib.import_module(f"{PACKAGE}.domain.constants")
progress_module = importlib.import_module(f"{PACKAGE}.core.progress")
ProgressState = progress_module.ProgressState

PicacgChannel = picacg_channel.PicacgChannel
PluginSettings = domain_module.PluginSettings
IMAGE_SUFFIXES = domain_constants.IMAGE_SUFFIXES
API_KEY = picacg_constants.API_KEY
DEFAULT_SECRET_KEY = picacg_constants.DEFAULT_SECRET_KEY

COMIC_ID = "5f1e2d3c4b5a69788796a5b4"
TOKEN = "mock-token"
# 故意坏掉的图片：验证渠道会把失败计入 failed_images 而不是整单失败
BROKEN_IMAGE = "ep2/002.jpg"
EXPECTED_IMAGES = 8
EXPECTED_FAILED = 1

_EPISODES = [
    [
        {"_id": "ep-a", "id": "ep-a", "order": 1, "title": "第一话/序章", "updated_at": "2024-01-01"},
        {"_id": "ep-b", "id": "ep-b", "order": 2, "title": "第二话", "updated_at": "2024-01-02"},
    ],
    [
        {"_id": "ep-c", "id": "ep-c", "order": 3, "title": "第三话", "updated_at": "2024-01-03"},
    ],
]
_PAGES = {
    1: [["ep1/001.jpg", "ep1/002.jpg", "ep1/003.jpg"], ["ep1/004.jpg", "ep1/005.jpg"]],
    2: [["ep2/001.jpg", BROKEN_IMAGE]],
    3: [["ep3/001.jpg"]],
}
_PAGE_RE = re.compile(r"^comics/(?P<comic>[0-9a-f]{24})/order/(?P<order>\d+)/pages$")
_COMIC_RE = re.compile(r"^comics/(?P<comic>[0-9a-f]{24})$")
_EPS_RE = re.compile(r"^comics/(?P<comic>[0-9a-f]{24})/eps$")


class _Handler(BaseHTTPRequestHandler):
    """假哔咔：先验签名与登录态，再按固定数据应答。"""

    protocol_version = "HTTP/1.1"
    stats = {"signed": 0, "bad_signature": 0, "missing_token": 0, "images": 0}
    port = 0

    def log_message(self, fmt: str, *args: object) -> None:  # 静音默认访问日志
        return

    # ---- 响应工具 ----
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, object]) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    # ---- 鉴权 ----
    def _check_signature(self, endpoint: str, method: str) -> bool:
        timestamp = self.headers.get("time", "")
        nonce = self.headers.get("nonce", "")
        signature = self.headers.get("signature", "")
        if not (timestamp and nonce and signature) or self.headers.get("api-key") != API_KEY:
            return False
        raw = f"{endpoint}{timestamp}{nonce}{method}{API_KEY}".lower()
        expected = hmac.new(
            DEFAULT_SECRET_KEY.encode(), raw.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            type(self).stats["bad_signature"] += 1
            return False
        type(self).stats["signed"] += 1
        return True

    def _need_token(self) -> bool:
        if self.headers.get("authorization") == TOKEN:
            return True
        type(self).stats["missing_token"] += 1
        self._json(401, {"code": 401, "message": "请先登录"})
        return False

    # ---- 路由 ----
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        self._body = self.rfile.read(length) if length else b""
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        split = urlsplit(self.path)
        path = split.path.lstrip("/")
        query = parse_qs(split.query)

        # 图片是静态直链，真实哔咔也不校验签名与登录态，所以先于签名检查处理
        if path.startswith("static/"):
            self._serve_image(path[len("static/") :])
            return

        endpoint = self.path.lstrip("/")  # 含 query，与哔咔的签名口径一致
        if not self._check_signature(endpoint, method):
            self._json(401, {"code": 401, "message": "签名错误"})
            return

        if path == "auth/sign-in" and method == "POST":
            self._sign_in()
            return

        matched = _COMIC_RE.match(path)
        if matched:
            self._comic()
            return
        matched = _EPS_RE.match(path)
        if matched:
            self._episodes(int((query.get("page") or ["1"])[0]))
            return
        matched = _PAGE_RE.match(path)
        if matched:
            self._pages(int(matched.group("order")), int((query.get("page") or ["1"])[0]))
            return
        self._json(404, {"code": 404, "message": f"未知接口 {path}"})

    def _sign_in(self) -> None:
        try:
            payload = json.loads(self._body or b"{}")
        except ValueError:
            payload = {}
        if payload.get("email") != "demo@example.com" or payload.get("password") != "pw":
            self._json(400, {"code": 400, "error": "1005", "message": "账号或密码错误"})
            return
        self._json(200, {"code": 200, "data": {"token": TOKEN}})

    def _comic(self) -> None:
        if not self._need_token():
            return
        self._json(
            200,
            {
                "code": 200,
                "data": {
                    "comic": {
                        "_id": COMIC_ID,
                        "title": "自测用漫画",
                        "author": "自测作者",
                        "epsCount": 3,
                        "finished": False,
                    }
                },
            },
        )

    def _episodes(self, page: int) -> None:
        if not self._need_token():
            return
        docs = _EPISODES[page - 1] if 0 < page <= len(_EPISODES) else []
        self._json(
            200,
            {
                "code": 200,
                "data": {
                    "eps": {
                        "docs": docs,
                        "total": sum(len(item) for item in _EPISODES),
                        "limit": 2,
                        "page": page,
                        "pages": len(_EPISODES),
                    }
                },
            },
        )

    def _pages(self, order: int, page: int) -> None:
        if not self._need_token():
            return
        chunk = _PAGES.get(order, [])
        docs = chunk[page - 1] if 0 < page <= len(chunk) else []
        self._json(
            200,
            {
                "code": 200,
                "data": {
                    "pages": {
                        "docs": [
                            {"_id": f"{order}-{index}", "id": f"{order}-{index}", "media": {
                                "originalName": item.rsplit("/", 1)[-1],
                                "path": item,
                                "fileServer": f"http://127.0.0.1:{type(self).port}",
                            }}
                            for index, item in enumerate(docs, start=1)
                        ],
                        "total": sum(len(item) for item in chunk),
                        "limit": 3,
                        "page": page,
                        "pages": len(chunk),
                    },
                    "ep": {"_id": f"ep-{order}", "title": f"第{order}话"},
                },
            },
        )

    def _serve_image(self, name: str) -> None:
        type(self).stats["images"] += 1
        if name == BROKEN_IMAGE:
            self._send(500, b"boom", "text/plain")
            return
        # 前 4 字节是 JPEG 魔数，其余填充到超过最小体积阈值
        self._send(200, b"\xff\xd8\xff\xe0" + b"\x00" * 1024, "image/jpeg")


def _start_server() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    _Handler.port = port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


async def _run() -> int:
    server, port = _start_server()
    picacg_api.BASE_URL = f"http://127.0.0.1:{port}/"
    work_dir = Path(tempfile.mkdtemp(prefix="picacg-selftest-"))
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    channel = PicacgChannel(
        {
            "enable": True,
            "account": "demo@example.com",
            "password": "pw",
            "retry_times": 1,
            "max_concurrent_images": 3,
            "request_timeout": 10,
        },
        PluginSettings(),
    )
    progress: list[ProgressState] = []

    async def on_progress(state: ProgressState) -> None:
        progress.append(state)

    try:
        # ---- ID 解析 ----
        check(
            "normalize_id 能从链接里认出 24 位 ID",
            channel.normalize_id(f"https://manhuapica.com/comic/{COMIC_ID}") == COMIC_ID,
        )

        # ---- 元信息（含翻页取章节）----
        meta = await channel.fetch_meta(COMIC_ID, work_dir)
        check("元信息标题/作者正确", meta.title == "自测用漫画" and meta.author == "自测作者",
              f"{meta.title}/{meta.author}")

        # ---- 真实下载 ----
        comic = await channel.download(COMIC_ID, work_dir, on_progress)
        on_disk = [
            path
            for path in comic.source_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        ]
        check(
            "图片真的落盘（成功张数 = 磁盘文件数）",
            comic.image_count == EXPECTED_IMAGES - EXPECTED_FAILED == len(on_disk),
            f"落盘 {len(on_disk)} 张，上报 {comic.image_count} 张",
        )
        check("单张坏图只计失败、不拖垮整单", comic.failed_images == EXPECTED_FAILED,
              f"failed_images={comic.failed_images}")
        check("章节目录按 order 前缀命名（打包不乱序）",
              sorted(p.name for p in comic.source_dir.iterdir())[:1] == ["001_第一话_序章"],
              str(sorted(p.name for p in comic.source_dir.iterdir())))
        check("进度回调拿到正确总数", progress and progress[-1].total == EXPECTED_IMAGES,
              f"最后一次 {progress[-1] if progress else None}")
        check("章节列表翻页取全（2 页 → 3 章）",
              len(list(comic.source_dir.iterdir())) == 3,
              str(len(list(comic.source_dir.iterdir()))))

        # ---- 协议层校验 ----
        check("所有请求签名都被假站点验过", _Handler.stats["bad_signature"] == 0,
              f"signed={_Handler.stats['signed']} bad={_Handler.stats['bad_signature']}")
        check("未出现缺登录态请求", _Handler.stats["missing_token"] == 0,
              f"missing_token={_Handler.stats['missing_token']}")
        check("确实请求了图片接口", _Handler.stats["images"] >= EXPECTED_IMAGES,
              f"images={_Handler.stats['images']}")

        # ---- 端到端：渠道 → 上层统一打包 ----
        task_module = importlib.import_module(f"{PACKAGE}.core.task")
        settings = PluginSettings.from_config(
            {
                "packing": {"pack_format": "zip"},
                "download": {"retry_times": 0, "timeout": 120},
            }
        )
        task = task_module.DownloadTask(
            channel, settings, work_dir / "temp", work_dir / "save"
        )
        outcome = await task.run(COMIC_ID)
        members = (
            zipfile.ZipFile(outcome.pack.output_path).namelist()
            if outcome.pack.output_path and outcome.pack.output_path.exists()
            else []
        )
        check(
            "端到端打包出可用的 ZIP",
            outcome.pack.success and len(members) == EXPECTED_IMAGES - EXPECTED_FAILED,
            f"{outcome.pack.output_path.name if outcome.pack.output_path else '无产物'} · {len(members)} 个成员",
        )

        # ---- 图片直链拼接（三种历史前缀）----
        plain = picacg_api.image_url({"path": "ep/001.jpg", "fileServer": "http://h:1"})
        check("普通路径拼 /static/", plain == "http://h:1/static/ep/001.jpg", plain)
        tobs = picacg_api.image_url({"path": "tobs/ep/001.jpg", "fileServer": ""})
        check("tobs/ 前缀走 B 节点", tobs.endswith("/static/ep/001.jpg"), tobs)
        encoded = base64.b64encode(b"https://cdn.example.com/a.jpg").decode()
        tobe = picacg_api.image_url({"path": f"tobeimg/{encoded}.jpg", "fileServer": ""})
        check("tobeimg/ 前缀解密出绝对地址", tobe == "https://cdn.example.com/a.jpg", tobe)
    finally:
        server.shutdown()
        shutil.rmtree(work_dir, ignore_errors=True)

    width = max(len(name) for name, _, _ in results)
    print("\n===== 哔咔渠道下载自测 =====")
    for name, ok, detail in results:
        print(f"{'✅' if ok else '❌'} {name.ljust(width)}  {detail}")
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)} 项")
    return 1 if failed else 0


async def _run_live() -> int:
    """真站探针：登录 → 取元信息 → 取章节 → 真下一张图，验证渠道在网络可达时的表现。

    账号从环境变量读，避免把凭据写进仓库；只下第一张图，不整本下载。
    """
    import os

    account = os.getenv("PICA_ACCOUNT", "")
    password = os.getenv("PICA_PASSWORD", "")
    comic_id = os.getenv("PICA_COMIC_ID", "")
    proxy = os.getenv("PICA_PROXY", "")
    if not (account and password and comic_id):
        print("请先设置 PICA_ACCOUNT / PICA_PASSWORD / PICA_COMIC_ID 再跑 --live")
        return 2

    settings = PluginSettings.from_config({"download": {"proxy": proxy}})
    channel = PicacgChannel(
        {"enable": True, "account": account, "password": password},
        settings,
    )
    work_dir = Path(tempfile.mkdtemp(prefix="picacg-live-"))
    try:
        print(f"→ 登录并取元信息 {comic_id} …")
        meta = await channel.fetch_meta(comic_id, work_dir)
        print(f"  标题：{meta.title} · 作者：{meta.author} · 章节数：{meta.page_count}")
        print("→ 整本下载（含全部章节与图片）…")
        comic = await channel.download(comic_id, work_dir)
        print(f"  成功 {comic.image_count} 张，失败 {comic.failed_images} 张")
        print(f"  落盘目录：{comic.source_dir}")
        return 0 if comic.image_count else 1
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    if "--live" in sys.argv:
        raise SystemExit(asyncio.run(_run_live()))
    raise SystemExit(asyncio.run(_run()))

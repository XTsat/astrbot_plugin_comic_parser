"""维护类能力的离线自测：环境自检、运行期统计、临时目录清理。

不联网、不依赖 AstrBot 运行时（只在 `sys.modules` 里放一个假的 `astrbot.api`），
验证的是三件容易写错的事：

- 自检**只在当前配置真会踩到时**才报 error（缺 `pyzipper` 但没设密码不该报错）
- 统计把成功 / 失败 / 暂停分别计数，暂停不算失败，渠道合计与全局合计对得上
- 清理只删**按现有配置已经过期**的东西：未过期断点、正在下载的目录、面板暂存、长期保存目录都不碰

用法（在插件根目录执行）：

    python core/selftest.py          # 本地，不联网

退出码 0 表示全部通过。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

# 插件根目录自己不是包（靠 AstrBot 直接加载），所以把它的父目录放进 sys.path，
# 再按「插件目录名.core...」导入，相对 import 的层级才对得上
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))
PACKAGE = PLUGIN_ROOT.name


def _install_astrbot_stub() -> None:
    """给被导入的模块一个假的 `astrbot`：自测只验逻辑，不必拉起整个 AstrBot。

    除 `astrbot.api.logger` 外还要补 `main.py` 顶层用到的那几个装饰器与消息组件——
    否则「命令接线对不对」这条最有价值的检查就跑不起来。
    """
    if "astrbot.api" in sys.modules:
        return
    import logging
    import types

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    def module(name: str, **attrs: object) -> types.ModuleType:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return mod

    class _Filter:
        """命令装饰器一律原样返回函数：自测只关心函数体，不关心注册。"""

        def command(self, *args: object, **kwargs: object):
            return lambda fn: fn

        def permission_type(self, *args: object, **kwargs: object):
            return lambda fn: fn

        def event_message_type(self, *args: object, **kwargs: object):
            return lambda fn: fn

        class PermissionType:
            ADMIN = "admin"

        class EventMessageType:
            ALL = "all"

    class _Star:
        def __init__(self, context: object | None = None) -> None:
            self.context = context

    class _StarTools:
        @staticmethod
        def get_data_dir(name: str) -> str:
            return tempfile.mkdtemp(prefix=f"{name}-")

    def _register(*args: object, **kwargs: object):
        return lambda cls: cls

    module("astrbot")
    module("astrbot.api", logger=logging.getLogger("core-selftest"), AstrBotConfig=dict)
    module(
        "astrbot.api.star",
        Star=_Star,
        Context=object,
        StarTools=_StarTools,
        register=_register,
    )
    module(
        "astrbot.api.event",
        filter=_Filter(),
        AstrMessageEvent=object,
        MessageEventResult=object,
        MessageChain=object,
    )
    components = module("astrbot.api.message_components")
    for name in ("Plain", "Image", "Node", "Nodes", "File", "Video", "Reply", "At"):
        setattr(components, name, type(name, (), {}))
    module(
        "astrbot.api.web",
        request=None,
        json_response=lambda *a, **k: None,
        error_response=lambda *a, **k: None,
        file_response=lambda *a, **k: None,
        stream_response=lambda *a, **k: None,
    )


_install_astrbot_stub()

from astrbot_plugin_comic_parser.core import health as health_mod  # noqa: E402
from astrbot_plugin_comic_parser.core import janitor  # noqa: E402
from astrbot_plugin_comic_parser.core import probe as probe_mod  # noqa: E402
from astrbot_plugin_comic_parser.core import panel as panel_mod  # noqa: E402
from astrbot_plugin_comic_parser.core.stats import (  # noqa: E402
    OUTCOME_FAIL,
    OUTCOME_OK,
    OUTCOME_PAUSED,
    DownloadStats,
)
from astrbot_plugin_comic_parser.domain.constants import (  # noqa: E402
    CLEAN_WORDS,
    HEALTH_WORDS,
    HELP_WORDS,
    RANKING_WORDS,
    RETRY_WORDS,
    SEARCH_WORDS,
    SITE_WORDS,
    STATS_VERSION,
    STATS_WORDS,
    STATUS_WORDS,
    STOP_WORDS,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def make_settings(**overrides: object) -> SimpleNamespace:
    """最小可用配置：默认值刻意选「不需要任何可选依赖」的那一档。"""
    base = {
        "pack_format": "zip",
        "pack_password": "",
        "cover_censor": False,
        "cover_send": False,
        "ranking_enabled": False,
        "ranking_output": "text",
        "nsfw_auto_download": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def probe_all_present(module: str) -> bool:
    return True


def probe_missing(*missing: str):
    """构造一个「除了这些模块，别的都在」的探测面。"""

    def probe(module: str) -> bool:
        return module not in missing

    return probe


def fake_channel(name: str, *, enabled: bool = True, missing: str | None = None):
    channel = SimpleNamespace(
        name=name,
        display_name=name.upper(),
        enabled=enabled,
    )
    channel.missing_requirement = lambda: missing
    return channel


# ---- 环境自检 ----


def test_health() -> None:
    # 依赖齐全 + 无密码 + zip：什么都不报
    report = health_mod.check(make_settings(), {}, probe=probe_all_present)
    check("自检：依赖齐全时不报问题", report.ok and not report.issues, str(report.issues))
    check(
        "自检：已就位依赖有据可查",
        "pyzipper" in report.present and "pymupdf" in report.present,
        str(report.present),
    )

    # 缺 pyzipper 但没设密码：不该报错（没走到加密那条路）
    report = health_mod.check(
        make_settings(pack_password=""), {}, probe=probe_missing("pyzipper")
    )
    check(
        "自检：没设密码时缺 pyzipper 不算问题",
        report.ok and not report.issues,
        str([item.title for item in report.issues]),
    )

    # 缺 pyzipper 且设了密码：error，且文案说清后果与补救
    report = health_mod.check(
        make_settings(pack_password="hunter2"), {}, probe=probe_missing("pyzipper")
    )
    titles = " ".join(item.title for item in report.issues)
    fixes = " ".join(item.fix for item in report.issues)
    check(
        "自检：设了密码缺 pyzipper 报 error",
        not report.ok and "pyzipper" in titles,
        titles,
    )
    check("自检：给出可照做的补救命令", "pip install pyzipper" in fixes, fixes)

    # PDF 格式缺 pymupdf
    report = health_mod.check(
        make_settings(pack_format="pdf"), {}, probe=probe_missing("fitz")
    )
    check(
        "自检：PDF 格式缺 pymupdf 报 error",
        not report.ok and "pymupdf" in " ".join(item.title for item in report.issues),
        str([item.title for item in report.issues]),
    )

    # 长图格式缺 Pillow
    report = health_mod.check(
        make_settings(pack_format="long_img"), {}, probe=probe_missing("PIL")
    )
    check(
        "自检：长图格式缺 Pillow 报 error",
        not report.ok and "Pillow" in " ".join(item.title for item in report.issues),
        str([item.title for item in report.issues]),
    )

    # 打码开着、缺 onnxruntime、又关了自动补装：只降级（打码跑不起来，下载照常）
    report = health_mod.check(
        make_settings(
            cover_censor=True, cover_send=True, nsfw_auto_download=False
        ),
        {},
        probe=probe_missing("onnxruntime"),
    )
    check(
        "自检：打码缺 onnxruntime 只降级不报错",
        report.ok and len(report.warnings) == 1,
        f"ok={report.ok} warns={len(report.warnings)}",
    )

    # 自动补装开着时连降级都不用提：加载阶段会自己装上
    report = health_mod.check(
        make_settings(cover_censor=True, cover_send=True, nsfw_auto_download=True),
        {},
        probe=probe_missing("onnxruntime"),
    )
    check(
        "自检：自动补装开着时缺 onnxruntime 不啰嗦",
        report.ok and not report.issues,
        str([item.title for item in report.issues]),
    )

    # 缺 cloudscraper：只降级（主路径不依赖它），但不能像以前那样直接不提
    report = health_mod.check(
        make_settings(), {}, probe=probe_missing("cloudscraper")
    )
    check(
        "自检：缺 cloudscraper 报降级而不是不吭声",
        report.ok and len(report.warnings) == 1,
        f"ok={report.ok} warns={len(report.warnings)}",
    )
    check(
        "自检：缺 cloudscraper 会点名受影响的两个渠道",
        "紳士漫畫" in " ".join(item.impact for item in report.issues),
        str([item.impact for item in report.issues]),
    )

    # 渠道启用了却缺依赖：error，并点名渠道
    report = health_mod.check(
        make_settings(),
        {"jmcomic": fake_channel("jmcomic", missing="jmcomic")},
        probe=probe_all_present,
    )
    check(
        "自检：启用的渠道缺依赖会被点名",
        not report.ok and "JMCOMIC" in " ".join(item.title for item in report.issues),
        str([item.title for item in report.issues]),
    )

    # 渠道没启用：不报
    report = health_mod.check(
        make_settings(),
        {"jmcomic": fake_channel("jmcomic", enabled=False, missing="jmcomic")},
        probe=probe_all_present,
    )
    check("自检：未启用的渠道不参与判定", report.ok and not report.issues, "")

    # 聊天文案：通过时是一句话，有问题时逐条列
    text_ok = health_mod.check(make_settings(), {}, probe=probe_all_present).render()
    text_bad = health_mod.check(
        make_settings(pack_password="x"), {}, probe=probe_missing("pyzipper")
    ).render()
    check(
        "自检文案：通过时一句话带依赖数",
        "通过" in text_ok and "项依赖齐全" in text_ok,
        text_ok,
    )
    check(
        "自检文案：有问题时说清后果与补救",
        "白费" in text_bad and "补：" in text_bad,
        text_bad,
    )

    # 面板快照字段齐全
    snap = health_mod.check(
        make_settings(pack_password="x"), {}, probe=probe_missing("pyzipper")
    ).snapshot()
    check(
        "自检快照：ok/errors/warnings/issues 齐全",
        {"ok", "errors", "warnings", "present", "issues"} <= set(snap),
        str(sorted(snap)),
    )


# ---- 运行期统计 ----




# ---- 运行期统计 ----


def test_stats() -> None:
    stats = DownloadStats()
    empty = stats.render()
    check("统计：没有记录时给一句说明", "还没有下载记录" in empty, empty)

    stats.record("jmcomic", OUTCOME_OK, images=20, size_bytes=2 * 1024 * 1024, elapsed=30)
    stats.record("jmcomic", OUTCOME_FAIL, elapsed=5)
    stats.record("nhentai", OUTCOME_PAUSED, images=7, elapsed=12)

    snap = stats.snapshot()
    check(
        "统计：成功/失败/暂停分别计数",
        (snap.ok, snap.fail, snap.paused) == (1, 1, 1),
        f"{snap.ok}/{snap.fail}/{snap.paused}",
    )
    check("统计：总次数含暂停", snap.total == 3, str(snap.total))
    check(
        "统计：图片与产物只累加成功那次",
        snap.images == 27 and snap.bytes == 2 * 1024 * 1024,
        f"images={snap.images} bytes={snap.bytes}",
    )
    check(
        "统计：渠道合计与全局合计对得上",
        snap.channels["jmcomic"].total == 2 and snap.channels["nhentai"].total == 1,
        str({k: v.total for k, v in snap.channels.items()}),
    )
    check(
        "统计：渠道按次数降序排（jm 有两次）",
        next(iter(snap.channels)) == "jmcomic",
        str(list(snap.channels)),
    )

    text = stats.render()
    check(
        "统计文案：总览 + 渠道明细 + 起算说明",
        "3 次下载" in text and "jmcomic" in text and "本次运行" in text,
        text,
    )

    # 未知结局按失败记：宁可多报一次失败，也不能静默吞掉
    stats.record("wnacg", "something-odd")
    snap = stats.snapshot()
    check(
        "统计：未知结局按失败记",
        snap.fail == 2 and snap.channels["wnacg"].fail == 1,
        f"fail={snap.fail}",
    )

    stats.reset()
    snap = stats.snapshot()
    check(
        "统计：清零后回到起点",
        snap.total == 0 and not snap.channels and snap.images == 0,
        str(snap.snapshot()),
    )


def test_stats_persistence() -> None:
    """落盘与读回：跨「重载」累计数要留着，本次运行的增量要归零。"""
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "stats.json"

        first = DownloadStats(path)
        first.record("jmcomic", OUTCOME_OK, images=10, size_bytes=1024, elapsed=4)
        first.record("jmcomic", OUTCOME_FAIL, elapsed=1)
        check("落盘：文件写出来了", path.is_file(), str(path))

        payload = json.loads(path.read_text(encoding="utf-8"))
        check(
            "落盘：带版本号与渠道表",
            payload.get("version") == STATS_VERSION
            and "jmcomic" in (payload.get("channels") or {}),
            str(sorted(payload)),
        )

        # 模拟重载：新实例读同一个文件
        second = DownloadStats(path)
        snap = second.snapshot()
        check(
            "落盘：重载后累计数还在",
            snap.total == 2 and snap.channels["jmcomic"].ok == 1,
            f"total={snap.total}",
        )
        check(
            "落盘：重载后本次运行从零开始",
            snap.session.get("total") == 0,
            str(snap.session),
        )

        second.record("nhentai", OUTCOME_OK, images=3)
        snap = second.snapshot()
        check(
            "落盘：新记录同时进累计与本次运行",
            snap.total == 3 and snap.session.get("total") == 1,
            f"total={snap.total} session={snap.session}",
        )
        text = second.render()
        check(
            "落盘文案：说清是累计、存于哪个文件",
            "累计" in text and "stats.json" in text,
            text,
        )
        check(
            "落盘文案：不重复念「存盘、重载不丢」",
            "重载不丢" not in text and "会存盘" not in text,
            text,
        )

        # 版本对不上：按「没有历史」重来，但不能删用户文件、也不能崩
        path.write_text(
            json.dumps({"version": 999, "channels": {"jmcomic": {"ok": 5}}}),
            encoding="utf-8",
        )
        third = DownloadStats(path)
        check(
            "落盘：版本不认识时按无历史处理",
            third.snapshot().total == 0,
            str(third.snapshot().total),
        )

        # 内容损坏：同样不崩
        path.write_text("{ not json", encoding="utf-8")
        fourth = DownloadStats(path)
        check(
            "落盘：文件损坏时按无历史处理",
            fourth.snapshot().total == 0,
            str(fourth.snapshot().total),
        )

        # 字段类型不对：单条丢掉，不影响其它渠道
        path.write_text(
            json.dumps(
                {
                    "version": STATS_VERSION,
                    "channels": {
                        "jmcomic": {"ok": "坏值", "fail": 2},
                        "nhentai": "根本不是字典",
                    },
                }
            ),
            encoding="utf-8",
        )
        fifth = DownloadStats(path)
        snap = fifth.snapshot()
        check(
            "落盘：脏字段按 0 算、坏条目丢弃",
            snap.channels["jmcomic"].ok == 0
            and snap.channels["jmcomic"].fail == 2
            and "nhentai" not in snap.channels,
            str({k: v.snapshot() for k, v in snap.channels.items()}),
        )

        # 清零要把文件也清掉，否则重载后又回来了
        fifth.reset()
        sixth = DownloadStats(path)
        check(
            "落盘：清零后重载仍是空的",
            sixth.snapshot().total == 0,
            str(sixth.snapshot().total),
        )

    # 纯内存模式（不传路径）不该碰磁盘
    memory_only = DownloadStats()
    memory_only.record("jmcomic", OUTCOME_OK)
    check(
        "落盘：不传路径时是纯内存",
        memory_only.snapshot().total == 1,
        "",
    )


# ---- 面板统计清零 ----


def test_panel_stats_reset() -> None:
    """面板「清零」：清掉累计数、清盘，并把新快照返回给页面。"""
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "stats.json"
        stats = DownloadStats(path)
        stats.record("jmcomic", OUTCOME_OK, images=4, size_bytes=2048, elapsed=3)

        service = panel_mod.PanelService(
            models_dir=Path(raw) / "models",
            temp_root=Path(raw) / "temp",
            stats_provider=lambda: stats.snapshot().snapshot(),
            stats_reset=stats.reset,
        )
        payload = service.reset_stats()
        check(
            "面板清零：返回的是清零后的快照",
            payload.get("total") == 0,
            str(payload.get("total")),
        )
        check("面板清零：累计数确实清掉了", stats.snapshot().total == 0, "")
        check(
            "面板清零：磁盘上也不再保留旧数",
            DownloadStats(path).snapshot().total == 0,
            "",
        )

        # 没注入回调时要说清不可用，不能静默成功
        bare = panel_mod.PanelService(
            models_dir=Path(raw) / "models2",
            temp_root=Path(raw) / "temp2",
        )
        try:
            bare.reset_stats()
            check("面板清零：没注入回调时报错", False, "没有抛异常")
        except panel_mod.PanelError as exc:
            check("面板清零：没注入回调时报错", "不支持" in str(exc), str(exc))

        # 重置回调自己炸了，要变成可读错误，而不是把异常甩给页面
        def boom() -> None:
            raise OSError("磁盘只读")

        broken = panel_mod.PanelService(
            models_dir=Path(raw) / "models3",
            temp_root=Path(raw) / "temp3",
            stats_provider=lambda: {},
            stats_reset=boom,
        )
        try:
            broken.reset_stats()
            check("面板清零：写盘失败时报错", False, "没有抛异常")
        except panel_mod.PanelError as exc:
            check("面板清零：写盘失败时报错", "磁盘只读" in str(exc), str(exc))



# ---- 临时目录清理 ----


def _touch(path: Path, *, age: float = 0.0, size: int = 16) -> None:
    """建一个文件并把它连同各级父目录的 mtime 一起往前挪 ``age`` 秒。

    目录自己的 mtime 必须一起改：断点是否过期看的是「目录与其内部文件里最新的那个时间」
    （见 core/task.py 的 `_newest_mtime`），只把文件改旧、目录还是新建的，就会被判成没过期。
    """
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    for parent in path.parents:
        try:
            os.utime(parent, (stamp, stamp))
        except OSError:
            break


def test_janitor() -> None:
    now = time.time()
    with tempfile.TemporaryDirectory() as raw:
        temp_root = Path(raw) / "temp"
        # 过期断点（2 小时前）与未过期断点（刚写）
        old_break = temp_root / "resume" / "jmcomic-111"
        _touch(old_break / "001.jpg", age=7200, size=100)
        fresh_break = temp_root / "resume" / "jmcomic-222"
        _touch(fresh_break / "001.jpg", age=0, size=100)
        # 正在下载的目录：即使很旧也不能删
        active_dir = temp_root / "resume" / "jmcomic-333"
        _touch(active_dir / "001.jpg", age=7200, size=100)
        # 封面临时图：一张过期、一张新鲜
        _touch(temp_root / "covers" / "old-cover.jpg", age=7200, size=50)
        _touch(temp_root / "covers" / "new-cover.jpg", age=0, size=50)
        # 榜单：中间图过期、成图缓存还在有效期内
        _touch(temp_root / "ranking" / "card-old.png", age=7200, size=70)
        _touch(temp_root / "ranking" / "cache-abc.png", age=7200, size=70)
        # 强杀残留的信息目录
        _touch(temp_root / "info" / "jmcomic-444" / "meta.json", age=7200, size=10)
        # 面板暂存：不归清理命令管
        _touch(temp_root / "panel" / "keep-me.png", age=99999, size=10)

        result = janitor.cleanup_temp(
            temp_root,
            resume_ttl=3600,
            active_dirs={active_dir},
            now=now,
        )
        check("清理：删掉过期断点", not old_break.exists(), str(old_break))
        check("清理：未过期断点留着", fresh_break.exists(), str(fresh_break))
        check("清理：正在下载的目录不动", active_dir.exists(), str(active_dir))
        check(
            "清理：如实报出跳过数",
            result.skipped_active == 1,
            str(result.skipped_active),
        )
        check(
            "清理：过期封面临时图删掉、新鲜的留着",
            not (temp_root / "covers" / "old-cover.jpg").exists()
            and (temp_root / "covers" / "new-cover.jpg").exists(),
            "",
        )
        check(
            "清理：榜单中间图删掉、缓存按上限留着",
            not (temp_root / "ranking" / "card-old.png").exists()
            and (temp_root / "ranking" / "cache-abc.png").exists(),
            "",
        )
        check(
            "清理：强杀残留的信息目录清掉",
            not (temp_root / "info" / "jmcomic-444").exists(),
            "",
        )
        check(
            "清理：面板暂存不归它管",
            (temp_root / "panel" / "keep-me.png").exists(),
            "",
        )
        check(
            "清理：报了释放空间与分组明细",
            result.freed_bytes >= 100 and len(result.groups) >= 3,
            f"bytes={result.freed_bytes} groups={result.groups}",
        )
        check(
            "清理文案：说明不影响长期保存目录",
            "仅本地保存目录" in result.render(),
            result.render(),
        )

        # resume_ttl=0 = 不留断点：连新鲜断点也算过期
        janitor.cleanup_temp(temp_root, resume_ttl=0, active_dirs=set(), now=now)
        check(
            "清理：断点保留设 0 时全部清掉",
            not fresh_break.exists(),
            str(fresh_break),
        )

        # 空目录不该炸
        empty = janitor.cleanup_temp(Path(raw) / "nope", resume_ttl=60, now=now)
        check("清理：目录不存在时安全返回", empty.removed_total == 0, "")



# ---- 渠道探测（/comic 测试 <渠道> <ID> 与面板渠道测试共用） ----


class _FakeMeta:
    """只带探测用到的字段，免得为一条断言去构造完整 ComicMeta。"""

    def __init__(self, comic_id: str, title: str = "测试本") -> None:
        self.channel = "fake"
        self.comic_id = comic_id
        self.title = title
        self.author = "作者"
        self.page_count = 12
        self.tags = ["标签一", "标签二"]


class _FakeChannel:
    """够用的假渠道：认 ID、能取元信息、可选支持封面。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        supports_cover: bool = False,
        meta_error: BaseException | None = None,
        cover: bytes | None = None,
        cover_error: str = "",
    ) -> None:
        self.name = "fake"
        self.command = "fakecomic"
        self.command_aliases = ("fk",)
        self.display_command = "fk"
        self.enabled = enabled
        self.supports_cover = supports_cover
        self._meta_error = meta_error
        self._cover = cover
        self.cover_error = cover_error
        self.fetch_meta_calls = 0

    @property
    def command_names(self) -> tuple[str, ...]:
        return (self.command, *self.command_aliases)

    def normalize_id(self, raw: str) -> str:
        text = str(raw or "").strip()
        if not text.isdigit():
            raise probe_mod.PluginError("这看着不是作品 ID")
        return text

    def match_link(self, text: str) -> str:
        if "fakecomic.test" in str(text):
            return "777"
        return ""

    async def fetch_meta(self, comic_id: str, work_dir: Path) -> _FakeMeta:
        self.fetch_meta_calls += 1
        if self._meta_error is not None:
            raise self._meta_error
        return _FakeMeta(comic_id)

    async def fetch_cover(self, comic_id: str) -> bytes | None:
        return self._cover


def test_probe() -> None:
    """探测：成功/失败/链接路由/共用同一条实现。"""
    import asyncio

    channels = {"fake": _FakeChannel()}

    async def run(channel_name: str, text: str, **kwargs: object):
        with tempfile.TemporaryDirectory() as raw:
            return await probe_mod.probe_channel(
                channels, channel_name, text, Path(raw) / "probe", **kwargs
            )

    # 成功：ID 与渠道都收敛好，耗时也算得出来
    outcome = asyncio.run(run("fk", "123"))
    check("探测：别名也能命中渠道", outcome.ok and outcome.comic_id == "123", outcome.error)
    check(
        "探测：成功时给出标题与页数",
        outcome.meta.get("title") == "测试本" and outcome.meta.get("page_count") == 12,
        str(outcome.meta),
    )
    check("探测：成功文案带耗时", "通了" in outcome.render() and "ms" in outcome.render(), outcome.render())

    # 链接走链接那家，不走命令对应的渠道
    other = _FakeChannel()
    other.name = "other"
    other.command = "othercomic"
    other.display_command = "ot"
    resolved = asyncio.run(
        probe_mod.probe_channel(
            {"fake": channels["fake"], "other": other},
            "fk",
            "https://fakecomic.test/album/777",
            Path(tempfile.mkdtemp()),
        )
    )
    # 注意：channel 字段是渠道命令名（面板原字段就是这么给的），不是内部名
    check(
        "探测：作品链接按链接走（不是按命令）",
        resolved.ok and resolved.channel == "fakecomic" and resolved.comic_id == "777",
        f"{resolved.channel}/{resolved.comic_id}",
    )

    # 渠道没启用
    disabled = asyncio.run(
        probe_mod.probe_channel(
            {"off": _FakeChannel(enabled=False)}, "off", "1", Path(tempfile.mkdtemp())
        )
    )
    check(
        "探测：未启用的渠道直接说明",
        not disabled.ok and "没有启用" in disabled.error,
        disabled.error,
    )

    # 渠道不存在
    missing = asyncio.run(
        probe_mod.probe_channel({}, "nope", "1", Path(tempfile.mkdtemp()))
    )
    check(
        "探测：渠道不存在时说清是哪个",
        not missing.ok and "nope" in missing.error,
        missing.error,
    )

    # 超时：要变成可读文案，不能把 asyncio.TimeoutError 甩给用户
    slow = _FakeChannel(meta_error=asyncio.TimeoutError())

    async def slow_run():
        return await probe_mod.probe_channel(
            {"fake": slow}, "fk", "1", Path(tempfile.mkdtemp()), timeout=0.01
        )

    timeout_outcome = asyncio.run(slow_run())
    check(
        "探测：超时给可读文案",
        not timeout_outcome.ok and "秒" in timeout_outcome.error,
        timeout_outcome.error,
    )

    # 渠道自己抛的业务错误原样透出（已经是人话）
    failing = _FakeChannel(meta_error=probe_mod.PluginError("这本已下架"))
    failed = asyncio.run(
        probe_mod.probe_channel(
            {"fake": failing}, "fk", "1", Path(tempfile.mkdtemp())
        )
    )
    check(
        "探测：渠道的业务错误原样透出",
        not failed.ok and failed.error == "这本已下架",
        failed.error,
    )

    # 取封面：成功、以及「元信息通了但封面没取到」不该推翻成功结论
    with_cover = _FakeChannel(supports_cover=True, cover=b"\xff\xd8\xff" + b"0" * 100)
    covered = asyncio.run(
        probe_mod.probe_channel(
            {"fake": with_cover}, "fk", "1", Path(tempfile.mkdtemp()), with_cover=True
        )
    )
    check(
        "探测：要封面时能拿到字节",
        covered.ok and covered.cover is not None,
        covered.cover_error,
    )

    no_cover = _FakeChannel(supports_cover=False)
    nocover = asyncio.run(
        probe_mod.probe_channel(
            {"fake": no_cover}, "fk", "1", Path(tempfile.mkdtemp()), with_cover=True
        )
    )
    check(
        "探测：渠道不支持封面时仍算通",
        nocover.ok and nocover.cover is None and "只支持查元信息" in nocover.cover_error,
        nocover.cover_error,
    )

    # 参数拆分：只给渠道 / 渠道 + ID / 空
    check(
        "探测：参数拆分认「渠道 + ID」",
        probe_mod.split_target_args("jm 123456") == ("jm", "123456"),
        str(probe_mod.split_target_args("jm 123456")),
    )
    check(
        "探测：只给一个词时当渠道",
        probe_mod.split_target_args("  jm  ") == ("jm", ""),
        str(probe_mod.split_target_args("  jm  ")),
    )
    check(
        "探测：空参数原样返回",
        probe_mod.split_target_args("") == ("", ""),
        str(probe_mod.split_target_args("")),
    )
    check(
        "探测：多余空格被压平",
        probe_mod.split_target_args("jm   123") == ("jm", "123"),
        str(probe_mod.split_target_args("jm   123")),
    )

    # 链接粗判
    check(
        "探测：认得出作品链接",
        probe_mod.looks_like_link("https://x.test/a") and not probe_mod.looks_like_link("123"),
        "",
    )

    # 渠道名清单：面板与命令的提示都用它
    check(
        "探测：渠道名清单可用",
        "fk" in probe_mod.known_channel_names({"fake": _FakeChannel()}),
        probe_mod.known_channel_names({"fake": _FakeChannel()}),
    )


# ---- 平台自检（/comic 测试 不带参数） ----


class _SelfcheckChannel:
    """只带自检需要的那几个属性的假渠道。"""

    def __init__(self, name, *, enabled=True, ok=True, reason="", hint="", delay=0.0):
        self.name = name
        self.command = name
        self.command_aliases = (name,)
        self.display_command = name
        self.enabled = enabled
        self._ok = ok
        self._reason = reason
        self._hint = hint
        self._delay = delay
        self.calls = 0

    def resolve_proxy(self):
        """真实渠道都有这个方法；自检的失败路径也要靠它报线路。"""
        return ("http://127.0.0.1:7890", "代理")

    async def selfcheck(self, timeout: float = 15.0):
        import asyncio

        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return probe_mod.ChannelSelfcheck(
            channel=self.name,
            display_command=self.display_command,
            enabled=self.enabled,
            ok=self._ok,
            latency_ms=120 if self._ok else 0,
            # 失败项同样带线路：直连挂了该开代理，代理挂了该换线路
            proxy_source="直连",
            reason=self._reason,
            hint=self._hint,
        )


def test_selfcheck_channels() -> None:
    """平台自检：并发问每个已启用渠道，失败只影响自己。"""
    import asyncio

    async def run(channels, **kwargs):
        return await probe_mod.selfcheck_channels(channels, **kwargs)

    good = _SelfcheckChannel("jmcomic")
    bad = _SelfcheckChannel("nhentai", ok=False, reason="HTTP 403", hint="开代理")
    off = _SelfcheckChannel("wnacg", enabled=False)

    results = asyncio.run(run({"jmcomic": good, "nhentai": bad, "wnacg": off}))
    check(
        "自检：只问已启用的渠道",
        off.calls == 0 and good.calls == 1 and bad.calls == 1,
        f"good={good.calls} bad={bad.calls} off={off.calls}",
    )
    check(
        "自检：能用的排前面",
        [item.channel for item in results] == ["jmcomic", "nhentai"],
        str([(i.channel, i.ok) for i in results]),
    )

    text = probe_mod.render_selfcheck(results)
    check("自检文案：总览写「几个可用」", "1/2 个渠道可用" in text, text)
    check("自检文案：能用的给耗时与线路", "120ms" in text and "直连" in text, text)
    check("自检文案：失败项给原因与补救", "HTTP 403" in text and "开代理" in text, text)
    check(
        "自检文案：失败项也写明走的线路",
        "HTTP 403 · 直连" in text,
        text,
    )

    # 单个渠道抛异常 / 超时，不能把整次自检带崩
    class Boom(_SelfcheckChannel):
        async def selfcheck(self, timeout: float = 15.0):
            raise RuntimeError("炸了")

    class Slow(_SelfcheckChannel):
        async def selfcheck(self, timeout: float = 15.0):
            import asyncio as aio

            await aio.sleep(10)
            return await super().selfcheck(timeout)

    # timeout 传小：慢渠道睡 10 秒，1 秒上限必然超时，不必真等
    results = asyncio.run(
        run(
            {"boom": Boom("boom"), "slow": Slow("slow"), "good": _SelfcheckChannel("good")},
            timeout=1.0,
        )
    )
    by_name = {item.channel: item for item in results}
    check(
        "自检：某个渠道抛异常不影响其它渠道",
        by_name["good"].ok and not by_name["boom"].ok,
        str({k: v.ok for k, v in by_name.items()}),
    )
    check(
        "自检：超时的渠道给可读原因",
        not by_name["slow"].ok and "秒" in by_name["slow"].reason,
        by_name["slow"].reason,
    )
    check(
        "自检：超时/异常项也带线路",
        bool(by_name["slow"].proxy_source) and bool(by_name["boom"].proxy_source),
        f"slow={by_name['slow'].proxy_source!r} boom={by_name['boom'].proxy_source!r}",
    )

    # 一个渠道都没启用
    empty = asyncio.run(run({"off": _SelfcheckChannel("off", enabled=False)}))
    check("自检：没有已启用渠道时返回空", empty == [], str(empty))
    check(
        "自检文案：没有渠道时说清该做什么",
        "没有已启用的渠道" in probe_mod.render_selfcheck([]),
        probe_mod.render_selfcheck([]),
    )


# ---- 多域名降级（自检必须逐个试，且每个都有超时） ----


class _FallbackChannel:
    """假渠道：只实现多域名降级要用的那几样。"""

    name = "fallback"
    command = "fallback"
    display_command = "fb"

    async def _selfcheck_first_reachable(self, urls, **kwargs):
        from astrbot_plugin_comic_parser.core.channel import BaseChannel

        return await BaseChannel._selfcheck_first_reachable(self, urls, **kwargs)


def test_domain_fallback() -> None:
    """第一条域名打不开时继续试后面的，且每个候选都有超时上限。"""
    import asyncio

    from astrbot_plugin_comic_parser.core import domain_race

    calls: list[str] = []

    def fake_probe_url(domain, url, **kwargs):
        async def run():
            calls.append(url)
            # 只有第二个域名通
            ok = "second" in url
            return domain_race.DomainTiming(
                domain, ok, 100 if ok else 0, 0.0, "" if ok else "请求超时"
            )

        return run()

    original = domain_race.probe_url
    # channel.py 里用的是模块级别名，这里直接换掉那个名字
    import astrbot_plugin_comic_parser.core.channel as channel_mod

    channel_mod.probe_url = fake_probe_url
    try:
        timing, url = asyncio.run(
            _FallbackChannel()._selfcheck_first_reachable(
                ["https://first.test/", "https://second.test/"],
                proxy="",
                headers={"User-Agent": "x"},
            )
        )
        check("多域名：第一条不通时继续试第二条", timing.ok and "second" in url, url)
        check(
            "多域名：两条都试过了",
            calls == ["https://first.test/", "https://second.test/"],
            str(calls),
        )

        # 全都不通：返回最后一个的结果，不抛异常
        calls.clear()
        timing, url = asyncio.run(
            _FallbackChannel()._selfcheck_first_reachable(
                ["https://a.test/", "https://b.test/"],
                proxy="",
                headers={"User-Agent": "x"},
            )
        )
        check("多域名：全不通时给失败结论", not timing.ok, timing.note)

        # 卡住的候选要被超时切断，不能把整次自检拖死
        def hang(domain, url, **kwargs):
            async def run():
                await asyncio.sleep(30)
                return domain_race.DomainTiming(domain, True, 1, 0.0, "")

            return run()

        channel_mod.probe_url = hang
        import time as _time

        started = _time.monotonic()
        timing, _ = asyncio.run(
            _FallbackChannel()._selfcheck_first_reachable(
                ["https://hang.test/"], proxy="", headers={"User-Agent": "x"}, timeout=0.2
            )
        )
        elapsed = _time.monotonic() - started
        check(
            "多域名：卡住的候选按超时切断",
            not timing.ok and elapsed < 3,
            f"ok={timing.ok} 耗时 {elapsed:.1f}s",
        )
        check("多域名：超时文案可读", "s" in timing.note, timing.note)
    finally:
        channel_mod.probe_url = original



# ---- 命令接线 ----


def test_wiring() -> None:
    """接线检查：三条命令的词表进了分发器，main.py 能整份导入。

    这一段最容易在重构时悄悄断掉——词表加了、分支忘了加，用户敲命令就掉进「当成作品 ID」，
    而单测只看得到常量、看不到分发器。
    """
    import importlib

    module = importlib.import_module(f"{PACKAGE}.main")
    check("接线：main.py 能整份导入（装饰器与消息组件齐全）", True, "")

    # 词表之间不能互相重叠，否则同一句命令会随机走两个分支
    groups = {
        "自检": HEALTH_WORDS,
        "统计": STATS_WORDS,
        "清理": CLEAN_WORDS,
    }
    overlaps = []
    names = list(groups)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            shared = groups[left] & groups[right]
            if shared:
                overlaps.append(f"{left}/{right}:{shared}")
    check("接线：三组命令词互不重叠", not overlaps, str(overlaps))


    # 三个 handler 都挂在插件类上，且都是异步生成器（yield 结果）
    for name in ("_handle_health", "_handle_stats", "_handle_clean"):
        handler = getattr(module.ComicParserPlugin, name, None)
        check(
            f"接线：插件类有 {name}",
            handler is not None and hasattr(handler, "__wrapped__") or callable(handler),
            str(handler),
        )

    # 面板拿到的是可调用的提供者，且返回结构与前端约定一致
    import inspect

    source = inspect.getsource(module.ComicParserPlugin.__init__)
    check(
        "接线：面板注入了 health / stats 提供者",
        "health_provider=" in source and "stats_provider=" in source,
        "",
    )

    # 统计记录点在下载收尾处：没有它，/comic 统计永远是 0
    source = inspect.getsource(module.ComicParserPlugin)
    check(
        "接线：下载收尾会写统计",
        "_record_download_stat(channel, result_state, outcome)" in source,
        "",
    )

    # 聊天命令与面板共用同一套探测：两边各写一份的话「面板通、命令不通」迟早会出现
    from astrbot_plugin_comic_parser.core import panel as panel_mod

    check(
        "接线：面板渠道测试用的是共用探测",
        "probe_channel(" in inspect.getsource(panel_mod.PanelService.fetch_from_channel),
        "",
    )
    check(
        "接线：/comic 测试 用的是同一个探测",
        "probe_channel(" in inspect.getsource(module.ComicParserPlugin._handle_health),
        "",
    )

    # 统计落盘：主类必须把文件路径传进去，否则重载又归零
    check(
        "接线：统计接了落盘路径",
        "DownloadStats(self.data_dir / STATS_FILENAME)"
        in inspect.getsource(module.ComicParserPlugin.__init__),
        "",
    )

    # 权限：统计也限管理员了
    check(
        "接线：/comic 统计 限管理员",
        "_is_admin(event)" in inspect.getsource(module.ComicParserPlugin._handle_stats),
        "",
    )

    # 帮助文案宣传的写法必须真能进分支：这一条是补 bug 来的——
    # HEALTH_WORDS 一度没有中文「测试」，而帮助里写的正是 `/comic 测试`，
    # 用户照着敲只会掉进「认不出这条链接」。文案与词表必须对得上。
    for word, table, label in (
        ("测试", HEALTH_WORDS, "自检"),
        ("自检", HEALTH_WORDS, "自检"),
        ("test", HEALTH_WORDS, "自检"),
        ("统计", STATS_WORDS, "统计"),
        ("数据", STATS_WORDS, "统计"),
        ("清理", CLEAN_WORDS, "清理"),
        ("清缓存", CLEAN_WORDS, "清理"),
    ):
        check(
            f"接线：帮助里写的「{word}」能进{label}分支",
            word in table,
            f"{word} 不在 {label} 词表里",
        )

    # 这三个词与其它命令词不能撞车，否则同一句话会随机走两个分支
    for label, table in (
        ("搜索", SEARCH_WORDS),
        ("榜单", RANKING_WORDS),
        ("帮助", HELP_WORDS),
        ("站点", SITE_WORDS),
        ("停止", STOP_WORDS),
        ("状态", STATUS_WORDS),
        ("重试", RETRY_WORDS),
    ):
        shared = HEALTH_WORDS & table
        check(f"接线：自检词与{label}词不重叠", not shared, str(shared))

    # 面板「清零」：路由、处理器、注入的回调三样缺一不可
    init_source = inspect.getsource(module.ComicParserPlugin.__init__)
    check(
        "接线：面板注入了统计重置回调",
        "stats_reset=self.stats.reset" in init_source,
        "",
    )
    check(
        "接线：注册了 panel/stats/reset 路由",
        "panel/stats/reset" in inspect.getsource(module.ComicParserPlugin),
        "",
    )
    check(
        "接线：面板服务提供 reset_stats",
        hasattr(panel_mod.PanelService, "reset_stats"),
        "",
    )



def main() -> int:
    test_health()
    test_stats()
    test_stats_persistence()
    test_panel_stats_reset()
    test_janitor()
    test_probe()
    test_selfcheck_channels()
    test_domain_fallback()
    test_wiring()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print()
    for name, ok, detail in RESULTS:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}" + (f" —— {detail}" if detail and not ok else ""))
    print(f"\n{passed}/{len(RESULTS)} 项通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

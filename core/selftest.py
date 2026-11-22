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
from astrbot_plugin_comic_parser.domain import PluginSettings  # noqa: E402
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

    # Pillow 在多个模块顶层导入，缺了插件加载不起来——不开长图、不开打码同样是故障。
    # 以前这里返回 None（认为没事），等于把致命问题说成没事。
    report = health_mod.check(make_settings(pack_format="zip"), {}, probe=probe_missing("PIL"))
    check(
        "自检：不开长图也缺 Pillow 时照样报 error",
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
        self.chapter_count = 1
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



def test_channel_wiring() -> None:
    """六处接线一致性：注册表 / 命令 / schema 模板 / default_options 必须同一个渠道集合。

    AGENTS.md 把「新增渠道六处接线」写成硬约束，漏一处渠道就跑不起来——但漏哪一处
    都不会让别的测试变红，只会在用户敲命令时暴露。这里把四处能静态检查的对齐起来：
    渠道类（registry）、`cmd_<命令>` 薄方法（main.py）、`channels.templates`（schema）、
    `default_options()`（兜底启用）。依赖与文档两处无法静态校验，不在这里。
    """
    import ast
    import importlib

    registry = importlib.import_module(f"{PACKAGE}.core.registry")
    names = {cls.name for cls in registry.CHANNEL_CLASSES}
    check(
        "接线：注册表里有渠道",
        bool(names),
        f"CHANNEL_CLASSES={sorted(names)}",
    )

    # 每个渠道类都得有 command 与 default_options()，且默认启用
    bad_options = []
    for cls in registry.CHANNEL_CLASSES:
        try:
            options = cls.default_options()
        except Exception as exc:  # 兜底实现写错时不该让整条检查崩掉
            bad_options.append(f"{cls.__name__}: {exc}")
            continue
        if not isinstance(options, dict) or not options.get("enable"):
            bad_options.append(f"{cls.__name__}: {options!r}")
    check(
        "接线：每个渠道的 default_options() 默认启用",
        not bad_options,
        "; ".join(bad_options),
    )

    # main.py 里必须有一个以该渠道全称为名的命令装饰器
    main_src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_src)
    commands: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            func = deco.func
            if not (isinstance(func, ast.Attribute) and func.attr == "command"):
                continue
            if deco.args and isinstance(deco.args[0], ast.Constant):
                commands.add(str(deco.args[0].value))
    missing_cmds = sorted(names - commands)
    check(
        "接线：每个渠道在 main.py 里都有同名命令",
        not missing_cmds,
        f"缺命令: {missing_cmds}",
    )

    # schema 的 channels.templates 必须有同名模板，否则 WebUI 里配不了该渠道
    schema_path = PLUGIN_ROOT / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    templates = schema.get("channels", {}).get("templates") or {}
    template_keys = set(templates) if isinstance(templates, dict) else set()
    missing_tpl = sorted(names - template_keys)
    extra_tpl = sorted(template_keys - names)
    check(
        "接线：schema 模板与注册表渠道一一对应",
        not missing_tpl and not extra_tpl,
        f"缺模板: {missing_tpl}；多余模板: {extra_tpl}",
    )

    # 模板里声明的字段，渠道必须真的会读（用 opt_* 读）；否则用户在 WebUI 里填了也没用。
    # 反向不要求：default_options() 可以不写某些字段（留空由 opt_* 兜底），
    # 例如 jmcomic 的域名清单默认就是空。
    unread: list[str] = []
    for cls in registry.CHANNEL_CLASSES:
        template = templates.get(cls.name) or {}
        items = set((template.get("items") or {}).keys())
        if not items:
            continue
        source = (PLUGIN_ROOT / "core" / "channels" / cls.name).rglob("*.py")
        body = "\n".join(
            path.read_text(encoding="utf-8")
            for path in source
            if path.name != "selftest.py"
        )
        for field in sorted(items):
            if f'"{field}"' not in body:
                unread.append(f"{cls.name}.{field}")
    check(
        "接线：渠道模板里的字段都被渠道代码读取",
        not unread,
        f"未被读取: {unread[:6]}",
    )


def test_channel_boundaries() -> None:
    """渠道边界：`core/channels/` 只能碰 domain / utils / 自己的包内模块。

    AGENTS.md 的硬约束是「渠道只做解析 ID → 取元信息 → 下载图片」，打包、加密、
    发送、清理都属上层。越界不会立刻出错（上层模块恰好能导入），但会让渠道与产物
    流程耦合，以后换打包方式要改六个渠道。
    """
    import ast

    # 上层业务模块：渠道引用它们即越界
    forbidden = {
        "packer",
        "task",
        "group_file",
        "guard",
        "cover",
        "panel",
        "ranking",
        "ranking_card",
        "search",
        "stats",
        "janitor",
        "probe",
        "health",
    }
    violations: list[str] = []
    scanned = 0
    for path in (PLUGIN_ROOT / "core" / "channels").rglob("*.py"):
        rel = path.relative_to(PLUGIN_ROOT).as_posix()
        if path.name == "selftest.py":
            continue
        scanned += 1
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            violations.append(f"{rel}: 语法错误 {exc}")
            continue
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    leaf = alias.name.rsplit(".", 1)[-1]
                    if leaf in forbidden and "core.channels" not in alias.name:
                        violations.append(f"{rel}:{node.lineno} import {alias.name}")
                continue
            if not module:
                continue
            leaf = module.rsplit(".", 1)[-1]
            # 包内相对 import（`.option`）与 domain / utils 都合规
            if not module.startswith(".") and leaf not in forbidden:
                continue
            if leaf in forbidden and "channels" not in module:
                violations.append(f"{rel}:{node.lineno} from {module}")

    check(
        "边界：扫到了渠道文件（守卫没空转）",
        scanned >= 10,
        f"只扫到 {scanned} 个文件",
    )
    check(
        "边界：渠道没有引用打包/发送/清理等上层模块",
        not violations,
        "; ".join(violations[:5]),
    )


def test_config_sync() -> None:
    """配置项三处同步：schema 里有的项，`domain/config.py` 要读、两份 README 要写。

    AGENTS.md 把这条列为硬性要求（漏写 = 文档不全）。嵌套项在 README 里允许写成
    相对路径（`censor.style`）或完整路径（`cover.censor.style`），两种都算覆盖。
    """
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    config_src = (PLUGIN_ROOT / "domain" / "config.py").read_text(encoding="utf-8")
    readmes = {
        name: (PLUGIN_ROOT / name).read_text(encoding="utf-8")
        for name in ("README.md", "README_en.md")
    }

    def leaves(node: dict, prefix: str = "") -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if value.get("type") == "object" and "items" in value:
                out.extend(leaves(value["items"], path))
            elif value.get("type") == "template_list":
                continue
            else:
                out.append((path, key))
        return out

    items: list[tuple[str, str]] = []
    for group, node in schema.items():
        if group == "channels":  # 渠道私有项在各自模板里，不要求进顶层 README 表
            continue
        if node.get("type") == "object":
            items.extend(leaves(node["items"], group))
        else:
            items.append((group, group))

    unread = [path for path, key in items if f'"{key}"' not in config_src]
    check(
        "配置：schema 的每一项都在 domain/config.py 里被读取",
        not unread,
        f"未读取: {unread[:6]}",
    )

    for name, text in readmes.items():
        missing = [
            path
            for path, key in items
            # 嵌套项允许三种写法：完整路径（cover.censor.style）、
            # 去组名的相对路径（censor.style）、仅叶子名（style）
            if not any(
                f"`{candidate}`" in text
                for candidate in (path, path.split(".", 1)[-1], key)
            )
        ]
        check(
            f"配置：schema 的每一项都写进了 {name}",
            not missing,
            f"未写入: {missing[:6]}",
        )


def test_changelog_folding() -> None:
    """CHANGELOG 折叠结构：展开区只留 [Unreleased] + 最近 3 个版本段。

    规范把折叠列为发版动作的一部分。发版时手一抖（折多一个、把 [Unreleased] 也折了、
    或忘了加分隔说明）用户就得多点一次才看得到上一版差异，而这种错不会让任何测试变红。
    """
    import re

    path = PLUGIN_ROOT / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")

    check("CHANGELOG：[Unreleased] 段存在且未折叠", "## [Unreleased]" in text, "")
    check(
        "CHANGELOG：[Unreleased] 不在 <details> 里",
        "<summary>[Unreleased]" not in text and "<summary>Unreleased" not in text,
        "",
    )

    expanded = re.findall(r"^## (v[\d.]+) \((\d{4}-\d{2}-\d{2})\)", text, re.M)
    folded = re.findall(r"<summary>(v[\d.]+) \((\d{4}-\d{2}-\d{2})\)", text)

    check(
        "CHANGELOG：展开区是最近 3 个版本段",
        len(expanded) == 3,
        f"实际 {len(expanded)} 个: {[v for v, _ in expanded]}",
    )
    check("CHANGELOG：更早的版本已折叠", bool(folded), f"折叠 {len(folded)} 个")

    # 折叠块数量必须与 details 标签配对，否则渲染会吃掉后面所有内容
    check(
        "CHANGELOG：<details> 与 </details> 数量一致",
        text.count("<details>") == text.count("</details>"),
        f"{text.count('<details>')} 开 / {text.count('</details>')} 闭",
    )
    check(
        "CHANGELOG：每个 details 都有 summary",
        text.count("<details>") == len(folded),
        f"details {text.count('<details>')} / summary {len(folded)}",
    )

    # 折叠区前必须有分隔说明，且只出现一次
    separator = "以下为更早的历史版本"
    check(
        "CHANGELOG：折叠区前有分隔说明且只出现一次",
        text.count(separator) == 1,
        f"出现 {text.count(separator)} 次",
    )

    # 破坏性变更版本必须保持展开（本仓目前没有，将来有时这条会挡住误折）
    breaking = re.findall(r"^## (v[\d.]+).*\n\n> ⚠️ 破坏性变更", text, re.M)
    folded_breaking = [
        ver
        for ver, _ in folded
        if ver in set(breaking)
    ]
    check(
        "CHANGELOG：带破坏性变更标记的版本没被折叠",
        not folded_breaking,
        f"被折叠: {folded_breaking}",
    )


def test_silent_excepts() -> None:
    """空捕获必须有理由：`except ...: pass/continue` 且不记日志时，行内或上一行要有注释。

    规范禁止静默吞异常。这类写法多数是合理兜底（遍历时跳过坏文件、清理失败不阻塞），
    但没写理由就看不出是「想清楚了」还是「顺手吞了」。异常类型已精确的不强求记日志。
    """
    import ast

    offenders: list[str] = []
    scanned = 0
    targets = [PLUGIN_ROOT / "main.py"]
    for sub in ("core", "domain", "utils"):
        targets.extend((PLUGIN_ROOT / sub).rglob("*.py"))

    for path in targets:
        rel = path.relative_to(PLUGIN_ROOT).as_posix()
        if rel.endswith("selftest.py"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        scanned += 1
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            logs = any(
                isinstance(st, ast.Expr)
                and isinstance(st.value, ast.Call)
                and isinstance(st.value.func, ast.Attribute)
                and getattr(st.value.func.value, "id", None) == "logger"
                for st in node.body
            )
            silent = all(isinstance(st, (ast.Pass, ast.Continue)) for st in node.body)
            if not silent or logs:
                continue
            line = lines[node.lineno - 1]
            has_inline = "#" in line.split("except", 1)[-1]
            has_above = node.lineno >= 2 and lines[node.lineno - 2].strip().startswith("#")
            if not has_inline and not has_above:
                offenders.append(f"{rel}:{node.lineno}")

    check(
        "错误处理：扫到了源文件（守卫没空转）",
        scanned >= 20,
        f"只扫到 {scanned} 个文件",
    )
    check(
        "错误处理：静默捕获都写了理由",
        not offenders,
        "; ".join(offenders[:6]),
    )


def test_config_defaults() -> None:
    """README 配置表里的默认值要与 schema 的 default 一致。

    用户在 WebUI 里看到的是 schema 的默认值，在 README 里读到的是表格——两边不一致时
    用户会以为「不改就是这个值」，实际行为却不同。用中文名（description）定位行，
    因为 key 会跨组重名（timeout / enable / cache_ttl 各有多处）。
    """
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    lines = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8").splitlines()

    def leaves(node: dict, prefix: str = "") -> list[tuple[str, str, object, str]]:
        out: list[tuple[str, str, object, str]] = []
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if value.get("type") == "object" and "items" in value:
                out.extend(leaves(value["items"], path))
            elif value.get("type") == "template_list":
                continue
            else:
                out.append(
                    (
                        path,
                        key,
                        value.get("default"),
                        value.get("description", ""),
                        list(value.get("labels") or ()),
                        list(value.get("options") or ()),
                    )
                )
        return out

    items: list[tuple[str, str, object, str, list, list]] = []
    for group, node in schema.items():
        if group == "channels":
            continue
        if node.get("type") == "object":
            items.extend(leaves(node["items"], group))
        else:
            items.append(
                (
                    group,
                    group,
                    node.get("default"),
                    node.get("description", ""),
                    list(node.get("labels") or ()),
                    list(node.get("options") or ()),
                )
            )

    mismatches: list[str] = []
    matched = 0
    for path, key, default, desc, labels, options in items:
        if default is None:
            continue
        if isinstance(default, bool):
            expected = "true" if default else "false"
        else:
            expected = str(default)
        # README 对嵌套项写相对路径（`censor.style`），配置名也常省掉单位后缀
        # （「单本下载超时（秒）」在表里写成「单本下载超时」），所以两者都要认
        short_path = path.split(".", 1)[-1]
        bare_desc = desc.split("（", 1)[0].strip() if desc else ""
        row = None
        for line in lines:
            if not line.startswith("|") or not desc:
                continue
            if bare_desc not in line and desc not in line:
                continue
            if any(f"`{candidate}`" in line for candidate in (key, path, short_path)):
                row = line
                break
        if row is None:
            continue  # 未写进表由 test_config_sync 负责
        matched += 1
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) < 3:
            continue
        documented = cells[2]
        # 允许「week（周榜）」「文字版」这类带 label 的写法，也允许「见默认值」「空」
        if documented in ("见默认值", "空"):
            continue
        if documented.lower().startswith(expected.lower()):
            continue
        # 数值等价也算一致：schema 写 45.0、表里写 45
        try:
            if float(documented) == float(default):
                continue
        except (TypeError, ValueError):
            pass
        # 下拉项允许直接写 label（如 `text` 写成「文字版」）
        if isinstance(default, str) and default in options:
            idx = options.index(default)
            if idx < len(labels) and documented.startswith(str(labels[idx])):
                continue
        mismatches.append(f"{path}: schema={expected} README={documented}")

    # 匹配不到行时静默通过等于空守卫：至少要有几十行真比过
    check(
        "配置：README 配置表能按中文名匹配到足够多行",
        matched >= 60,
        f"只匹配到 {matched} 行",
    )
    check(
        "配置：README 表里的默认值与 schema 一致",
        not mismatches,
        "; ".join(mismatches[:5]),
    )


def test_declared_dependencies() -> None:
    """顶层导入的第三方模块必须出现在 requirements.txt 里。

    惰性导入（写在函数体内）允许缺——那些地方有自己的缺失提示；但写在模块顶层的
    导入一旦缺失，整个插件加载就会失败，报错还指向业务模块、看不出是缺依赖。
    AstrBot 运行时自带的那几个（astrbot / aiohttp / certifi）除外。
    """
    import ast

    # AstrBot 运行时自带，或标准库，不算「需要声明的第三方依赖」
    allowed = {
        "__future__",
        "astrbot",
        "aiohttp",
        "certifi",
    }
    # 包名 → import 名可能不同，这里按 import 名声明映射
    package_of = {
        "PIL": "Pillow",
        "fitz": "pymupdf",
    }
    # Python 3.10+ 自带 stdlib_module_names，不必手抄清单（手抄一定会漏）
    stdlib = set(sys.stdlib_module_names)
    # numpy 是 onnxruntime 的伴随依赖，只在打码路径惰性用到，缺了自检会点名
    optional = {"numpy"}

    requirements = (PLUGIN_ROOT / "requirements.txt").read_text(encoding="utf-8")
    declared = {
        line.split(">")[0].split("=")[0].split("<")[0].strip().lower()
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    undeclared: list[str] = []
    targets = [PLUGIN_ROOT / "main.py"]
    for sub in ("core", "domain", "utils"):
        targets.extend((PLUGIN_ROOT / sub).rglob("*.py"))
    for path in targets:
        rel = path.relative_to(PLUGIN_ROOT).as_posix()
        if rel.endswith("selftest.py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        # 只看模块顶层（tree.body 里直接的 Import/ImportFrom）
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in allowed or name in stdlib or name in optional:
                    continue
                if name.startswith("astrbot_plugin"):
                    continue
                pkg = package_of.get(name, name).lower()
                if pkg not in declared:
                    undeclared.append(f"{rel}: {name}")
    check(
        "依赖：顶层导入的第三方模块都在 requirements.txt 里",
        not undeclared,
        "; ".join(sorted(set(undeclared))[:6]),
    )


def test_dependency_import_style() -> None:
    """自检清单里的依赖若在模块顶层被导入，缺失就会让插件加载失败，判定必须一律报错。

    反过来：只在函数体内惰性导入的依赖，缺了只影响对应功能，按「当前配置会不会用到」
    判定才合理。这条守卫锁住这个对应关系——顶层导入的依赖不能在 _requirement_reason
    里返回 None。
    """
    import ast
    import inspect

    deps = {dep.module for dep in health_mod.DEPENDENCIES}
    top_level: dict[str, list[str]] = {}
    targets = [PLUGIN_ROOT / "main.py"]
    for sub in ("core", "domain", "utils"):
        targets.extend((PLUGIN_ROOT / sub).rglob("*.py"))
    for path in targets:
        rel = path.relative_to(PLUGIN_ROOT).as_posix()
        if rel.endswith("selftest.py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in deps:
                    top_level.setdefault(name, []).append(rel)

    source = inspect.getsource(health_mod._requirement_reason)
    unconditional = []
    for module, files in top_level.items():
        # 该依赖的分支里不能出现 `return None`（意味着「这次不算问题」）
        branch = source.split(f'dep.module == "{module}"', 1)
        if len(branch) < 2:
            unconditional.append(f"{module}: 自检里没有对应分支（{files[:2]}）")
            continue
        body = branch[1].split("if dep.module ==", 1)[0]
        if "return None" in body:
            unconditional.append(
                f"{module}: 顶层导入（{files[0]}）却可能被判为无事"
            )
    check(
        "自检：顶层导入的依赖一律报故障",
        not unconditional,
        "; ".join(unconditional),
    )


def test_settings_fields_used() -> None:
    """`PluginSettings` 的字段必须真的被业务代码读到。

    配置项映射进 dataclass 却没人读，用户在 WebUI 里改了没有任何效果——这类「死配置」
    既不报错也不影响测试。这里扫 core/ 与 main.py 的三种读法：`settings.x`、
    `getattr(settings, "x")`、以及同一 dataclass 内部的方法（如 ranking_size_ok）。
    """
    import ast
    import re

    config_src = (PLUGIN_ROOT / "domain" / "config.py").read_text(encoding="utf-8")
    tree = ast.parse(config_src)
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "PluginSettings":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.add(stmt.target.id)

    used: set[str] = set()
    targets = [PLUGIN_ROOT / "main.py"]
    for sub in ("core", "domain"):
        targets.extend((PLUGIN_ROOT / sub).rglob("*.py"))
    for path in targets:
        if path.name == "selftest.py":
            continue
        src = path.read_text(encoding="utf-8")
        used |= set(re.findall(r"settings\.([a-z_][a-z0-9_]*)", src))
        used |= set(
            re.findall(
                r"getattr\(\s*(?:self\.)?settings\s*,\s*[\"']([a-z_][a-z0-9_]*)[\"']",
                src,
            )
        )
        used |= set(re.findall(r"\bself\.([a-z_][a-z0-9_]*)\b", src)) & fields

    dead = sorted(fields - used)
    check(
        "配置：PluginSettings 的字段都被业务代码读取",
        not dead,
        f"死配置: {dead[:8]}",
    )


def test_view_endpoints() -> None:
    """视图里调用的 endpoint 必须都注册过，且路由带插件名前缀。

    前端调错路径时页面只会在浏览器控制台报 404，插件日志与自测都看不见；
    反过来注册了没人调只是冗余。两边对齐一次能省掉一轮「面板点了没反应」。
    """
    import re

    main_src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    registered = set(
        re.findall(r'f"/\{PLUGIN_NAME\}/([^"]+)"', main_src)
    )
    # 路由必须全部带插件名前缀（规范硬要求）；上一条正则本身就要求了前缀，
    # 这里再确认没有裸路由混进来
    bare = re.findall(r'register_web_api\(\s*f?"(?!/\{PLUGIN_NAME\})', main_src)
    check(
        "视图：所有 Web API 路由都带插件名前缀",
        not bare,
        f"疑似裸路由 {len(bare)} 处",
    )

    app_js = PLUGIN_ROOT / "views" / "panel" / "app.js"
    if not app_js.is_file():
        check("视图：app.js 存在", False, "找不到 views/panel/app.js")
        return
    js = app_js.read_text(encoding="utf-8")
    block = js.split("EP: {", 1)
    called: set[str] = set()
    if len(block) > 1:
        body = block[1].split("}", 1)[0]
        called = set(re.findall(r':\s*"([^"]+)"', body))

    unknown = sorted(called - registered)
    check(
        "视图：调用的 endpoint 都注册过",
        not unknown,
        f"未注册: {unknown[:6]}",
    )


def test_no_secret_logging() -> None:
    """日志不得回显代理地址、密码与 Cookie。

    代理串常带内网主机与账号密码，而日志会被贴进群、发进 issue。`proxy_summary()`
    已经刻意只返回「直连 / 代理」这种来源描述，这里守住别处别又直接把地址打出来。
    """
    import re

    # 形如 `{proxy}`、`{self.settings.proxy}`、`{password}`、`{cookie}` 直接插值进日志
    risky = re.compile(
        r"logger\.(?:info|warning|error|debug)\([^)]*\{[^}]*(?:\bproxy\b|\bpassword\b|\bsecret\b|\bcookie\b)[^}]*\}",
        re.IGNORECASE,
    )
    hits: list[str] = []
    targets = [PLUGIN_ROOT / "main.py"]
    for sub in ("core",):
        targets.extend((PLUGIN_ROOT / sub).rglob("*.py"))
    for path in targets:
        rel = path.relative_to(PLUGIN_ROOT).as_posix()
        if rel.endswith("selftest.py"):
            continue
        source = path.read_text(encoding="utf-8")
        # 多行调用也要能匹配：先压掉换行再扫
        flat = source.replace("\n", " ")
        for match in risky.finditer(flat):
            hits.append(f"{rel}: {match.group(0)[:70]}")
    check(
        "安全：日志里没有直接插值代理地址 / 密码 / Cookie",
        not hits,
        "; ".join(hits[:3]),
    )


def test_readme_config_names() -> None:
    """README 用「」引用的配置名必须与 schema 的 description 一字不差。

    规范要求配置项在文档里用「中文名（`key`）」形式，中文名取自 description。
    写成简写（「上传到群文件」对应的是「下载后上传到群文件」）时，用户照着在
    WebUI 里搜不到——而这类偏差不会让任何测试变红。
    """
    import re

    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    names: set[str] = set()

    def walk(node: dict) -> None:
        for value in node.values():
            if not isinstance(value, dict):
                continue
            desc = value.get("description")
            if isinstance(desc, str) and desc:
                names.add(desc)
                # 「单渠道搜索超时（秒）」这类带单位后缀的，允许只引用前半段
                names.add(re.sub(r"（[^）]*）$", "", desc).strip())
            if isinstance(value.get("items"), dict):
                walk(value["items"])

    walk(schema)

    # 引用里会混进命令名、错误文案、文件夹名等，只挑「看起来是配置名」的：
    # 出现在 schema description 里、或至少包含一个 schema 名的子串才算候选
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    quoted = set(re.findall(r"「([^」]{2,20})」", readme))
    # 只把「是某个配置名的严格子串、且本身够长」当简写；
    # 「章节」「下载」这类短词是正常叙述，不算引用配置项
    suspects = [
        q
        for q in quoted
        if q not in names
        and len(q) >= 4
        and any(q in n and q != n for n in names if len(n) >= 4)
    ]
    check(
        "文档：README 引用的配置名与 schema 一致",
        not suspects,
        f"疑似简写: {suspects[:6]}",
    )


def test_config_dirty_values() -> None:
    """配置解析必须扛住脏值：用户手改 JSON、老版本残留、类型写错都不能让插件崩。

    规范要求「类型转换失败一律回落默认值」。这类问题只在真实部署里出现——用户把
    `timeout` 写成 "abc"、把列表写成字符串、把分组写成 null——而自测通常只喂正常值。
    """
    defaults = PluginSettings.from_config({})
    dirty_cases = {
        "字符串塞进 int": {"download": {"timeout": "abc", "retry_times": None}},
        "负数与零": {"download": {"max_concurrent_tasks": -5}, "search": {"page_size": 0}},
        "枚举写错": {"packing": {"pack_format": "nonsense"}},
        "列表写成字符串": {"access": {"whitelist": "notalist"}},
        "分组写成 null": {"download": None, "packing": None, "search": None},
        "类型全错": {"search": {"enable": "yes", "timeout": -1}},
    }
    crashed: list[str] = []
    for label, config in dirty_cases.items():
        try:
            settings = PluginSettings.from_config(config)
        except Exception as exc:  # noqa: BLE001 - 就是要抓「解析崩溃」这一类
            crashed.append(f"{label}: {type(exc).__name__}")
            continue
        # 兜底后必须仍是可用值：这几个字段 0 都不合法（retry_times 允许 0，单列）
        for field in ("download_timeout", "max_concurrent_tasks"):
            value = getattr(settings, field, None)
            if not isinstance(value, int) or value <= 0:
                crashed.append(f"{label}: {field}={value!r}")
        retries = getattr(settings, "download_retry_times", None)
        if not isinstance(retries, int) or retries < 0:
            crashed.append(f"{label}: download_retry_times={retries!r}")
    check(
        "配置：脏值一律回落到可用默认值",
        not crashed,
        "; ".join(crashed),
    )
    check(
        "配置：空配置等于全默认",
        defaults.download_timeout > 0 and defaults.max_concurrent_tasks > 0,
        "",
    )


def test_changelog_wording() -> None:
    """CHANGELOG 的展开区只写用户能感知的变化，不出现内部实现细节。

    规范明确禁止：类名/函数名、文件路径、数据结构、算法、缓存与并发策略。这些
    条目用户读了也用不上，还会让「要不要升级」的判断变难。只查展开区——折叠的
    历史版本保持原样不动。
    """
    import re

    text = (PLUGIN_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    expanded = text.split("以下为更早的历史版本")[0]

    patterns = {
        "函数/方法名": r"`_[a-z][a-z0-9_]*`|`[a-z_]+\\(\\)`",
        "文件路径": r"`(?:core|domain|utils|main)\.[a-z]|`[a-z_]+/[a-z_]+\.py`",
        "并发原语": r"asyncio\.|Semaphore|ProcessPool|gather\(",
        "数据结构": r"dataclass|@dataclass|NamedTuple|frozenset",
    }
    hits: list[str] = []
    for label, pattern in patterns.items():
        for match in re.finditer(pattern, expanded):
            hits.append(f"{label}: {match.group(0)}")
    check(
        "文档：CHANGELOG 展开区没有内部实现细节",
        not hits,
        "; ".join(hits[:5]),
    )


def test_exception_specificity() -> None:
    """禁止裸 `except:` 与 `except BaseException`。

    裸捕获会连 KeyboardInterrupt / SystemExit / asyncio.CancelledError 一起吃进去，
    任务取消与插件卸载的语义会被悄悄改掉。规范要求精确捕获异常类型；确实需要
    兜底的用 `except Exception` 并写清理由（另有守卫管注释）。
    """
    import ast

    offenders: list[str] = []
    handlers = 0
    targets = [PLUGIN_ROOT / "main.py"]
    for sub in ("core", "domain", "utils"):
        targets.extend((PLUGIN_ROOT / sub).rglob("*.py"))
    for path in targets:
        rel = path.relative_to(PLUGIN_ROOT).as_posix()
        if rel.endswith("selftest.py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            handlers += 1
            if node.type is None:
                offenders.append(f"{rel}:{node.lineno} 裸 except:")
            elif isinstance(node.type, ast.Name) and node.type.id == "BaseException":
                offenders.append(f"{rel}:{node.lineno} except BaseException")
    check(
        "错误处理：扫到了足够多的 except 分支（守卫没空转）",
        handlers >= 50,
        f"只扫到 {handlers} 个",
    )
    check(
        "错误处理：没有裸 except 或 BaseException",
        not offenders,
        "; ".join(offenders[:5]),
    )


def test_error_messages_actionable() -> None:
    """用户可见的错误必须说清「接下来能做什么」，而不只是「出错了」。

    规范要求「降级链路清晰：首选方案 → 兜底方案 → 占位文本」，用户看到的每句话都
    应该能照着做。这条只查 `PluginError` 族的字面量消息——内部异常（如 EHQuotaExceeded
    在上层会被转译）不在此列。
    """
    import ast

    # 「说明下一步」的常见措辞；命中任意一个即认为可操作
    hints = (
        "请", "可以", "建议", "试试", "重试", "再试", "重新", "检查", "改用", "换",
        "确认", "稍后", "联系", "打开", "填写", "配置", "去掉", "不写", "等",
        "过几小时", "管理员", "登录", "代理", "就要", "直接",
    )
    # 终态结论：用户做什么都改变不了，写清「是什么」就够了，硬塞下一步反而是噪音
    terminal = (
        "已下架", "已被删除", "被站点删除", "不存在", "不可用", "看不到", "不可见", "被删除",
        "中断", "取消下载", "已用尽", "配额", "不是合法", "不是预期", "结构可能已变化",
        "需要登录", "需要付费", "占位图", "风控",
    )
    # 只报「纯陈述结论、既无下一步也不够具体」的
    offenders: list[str] = []
    targets = [PLUGIN_ROOT / "main.py"]
    for sub in ("core", "domain"):
        targets.extend((PLUGIN_ROOT / sub).rglob("*.py"))
    for path in targets:
        rel = path.relative_to(PLUGIN_ROOT).as_posix()
        if rel.endswith("selftest.py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            if not node.exc.args:
                continue
            first = node.exc.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            message = first.value
            if any(word in message for word in hints):
                continue
            if any(word in message for word in terminal):
                continue
            offenders.append(f"{rel}:{node.lineno} {message[:40]}")
    check(
        "错误提示：用户可见的错误都带下一步",
        not offenders,
        f"缺指引 {len(offenders)} 条: {'; '.join(offenders[:3])}",
    )


def test_help_text_config_names() -> None:
    """帮助文案里「」引用的配置名必须与 schema description 一致。

    帮助文案会直接发到群里，用户照着去设置页找开关。写成简称（「启用搜索」对应
    「启用搜索」没问题，但「搜索开关」就对不上）时用户找不到。与 README 的同类
    守卫分开：帮助文案在 main.py 里，判定也只针对看起来像配置名的引用。
    """
    import re

    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    names: set[str] = set()

    def walk(node: dict) -> None:
        for value in node.values():
            if not isinstance(value, dict):
                continue
            desc = value.get("description")
            if isinstance(desc, str) and desc:
                names.add(desc)
            if isinstance(value.get("items"), dict):
                walk(value["items"])

    walk(schema)

    main_src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    quoted = set(re.findall(r"「([^」]{3,24})」", main_src))
    # 只挑「包含配置名关键词」的引用当候选，避免把「全部」「关闭」这类正常词算进来
    keywords = ("启用", "开关", "设置", "清理", "保存", "模板", "密码", "超时", "上限", "缓存")
    suspects = [
        q
        for q in quoted
        if q not in names
        and any(k in q for k in keywords)
        and not q.startswith(("下载", "上传"))
    ]
    check(
        "文档：帮助文案引用的配置名与 schema 一致",
        not suspects,
        f"疑似对不上: {suspects[:6]}",
    )


def test_metadata_consistency() -> None:
    """插件名与描述在 metadata / i18n / README 头部三处必须一致。

    规范要求中文 README 的 h1 取 `display_name`、desc 取 `desc`，i18n 的
    `metadata.display_name` / `description` 也是同一份文案。三处不一致时，用户在
    插件市场、详情页、README 里看到的是三个名字。
    """
    import re

    # metadata.yaml 是唯一版本源与展示源，这里只读它做基准
    raw = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    display = re.search(r"^display_name:\s*(.+)$", raw, re.M)
    desc = re.search(r"^desc:\s*(.+)$", raw, re.M)
    if not display or not desc:
        check("元数据：metadata.yaml 有 display_name 与 desc", False, "")
        return
    expected_name = display.group(1).strip()
    expected_desc = desc.group(1).strip()

    zh = json.loads(
        (PLUGIN_ROOT / ".astrbot-plugin" / "i18n" / "zh-CN.json").read_text(encoding="utf-8")
    )
    check(
        "元数据：i18n 的 display_name 与 metadata 一致",
        zh.get("metadata", {}).get("display_name") == expected_name,
        f"i18n={zh.get('metadata', {}).get('display_name')!r}",
    )
    check(
        "元数据：i18n 的 description 与 metadata 一致",
        zh.get("metadata", {}).get("description") == expected_desc,
        "",
    )

    head = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")[:1200]
    h1 = re.search(r"<h1>(.*?)</h1>", head)
    body = re.search(r"<p><strong>(.*?)</strong></p>", head)
    check(
        "元数据：README 的 h1 等于 display_name",
        bool(h1) and h1.group(1).strip() == expected_name,
        f"README={h1.group(1) if h1 else None!r}",
    )
    check(
        "元数据：README 的描述行等于 desc",
        bool(body) and body.group(1).strip() == expected_desc,
        "",
    )


def test_view_i18n_keys() -> None:
    """视图里 `t("views.<name>.<key>")` 用到的键必须在两份 locale 里都存在。

    缺键时页面会显示代码里写死的 fallback——中文用户看不出问题，英文用户会看到
    中文兜底；这类缺失不会报错，只会静默降级。
    """
    import re

    views_dir = PLUGIN_ROOT / "views"
    if not views_dir.is_dir():
        check("视图：存在 views/ 目录", False, "没有视图目录")
        return
    used: set[str] = set()
    for js in views_dir.rglob("*.js"):
        source = js.read_text(encoding="utf-8")
        used |= set(re.findall(r"[\"'](views\.[a-z0-9_]+\.[a-z0-9_]+)[\"']", source))
    # 必须真扫到键：匹配不到时静默通过等于空守卫（正则写错也发现不了）
    check(
        "视图：从 JS 里扫到了 i18n 键",
        bool(used),
        f"扫到 {len(used)} 个键；若为 0 说明匹配规则失效",
    )

    missing: list[str] = []
    for locale in ("zh-CN", "en-US"):
        path = PLUGIN_ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        views = data.get("views", {})
        for key in sorted(used):
            _, view_name, leaf = key.split(".", 2)
            if leaf not in (views.get(view_name) or {}):
                missing.append(f"{locale}:{key}")
    check(
        "视图：用到的 i18n 键在两份 locale 里都存在",
        not missing,
        "; ".join(missing[:5]),
    )


def test_command_annotations() -> None:
    """命令/事件方法的签名必须完整：`cmd_*` 标注 `AsyncGenerator[MessageEventResult, None]`。

    规范要求「函数签名必须完整类型注解；用 yield 返回结果的命令/事件处理方法标注为
    AsyncGenerator[MessageEventResult, None]」。写错不会影响运行，但会误导读代码的人
    ——尤其 cmd_ 方法里既有 yield 又有 return 时，注解是唯一的契约说明。
    """
    import ast

    main_src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_src)
    offenders: list[str] = []
    seen_cmds = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorated = False
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr in ("command", "event_message_type", "command_group")
            ):
                decorated = True
                break
        if not decorated:
            continue
        if node.returns is None:
            offenders.append(f"{node.name}: 缺返回注解")
            continue
        rendered = ast.unparse(node.returns)
        if not node.name.startswith("cmd_"):
            continue  # 命令组方法返回 None，不必是生成器
        seen_cmds += 1
        if "AsyncGenerator" not in rendered:
            offenders.append(f"{node.name}: {rendered}")
    check(
        "类型：扫到了 cmd_ 方法（守卫没空转）",
        seen_cmds >= 8,
        f"只扫到 {seen_cmds} 个",
    )
    check(
        "类型：cmd_ 方法都标注了 AsyncGenerator 返回类型",
        not offenders,
        "; ".join(offenders[:5]),
    )


def test_heavy_work_offloaded() -> None:
    """异步函数里不得直接跑重活（图片编解码、ONNX 推理、打包）。

    规范要求「渲染/计算重活必须走 `asyncio.to_thread` 或子进程」，否则一个几十兆的
    长图拼接就能把整个 AstrBot 事件循环卡住。这条只看已知的重活函数名，不试图做
    通用的耗时分析——那种判断必然误报。
    """
    import ast

    heavy_calls = {
        "censor_cover": "封面打码",
        "analyze": "NSFW 推理",
        "pack": "打包",
        "render": "卡片渲染",
    }
    offenders: list[str] = []
    async_funcs = 0
    for path in (PLUGIN_ROOT / "core").rglob("*.py"):
        rel = path.relative_to(PLUGIN_ROOT).as_posix()
        if rel.endswith("selftest.py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            async_funcs += 1
            # 收集本函数内 to_thread 的目标（按属性名匹配）
            offloaded: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    rendered = ast.unparse(sub)
                    if "to_thread" in rendered or "run_in_executor" in rendered:
                        for attr in heavy_calls:
                            if attr in rendered:
                                offloaded.add(attr)
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else (func.id if isinstance(func, ast.Name) else "")
                )
                if name in heavy_calls and name not in offloaded:
                    offenders.append(
                        f"{rel}:{sub.lineno} {node.name} 直接调用 {name}（{heavy_calls[name]}）"
                    )
    check(
        "并发：扫到了异步函数（守卫没空转）",
        async_funcs >= 30,
        f"只扫到 {async_funcs} 个",
    )
    check(
        "并发：异步函数里的重活都走了 to_thread",
        not offenders,
        "; ".join(offenders[:5]),
    )


def test_schema_integrity() -> None:
    """`_conf_schema.json` 自身要自洽：slider 范围包住默认值、options 与 labels 等长。

    WebUI 按 schema 渲染控件：`default` 落在 slider 范围外时滑杆会跳到边界、用户以为
    保存了却没存；`options` 与 `labels` 不等长时下拉框会缺项或错位。这类问题不报错，
    只在页面上表现得很怪。
    """
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    problems: list[str] = []
    seen = 0

    def walk(node: dict, prefix: str = "") -> None:
        nonlocal seen
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if not isinstance(value, dict):
                continue
            seen += 1
            slider = value.get("slider")
            default = value.get("default")
            if isinstance(slider, dict) and default is not None:
                low, high = slider.get("min"), slider.get("max")
                if low is not None and high is not None and low > high:
                    problems.append(f"{path}: min {low} > max {high}")
                if isinstance(default, (int, float)):
                    if low is not None and default < low:
                        problems.append(f"{path}: default {default} < min {low}")
                    if high is not None and default > high:
                        problems.append(f"{path}: default {default} > max {high}")
            options = value.get("options")
            labels = value.get("labels")
            if isinstance(options, list):
                if isinstance(labels, list) and len(labels) != len(options):
                    problems.append(
                        f"{path}: options {len(options)} 项 vs labels {len(labels)} 项"
                    )
                if default is not None and default not in options:
                    problems.append(f"{path}: default {default!r} 不在 options 里")
            if isinstance(value.get("items"), dict):
                walk(value["items"], path)

    walk(schema)
    check(
        "配置：扫到了 schema 节点（守卫没空转）",
        seen >= 50,
        f"只扫到 {seen} 个",
    )
    check(
        "配置：schema 自身自洽（slider 范围 / options 与 labels / 默认值）",
        not problems,
        "; ".join(problems[:5]),
    )


def test_cache_bounded() -> None:
    """按会话/关键词累积的缓存必须在过期后真的被释放。

    防抖缓存原来只清「当前会话」那一桶，来过的每个会话都会永久留下一个空 dict；
    群多的部署跑久了就是几万个空桶。这里模拟大量会话 + 过期，确认缓存会缩回去。
    """
    import time

    from astrbot_plugin_comic_parser.core.guard import AccessGuard  # noqa: PLC0415

    settings = PluginSettings.from_config({"access": {"debounce_interval": 1}})
    guard = AccessGuard(settings)
    for index in range(500):
        guard.is_duplicate(f"session-{index}", "jm:123")
    filled = len(guard._cache)

    time.sleep(1.2)
    # 任意一次新请求都会触发清扫
    guard.is_duplicate("fresh-session", "jm:123")
    after = len(guard._cache)

    check(
        "缓存：防抖缓存在过期后会被释放",
        filled >= 500 and after <= 2,
        f"过期前 {filled} 个会话，过期后 {after} 个",
    )

    # 防抖语义不能被清扫改坏
    guard2 = AccessGuard(PluginSettings.from_config({"access": {"debounce_interval": 5}}))
    first = guard2.is_duplicate("s", "jm:1")
    again = guard2.is_duplicate("s", "jm:1")
    other = guard2.is_duplicate("s", "jm:2")
    guard2.forget("s", "jm:1")
    after_forget = guard2.is_duplicate("s", "jm:1")
    check(
        "缓存：防抖语义不受清扫影响",
        first is False and again is True and other is False and after_forget is False,
        f"{first}/{again}/{other}/{after_forget}",
    )


def test_readme_chapter_examples() -> None:
    """README 里演示的章节范围写法必须都能被解析器接受。

    文档里写「也支持 `1-5,9`」而解析器不认时，用户照着敲只会得到一句「看不懂」。
    这里把 README 命令表里出现的范围写法逐个喂给 `ChapterSpec.parse`。
    """
    import re

    from astrbot_plugin_comic_parser.domain.chapters import ChapterSpec  # noqa: PLC0415

    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    # 命令表里「范围写法：`3`、`1-20`、`-20`、`100-`、`最新5`、`1-5,9`（多段）」
    row = ""
    for line in readme.splitlines():
        if "范围写法" in line and line.startswith("|"):
            row = line
            break
    if not row:
        check("文档：README 有章节范围写法的说明行", False, "")
        return

    # 取反引号里的全部示例，跳过明显不是范围写法的（命令名、文件名）
    candidates = [
        raw
        for raw in re.findall(r"`([^`]+)`", row)
        if not raw.startswith(("/", "章节")) and "章节" not in raw
    ]
    failures: list[str] = []
    for raw in candidates:
        try:
            ChapterSpec.parse(raw)
        except Exception as exc:  # noqa: BLE001 - 就是要抓「文档写了但解析不了」
            failures.append(f"{raw}: {exc}")
    check(
        "文档：README 的章节范围示例都能被解析",
        bool(candidates) and not failures,
        f"试了 {candidates}；失败 {failures}",
    )


def test_resume_cleanup() -> None:
    """断点清理只删真过期的：新鲜断点要留着给 `/comic retry` 接着下。

    规范要求「断点保留时长」到期才清，且不能碰正在下载的目录。这里造一个新、一个旧
    的断点目录，确认旧的真被删、新的还在。
    """
    import os
    import tempfile
    import time

    from astrbot_plugin_comic_parser.core import task as task_mod  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="resume-guard-"))
    try:
        fresh = tmp / "resume" / "jm-111"
        fresh.mkdir(parents=True)
        (fresh / "001.jpg").write_bytes(b"x" * 64)

        stale = tmp / "resume" / "jm-222"
        stale.mkdir(parents=True)
        (stale / "001.jpg").write_bytes(b"x" * 64)
        ancient = time.time() - 99999
        for target in [stale, *stale.rglob("*")]:
            os.utime(target, (ancient, ancient))

        class _FakeTask:
            temp_root = tmp
            channel = type("C", (), {"name": "jm"})()
            settings = PluginSettings.from_config({"download": {"resume_ttl": 3600}})
            _prepare_work_dir = task_mod.DownloadTask._prepare_work_dir

        _FakeTask()._prepare_work_dir("333")
        check("断点：新鲜断点被保留", fresh.exists(), "新鲜断点被误删")
        check("断点：过期断点被清理", not stale.exists(), "过期断点没清")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_pack_fails_closed() -> None:
    """设了密码却没有加密组件时必须失败关闭，绝不静默产出未加密文件。

    这是安全语义：用户以为文件加密了，实际谁都能打开。规范把它写成硬约束
    （「设置了密码却缺少加密依赖时**失败关闭**」）。这里把 `PYZIPPER_AVAILABLE`
    临时打成 False，确认 zip 打包返回失败而不是「悄悄不加密」。
    """
    import tempfile

    from astrbot_plugin_comic_parser.core import packer as packer_mod  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="pack-guard-"))
    try:
        src = tmp / "src"
        src.mkdir()
        # 造两张能通过体积下限的小图（PNG 头 + 填充）
        for index in range(2):
            (src / f"{index:03d}.png").write_bytes(
                b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096
            )

        original = packer_mod.PYZIPPER_AVAILABLE
        packer_mod.PYZIPPER_AVAILABLE = False
        try:
            packer = packer_mod.Packer("zip", password="secret")
            result = packer.pack(src, "测试", tmp / "out")
        finally:
            packer_mod.PYZIPPER_AVAILABLE = original

        check(
            "打包：设了密码但缺加密组件时失败关闭",
            not result.success,
            f"success={result.success} path={result.output_path}",
        )
        check(
            "打包：失败时给出可照做的补救说明",
            "加密" in (result.error or "") or "密码" in (result.error or ""),
            f"error={result.error!r}",
        )

        # 没有密码时照常产出未加密包（不该被上面的检查误伤）
        plain = packer_mod.Packer("zip", password="")
        plain_result = plain.pack(src, "测试", tmp / "out2")
        check(
            "打包：没设密码时正常产出未加密包",
            plain_result.success and not plain_result.encrypted,
            f"success={plain_result.success} encrypted={plain_result.encrypted}",
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_channel_isolation() -> None:
    """渠道之间不得互相 import，渠道私有常量不得跑到 domain/。

    规范两条硬约束：①「渠道之间禁止互相 import，共用逻辑要往上提」；②「只服务一个
    渠道的常量写进该渠道的 constants.py」。破了第一条会让渠道纠缠成一张网，破了第二条
    会让 domain/constants.py 被各站域名淹没。
    """
    import ast
    import re

    channels_dir = PLUGIN_ROOT / "core" / "channels"
    names = sorted(
        item.name
        for item in channels_dir.iterdir()
        if item.is_dir() and (item / "__init__.py").is_file()
    )

    cross: list[str] = []
    for name in names:
        for path in (channels_dir / name).rglob("*.py"):
            if path.name == "selftest.py":
                continue
            rel = path.relative_to(PLUGIN_ROOT).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    # 相对导入的 module 只有首段（`from ..nhentai import X` → module="nhentai",
                    # level=2），所以要按「首段是否是另一个渠道名」判断
                    head = (node.module or "").split(".", 1)[0]
                    if node.level and head in names and head != name:
                        cross.append(f"{rel}:{node.lineno} from {'.' * node.level}{node.module}")
                    elif node.level == 0 and node.module and "channels." in node.module:
                        for other in names:
                            if other != name and f"channels.{other}" in node.module:
                                cross.append(f"{rel}:{node.lineno} from {node.module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        for other in names:
                            if other != name and f"channels.{other}" in alias.name:
                                cross.append(f"{rel}:{node.lineno} {alias.name}")

    check(
        "边界：渠道之间没有互相 import",
        not cross,
        "; ".join(cross[:5]),
    )

    # domain/constants.py 里不该出现只服务单个渠道的常量
    domain_src = (PLUGIN_ROOT / "domain" / "constants.py").read_text(encoding="utf-8")
    declared = re.findall(r"^([A-Z][A-Z0-9_]*)\s*[:=]", domain_src, re.M)
    channel_tokens = (
        "JMCOMIC", "JM_", "NHENTAI", "NH_", "PICACG", "PICA_",
        "HANIME", "WNACG", "SSMH", "EHENTAI", "E_HENTAI",
    )
    leaked = [name for name in declared if any(t in name for t in channel_tokens)]
    check(
        "边界：domain/constants.py 里没有渠道私有常量",
        not leaked,
        f"疑似: {leaked[:5]}",
    )
    check(
        "边界：扫到了渠道目录（守卫没空转）",
        len(names) >= 5,
        f"只扫到 {len(names)} 个渠道",
    )


def test_ranking_boards_declared() -> None:
    """渠道声明的榜单窗口必须真的能取：`ranking_boards` 与窗口映射表要对得上。

    各站支持的档位不同（Hanime1 没有月榜、哔咔没有总榜、紳士漫畫没有日榜），渠道用
    `ranking_boards` 声明自己有哪些。声明了却没有对应映射时，用户选那一档会拿到
    「没有这一档」之外的错误——本该是缺档提示，却变成内部异常。
    """
    import importlib

    from astrbot_plugin_comic_parser.core.registry import CHANNEL_CLASSES  # noqa: PLC0415

    problems: list[str] = []
    checked = 0
    for cls in CHANNEL_CLASSES:
        boards = tuple(getattr(cls, "ranking_boards", ()) or ())
        if not boards:
            continue
        checked += 1
        module = importlib.import_module(cls.__module__)
        # 各渠道按上游语义命名映射表；也可能根本不需要（如 E-Hentai 所有窗口走同一接口）
        mapping = None
        for attr in ("_BOARD_TIME", "_BOARD_WINDOW", "_BOARD_PERIOD", "_BOARD_SORT"):
            candidate = getattr(module, attr, None)
            if isinstance(candidate, dict):
                mapping = candidate
                break
        if mapping is None:
            # 没有映射表时，确认它确实靠 ranking_boards 自己兜住（而不是漏了映射）
            source = Path(module.__file__).read_text(encoding="utf-8")
            if "ranking_boards" not in source or "RankingUnsupportedError" not in source:
                problems.append(f"{cls.name}: 声明了 {boards} 但没有窗口映射也没做缺档判断")
            continue
        missing = [board for board in boards if board not in mapping]
        if missing:
            problems.append(f"{cls.name}: 声明了 {missing} 但映射表里没有")
        extra = [board for board in mapping if board not in boards]
        if extra:
            problems.append(f"{cls.name}: 映射表多出 {extra}（未在 ranking_boards 声明）")

    check(
        "榜单：扫到了声明榜单的渠道（守卫没空转）",
        checked >= 5,
        f"只扫到 {checked} 个",
    )
    check(
        "榜单：ranking_boards 与窗口映射表一致",
        not problems,
        "; ".join(problems[:5]),
    )


def test_result_rendering() -> None:
    """搜索与榜单结果行的格式：序号用「1、」、超长标题截断但仍保留 ID。

    序号写成「1.」时，长消息被平台转成图片后会被当成 Markdown 有序列表，中间插一行
    说明就重新从 1 编号（v0.8.1 修过一次）。ID 是用户回序号下载之外的第二条路，
    任何情况下都不能被截掉。
    """
    from astrbot_plugin_comic_parser.core.ranking import RankingService  # noqa: PLC0415
    from astrbot_plugin_comic_parser.core.search import SearchService  # noqa: PLC0415
    from astrbot_plugin_comic_parser.core.registry import build_channels  # noqa: PLC0415
    from astrbot_plugin_comic_parser.domain import RankingItem, SearchItem  # noqa: PLC0415

    settings = PluginSettings.from_config({})
    channels = build_channels(settings)
    search = SearchService(channels, settings)
    ranking = RankingService(channels, settings)

    item = SearchItem(
        channel="jmcomic", comic_id="123456", title="测试标题", author="作者", page_count=42
    )
    line = search._format_item(1, item, channels.get("jmcomic"), False)[0]
    check("渲染：搜索行用「1、」而非「1.」", line.startswith("1、"), line[:12])
    check("渲染：搜索行含作者与页数", "作者" in line and "42P" in line, line)
    check("渲染：搜索行含 ID", "ID：123456" in line, line)

    rline = ranking._format_item(1, RankingItem(channel="jmcomic", comic_id="999", title="榜单作品"))
    check("渲染：榜单行用「1、」", rline.startswith("1、"), rline[:12])
    check("渲染：榜单行含 ID", "ID：999" in rline, rline)

    # 超长标题：截断但不丢 ID
    long_title = "这是一个非常非常长的标题" * 8
    long_line = search._format_item(
        1, SearchItem(channel="jmcomic", comic_id="1", title=long_title),
        channels.get("jmcomic"), False,
    )[0]
    check("渲染：超长标题被截断", len(long_line) < len(long_title), f"len={len(long_line)}")
    check("渲染：超长标题仍保留 ID", "ID：1" in long_line, long_line[-20:])


def test_filename_edge_cases() -> None:
    """文件名清洗与模板：危险字符要剥掉、密码段不能被截断。

    打包基名会落成真实文件名：斜杠会建出子目录、控制字符会让部分文件系统报错、
    超长会超过 255 字节上限。更要紧的是**密码段**——标题超长时整串截断会把密码截半，
    用户照着文件名里的密码打不开自己的包。
    """
    from astrbot_plugin_comic_parser.utils import filename as fn  # noqa: PLC0415

    check("文件名：斜杠被替换", "/" not in fn.sanitize_filename("a/b"), "")
    check("文件名：反斜杠被替换", "\\" not in fn.sanitize_filename("a\\b"), "")
    check(
        "文件名：控制字符被剥掉",
        all(ord(ch) >= 32 for ch in fn.sanitize_filename("标\x00\x01题")),
        repr(fn.sanitize_filename("标\x00\x01题")),
    )
    check(
        "文件名：纯特殊字符有兜底",
        bool(fn.sanitize_filename("////\\\\")),
        repr(fn.sanitize_filename("////\\\\")),
    )
    check(
        "文件名：超长被截断",
        len(fn.sanitize_filename("很长" * 200)) <= 120,
        str(len(fn.sanitize_filename("很长" * 200))),
    )

    # 模板非法值要回退到默认，不能原样塞进文件名
    pattern = fn.resolve_template("nonsense", "")
    check("文件名：非法模板回退到默认", "{title}" in pattern, pattern)

    # 密码段：标题超长时也必须完整保留
    password = "{{PASSWORD_dp6iqt5y}}"
    long_name = fn.build_package_name(
        title="超长标题" * 40,
        comic_id="123456",
        template="id_title_pw",
        password=password,
    )
    check("文件名：超长标题下密码段完整保留", password in long_name, long_name[-30:])
    plain_name = fn.build_package_name(
        title="标题", comic_id="999", template="id_title_pw", password=""
    )
    check("文件名：没设密码时密码段整体去掉", "()" not in plain_name, plain_name)


def test_pack_formats_produce_valid_output() -> None:
    """三种打包格式都要产出**能打开**的文件，而不只是「返回成功」。

    这里用真图片跑一遍：ZIP 要能解出同样张数、PDF 页数要对、长图要按统一宽度缩放
    并纵向拼接。只断言 `success=True` 不够——产物坏掉时同样会返回成功。
    """
    import tempfile
    import zipfile

    from PIL import Image  # noqa: PLC0415

    from astrbot_plugin_comic_parser.core import packer as packer_mod  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="pack-fmt-"))
    try:
        src = tmp / "src"
        src.mkdir()
        sizes = [(400, 600), (400, 500), (400, 700)]
        for index, (width, height) in enumerate(sizes):
            Image.new("RGB", (width, height), (index * 60, 80, 120)).save(
                src / f"{index:03d}.png"
            )

        # ZIP：能解出同样张数且 CRC 校验通过
        zip_result = packer_mod.Packer("zip").pack(src, "t", tmp / "o_zip")
        check("打包：ZIP 产出成功", zip_result.success, str(zip_result.error))
        if zip_result.success and zip_result.output_path:
            with zipfile.ZipFile(zip_result.output_path) as archive:
                members = [n for n in archive.namelist() if not n.endswith("/")]
                check("打包：ZIP 里是 3 个文件", len(members) == 3, str(members))
                check("打包：ZIP 内容完整（CRC 通过）", archive.testzip() is None, "")

        # PDF：页数等于图片数
        pdf_result = packer_mod.Packer("pdf").pack(src, "t", tmp / "o_pdf")
        check("打包：PDF 产出成功", pdf_result.success, str(pdf_result.error))
        if pdf_result.success and pdf_result.output_path:
            import fitz  # noqa: PLC0415

            with fitz.open(str(pdf_result.output_path)) as doc:
                check("打包：PDF 页数等于图片数", doc.page_count == 3, str(doc.page_count))

        # 长图：按统一宽度缩放后纵向拼接
        long_result = packer_mod.Packer("long_img").pack(src, "t", tmp / "o_long")
        check("打包：长图产出成功", long_result.success, str(long_result.error))
        if long_result.success and long_result.output_path:
            with Image.open(long_result.output_path) as image:
                width, height = image.size
            expected_width = packer_mod.LONG_IMG_WIDTH
            check(
                "打包：长图宽度统一为 LONG_IMG_WIDTH",
                width == expected_width,
                f"{width} vs {expected_width}",
            )
            check("打包：长图高度大于单张", height > max(h for _, h in sizes), str(height))
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_cover_picking() -> None:
    """封面挑选：优先够大的图、都是小图时退回第一张、空目录返回 None。

    站徽、占位图、章节目录缩略图当封面毫无意义，所以要按边长过一遍；但也不能因为
    全是小图就不发封面——退回第一张至少能让用户看到内容。章节分子目录时也要能找到。
    """
    import tempfile

    from PIL import Image  # noqa: PLC0415

    from astrbot_plugin_comic_parser.core import cover as cover_mod  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="cover-guard-"))
    try:
        src = tmp / "src"
        src.mkdir()
        for name, size in (("000.png", (80, 80)), ("001.png", (800, 1200))):
            Image.new("RGB", size, (100, 150, 200)).save(src / name)
        service = cover_mod.CoverService(None, tmp)
        picked = service.pick_cover(src)
        check(
            "封面：跳过小图选够大的那张",
            picked is not None and picked.name == "001.png",
            str(picked),
        )

        small_only = tmp / "small"
        small_only.mkdir()
        Image.new("RGB", (80, 80), (1, 2, 3)).save(small_only / "a.png")
        fallback = service.pick_cover(small_only)
        check(
            "封面：全是小图时退回第一张",
            fallback is not None and fallback.name == "a.png",
            str(fallback),
        )

        empty = tmp / "empty"
        empty.mkdir()
        check("封面：空目录返回 None", service.pick_cover(empty) is None, "")

        # 章节型产物在子目录里，封面也得能找到
        nested = tmp / "nested" / "ch01"
        nested.mkdir(parents=True)
        Image.new("RGB", (900, 900), (50, 60, 70)).save(nested / "x.png")
        found = service.pick_cover(tmp / "nested")
        check("封面：能找到章节目录里的图", found is not None and found.name == "x.png", str(found))
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_stats_counting_and_persistence() -> None:
    """统计口径：暂停不算失败、累计跨重载保留、本次运行增量归零。

    「暂停」是用户主动停或到点停，不是失败——混进失败数会让用户以为站点坏了。
    累计落盘是为了插件重载（改配置就会触发）后数字不归零；本次运行增量则必须从
    这次加载算起，否则「本次运行」失去意义。
    """
    import tempfile

    from astrbot_plugin_comic_parser.core import stats as stats_mod  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="stats-guard-"))
    try:
        path = tmp / "stats.json"
        stats = stats_mod.DownloadStats(path)
        stats.record("jmcomic", stats_mod.OUTCOME_OK, images=42, size_bytes=1048576, elapsed=12.5)
        stats.record("jmcomic", stats_mod.OUTCOME_FAIL)
        stats.record("nhentai", stats_mod.OUTCOME_PAUSED, images=5)

        snap = stats.snapshot()
        check("统计：暂停不计入失败", snap.fail == 1, f"fail={snap.fail}")
        check("统计：暂停单独计数", snap.paused == 1, f"paused={snap.paused}")
        check("统计：成功单独计数", snap.ok == 1, f"ok={snap.ok}")
        check(
            "统计：按渠道分列",
            snap.channels.get("jmcomic") is not None
            and snap.channels["jmcomic"].ok == 1
            and snap.channels["jmcomic"].fail == 1,
            str({k: (v.ok, v.fail) for k, v in snap.channels.items()}),
        )

        # 未知的 outcome 要按失败记，不能悄悄算成成功
        unknown = stats_mod.DownloadStats(tmp / "unknown.json")
        unknown.record("jmcomic", "nonsense-outcome")
        unknown_snap = unknown.snapshot()
        check(
            "统计：未知结果按失败记",
            unknown_snap.fail == 1 and unknown_snap.ok == 0,
            f"ok={unknown_snap.ok} fail={unknown_snap.fail}",
        )

        # 重载：累计要保留、本次运行要归零
        reloaded = stats_mod.DownloadStats(path).snapshot()
        check("统计：累计跨重载保留", reloaded.total == 3, f"total={reloaded.total}")
        check(
            "统计：重载后本次运行归零",
            reloaded.session.get("total", 0) == 0,
            str(reloaded.session),
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_domain_exports_complete() -> None:
    """`domain` 包里定义的公开类都要从包入口导出。

    上层模块习惯写 `from ...domain import X`。某个类只定义在子模块、没进
    `domain/__init__.py` 的 `__all__` 时，这种写法会 ImportError——而它是否被发现
    取决于下一个写代码的人恰好用了哪种 import 路径。
    """
    import re

    init_src = (PLUGIN_ROOT / "domain" / "__init__.py").read_text(encoding="utf-8")
    if "__all__" not in init_src:
        check("导出：domain 有 __all__", False, "缺 __all__")
        return
    exported = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', init_src.split("__all__", 1)[1]))

    defined: set[str] = set()
    for module in ("models.py", "config.py", "chapters.py", "errors.py", "constants.py"):
        path = PLUGIN_ROOT / "domain" / module
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        defined |= set(re.findall(r"^(?:class|def)\s+([A-Z][A-Za-z0-9_]*)", source, re.M))

    missing = sorted(defined - exported)
    check(
        "导出：domain 的公开类都进了 __all__",
        not missing,
        f"未导出: {missing[:6]}",
    )
    check(
        "导出：扫到了 domain 的类（守卫没空转）",
        len(defined) >= 10,
        f"只扫到 {len(defined)} 个",
    )

    # 真能 import 出来，而不只是名字写在 __all__ 里
    import importlib

    not_importable: list[str] = []
    for pkg_name in ("domain", "core", "utils"):
        package = importlib.import_module(f"astrbot_plugin_comic_parser.{pkg_name}")
        declared = getattr(package, "__all__", ()) or ()
        not_importable.extend(
            f"{pkg_name}.{name}" for name in declared if not hasattr(package, name)
        )
    check(
        "导出：三个包的 __all__ 名字都能 import",
        not not_importable,
        f"import 不到: {not_importable[:6]}",
    )

    # core 下被其他模块从子模块导入的公开类，也应该能从包入口拿到
    core_init = (PLUGIN_ROOT / "core" / "__init__.py").read_text(encoding="utf-8")
    core_exported = set(
        re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', core_init.split("__all__", 1)[1])
    )
    core_defined: set[str] = set()
    for path in (PLUGIN_ROOT / "core").glob("*.py"):
        if path.name == "__init__.py":
            continue
        core_defined |= set(
            re.findall(r"^class\s+([A-Z][A-Za-z0-9_]*)", path.read_text(encoding="utf-8"), re.M)
        )
    # 只要求「被别的模块从子模块导入过」的那些必须可从包入口拿
    used_from_submodule: set[str] = set()
    for path in list((PLUGIN_ROOT / "core").rglob("*.py")) + [PLUGIN_ROOT / "main.py"]:
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        for name in core_defined:
            if re.search(rf"from \.[a-z_.]+ import [^\n]*\b{name}\b", source):
                used_from_submodule.add(name)
    missing_core = sorted(used_from_submodule - core_exported)
    check(
        "导出：core 里被跨模块引用的类都能从包入口导入",
        not missing_core,
        f"未导出: {missing_core[:6]}",
    )


def test_config_type_matches_parser() -> None:
    """schema 里声明的类型，要和 config.py 里实际用的解析函数对得上。

    声明 `int` 却用 `_as_str` 读，用户填 30 会拿到字符串 "30"，后面做数值比较时
    要么 TypeError 要么静默走错分支。用 AST 精确取「赋值右侧调用的函数名」，
    不用正则——正则会被相邻行与 `min(...)` 这类包裹误匹配。
    """
    import ast

    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    config_tree = ast.parse((PLUGIN_ROOT / "domain" / "config.py").read_text(encoding="utf-8"))

    # 字段名 → 它被赋值时调用的解析函数
    parser_of: dict[str, str] = {}
    for node in ast.walk(config_tree):
        if not isinstance(node, ast.keyword) or node.arg is None:
            continue
        value = node.value
        # 可能是 `_as_int(...)`，也可能是 `min(_as_int(...), N)`
        while isinstance(value, ast.Call):
            func = value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name.startswith("_as_"):
                parser_of[node.arg] = name
                break
            # 继续往里剥一层（min/max 包裹）
            if name in ("min", "max") and value.args:
                value = value.args[0]
                continue
            break

    expect = {
        "int": "_as_int",
        "float": "_as_float",
        "bool": "_as_bool",
        "string": "_as_str",
        "text": "_as_str",
        "list": "_as_str_list",
    }

    def leaves(node: dict, prefix: str = "") -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if value.get("type") == "object" and "items" in value:
                out.extend(leaves(value["items"], path))
            elif value.get("type") == "template_list":
                continue
            else:
                out.append((path, key, value.get("type", "")))
        return out

    items: list[tuple[str, str, str]] = []
    for group, node in schema.items():
        if group == "channels":
            continue
        if node.get("type") == "object":
            items.extend(leaves(node["items"], group))
        else:
            items.append((group, group, node.get("type", "")))

    # 字段名在 PluginSettings 里的样子（前缀化），这里按「key 是字段名后缀」匹配
    mismatches: list[str] = []
    matched = 0
    for path, key, declared in items:
        want = expect.get(declared)
        if want is None:
            continue
        # 精确匹配优先；没有再退到「组名前缀 + 字段名」，且必须唯一命中。
        # 不能简单按后缀匹配：`use_proxy` 与 `proxy` 是两回事，会串。
        candidates = [(field, parser) for field, parser in parser_of.items() if field == key]
        if not candidates:
            suffix = [f for f in parser_of if f.endswith(f"_{key}")]
            if len(suffix) == 1:
                candidates = [(suffix[0], parser_of[suffix[0]])]
        if not candidates:
            continue
        matched += 1
        for field, parser in candidates:
            if parser != want:
                mismatches.append(f"{path}: 声明 {declared} 却用 {parser}")

    # 匹配数取决于「schema 的 key 与 config.py 的字段名能否对上」：
    # 有些字段带组名前缀（packing.pack_password → pack_password）、有些被 min/max
    # 包裹后又拆了一层，能自动对上的约 38 项。阈值按实际值定，只用来防「扫到 0 项」。
    check(
        "配置：扫到了足够多的类型声明（守卫没空转）",
        matched >= 30,
        f"只匹配到 {matched} 项",
    )
    check(
        "配置：schema 类型与解析函数一致",
        not mismatches,
        "; ".join(mismatches[:5]),
    )


def test_janitor_and_health_runtime() -> None:
    """清理与自检的运行时行为：只删过期的、活跃目录一个都不碰、自检按配置判故障。

    这两条维护链路的自测此前只有 `core/selftest.py` 里那批针对「构造」的检查；
    这里补的是「真建目录真跑一遍」的行为验证——清理删错目录是用户资产的直接损失。
    """
    import os
    import tempfile
    import time

    from astrbot_plugin_comic_parser.core import health as health_mod  # noqa: PLC0415
    from astrbot_plugin_comic_parser.core import janitor  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="janitor-guard-"))
    try:
        resume = tmp / "resume"
        resume.mkdir()
        stale = resume / "jm-old"
        stale.mkdir()
        (stale / "a.jpg").write_bytes(b"x" * 100)
        ancient = time.time() - 99999
        for target in [stale, *stale.rglob("*")]:
            os.utime(target, (ancient, ancient))
        fresh = resume / "jm-fresh"
        fresh.mkdir()
        (fresh / "b.jpg").write_bytes(b"y" * 100)

        result = janitor.cleanup_temp(tmp, resume_ttl=3600)
        check("清理：过期断点被删", not stale.exists(), str(result.removed_dirs))
        check("清理：新鲜断点被保留", fresh.exists(), "")
        check("清理：统计了释放的字节", result.freed_bytes > 0, str(result.freed_bytes))

        # ttl=0 表示不留断点，但活跃目录仍然不能碰
        keep = janitor.cleanup_temp(tmp, resume_ttl=0, active_dirs={fresh})
        check("清理：活跃目录即使 ttl=0 也不删", fresh.exists(), str(keep.skipped_active))

        # 自检：设了密码缺加密组件要报 error，没设密码则不算问题
        with_password = PluginSettings.from_config({"packing": {"pack_password": "x"}})
        report = health_mod.check(with_password, {}, probe=lambda name: name != "pyzipper")
        check(
            "自检：设了密码缺 pyzipper 报 error",
            not report.ok and any("pyzipper" in item.title for item in report.issues),
            str([i.title for i in report.issues]),
        )
        without = PluginSettings.from_config({})
        quiet = health_mod.check(without, {}, probe=lambda name: name != "pyzipper")
        check("自检：没设密码缺 pyzipper 不算问题", quiet.ok, str([i.title for i in quiet.issues]))
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_logging_rules() -> None:
    """日志规范守卫：插件代码不得引入 logging/loguru，且 logger 调用必须带插件前缀。

    用 AST 而不是 grep：grep 会被多行调用、字符串里的同名文本误判，也会漏掉
    `import logging as x` 这类写法。自测脚本本身在装 astrbot 桩时允许 import logging
    （那是测试环境的替身，不是插件运行路径），所以这里只扫 core/domain/utils/main.py
    且跳过 *_selftest.py 与 selftest.py。
    """
    import ast

    root = PLUGIN_ROOT
    targets = [root / "main.py"]
    for sub in ("core", "domain", "utils"):
        targets.extend((root / sub).rglob("*.py"))

    forbidden: list[str] = []
    no_prefix: list[str] = []
    for path in targets:
        rel = path.relative_to(root).as_posix()
        if rel.endswith("selftest.py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            forbidden.append(f"{rel}: 语法错误 {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in ("logging", "loguru"):
                        forbidden.append(f"{rel}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".")[0]
                if mod in ("logging", "loguru"):
                    forbidden.append(f"{rel}:{node.lineno} from {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "logger"
                ):
                    continue
                if not node.args:
                    continue
                rendered = ast.unparse(node.args[0])
                if "PLUGIN_NAME" not in rendered:
                    no_prefix.append(f"{rel}:{node.lineno} {rendered[:60]}")

    check("日志：插件代码没有 import logging/loguru", not forbidden, "; ".join(forbidden[:5]))
    check(
        "日志：logger 调用都带 PLUGIN_NAME 前缀",
        not no_prefix,
        "; ".join(no_prefix[:5]),
    )

    # `# type: ignore` 是类型作弊：规范要求无理由不许用。用 tokenize 只扫真注释，
    # 否则 docstring 里讲这件事的句子会被误判成违规。
    import io
    import tokenize

    type_ignores: list[str] = []
    for path in targets:
        rel = path.relative_to(root).as_posix()
        if rel.endswith("selftest.py"):
            continue
        try:
            tokens = tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline)
            for tok in tokens:
                if tok.type == tokenize.COMMENT and "type: ignore" in tok.string:
                    type_ignores.append(f"{rel}:{tok.start[0]}")
        except (tokenize.TokenError, SyntaxError) as exc:
            type_ignores.append(f"{rel}: 无法解析 {exc}")
    check(
        "类型：插件代码没有 # type: ignore",
        not type_ignores,
        "; ".join(type_ignores[:5]),
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
    test_channel_wiring()
    test_channel_boundaries()
    test_config_sync()
    test_changelog_folding()
    test_silent_excepts()
    test_config_defaults()
    test_declared_dependencies()
    test_dependency_import_style()
    test_settings_fields_used()
    test_view_endpoints()
    test_no_secret_logging()
    test_readme_config_names()
    test_config_dirty_values()
    test_changelog_wording()
    test_exception_specificity()
    test_error_messages_actionable()
    test_help_text_config_names()
    test_metadata_consistency()
    test_view_i18n_keys()
    test_command_annotations()
    test_heavy_work_offloaded()
    test_schema_integrity()
    test_cache_bounded()
    test_readme_chapter_examples()
    test_resume_cleanup()
    test_pack_fails_closed()
    test_channel_isolation()
    test_ranking_boards_declared()
    test_result_rendering()
    test_filename_edge_cases()
    test_pack_formats_produce_valid_output()
    test_cover_picking()
    test_stats_counting_and_persistence()
    test_domain_exports_complete()
    test_config_type_matches_parser()
    test_janitor_and_health_runtime()
    test_logging_rules()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print()
    for name, ok, detail in RESULTS:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}" + (f" —— {detail}" if detail and not ok else ""))
    print(f"\n{passed}/{len(RESULTS)} 项通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

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
from astrbot_plugin_comic_parser.core import panel as panel_mod  # noqa: E402
from astrbot_plugin_comic_parser.core.stats import (  # noqa: E402
    OUTCOME_FAIL,
    OUTCOME_OK,
    OUTCOME_PAUSED,
    DownloadStats,
)
from astrbot_plugin_comic_parser.domain.constants import (  # noqa: E402
    STATS_VERSION,
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



def main() -> int:
    test_health()
    test_stats()
    test_stats_persistence()
    test_panel_stats_reset()
    test_janitor()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print()
    for name, ok, detail in RESULTS:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}" + (f" —— {detail}" if detail and not ok else ""))
    print(f"\n{passed}/{len(RESULTS)} 项通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

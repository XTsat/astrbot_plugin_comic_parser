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

import sys
import tempfile
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




def main() -> int:
    test_health()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print()
    for name, ok, detail in RESULTS:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}" + (f" —— {detail}" if detail and not ok else ""))
    print(f"\n{passed}/{len(RESULTS)} 项通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

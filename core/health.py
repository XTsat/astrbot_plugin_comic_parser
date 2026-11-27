"""环境自检：只读本地状态，不联网、不打上游站点。

下载链路上有几处「依赖缺失 → 失败关闭」：设了密码却没有 ``pyzipper``、选了 PDF 却没有
``pymupdf``、长图没有 ``Pillow``、启用了 JM 却没有 ``jmcomic``。这些都要等到**整本下完、
打包那一刻**才暴露，用户白等一场。这里把它们提前成一次毫秒级检查，让管理员在出问题之前
就能看见。

只回答「这台机器现在能不能按当前配置跑」，不回答「站点通不通」——后者是面板「渠道测试」
与 ``scripts/*_probe.py`` 的职责，需要真打上游。
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

# 检测依赖是否就位：用 find_spec 而不是 import，省掉几十 MB 的加载开销，
# 也避免某个包装上了却导入失败时把自检本身拖崩
Probe = Callable[[str], bool]

LEVEL_ERROR: Final = "error"
LEVEL_WARN: Final = "warn"


def default_probe(module: str) -> bool:
    """默认探测面：模块能否被找到。"""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class Dependency:
    """一个运行时依赖：import 名与 pip 包名不一定相同（fitz ↔ pymupdf）。"""

    module: str
    package: str
    purpose: str


# 与 requirements.txt 对齐。onnxruntime 单列：它不随插件安装，缺了只影响打码
DEPENDENCIES: Final[tuple[Dependency, ...]] = (
    Dependency("jmcomic", "jmcomic", "禁漫天堂渠道"),
    Dependency("pyzipper", "pyzipper", "加密 ZIP（设了打包密码就要）"),
    Dependency("fitz", "pymupdf", "PDF 打包"),
    Dependency("PIL", "Pillow", "长图拼接与封面打码"),
    Dependency("cloudscraper", "cloudscraper", "紳士漫畫 / nhentai 的 Cloudflare 兜底"),
    Dependency("httpx", "httpx", "哔咔渠道与搜图（AstrBot 一般自带）"),
)
CENSOR_DEPENDENCY: Final = Dependency(
    "onnxruntime", "onnxruntime", "封面打码与榜单图片版"
)


@dataclass(slots=True)
class HealthIssue:
    """一条自检结论：是什么问题、会有什么后果、怎么补。"""

    level: str
    title: str
    impact: str
    fix: str

    def render(self) -> str:
        mark = "❌" if self.level == LEVEL_ERROR else "⚠️"
        return f"{mark} {self.title} —— {self.impact}\n   补：{self.fix}"


@dataclass(slots=True)
class HealthReport:
    """一次自检的完整结果。"""

    issues: list[HealthIssue] = field(default_factory=list)
    # 检查过、且确实在位的依赖名：让「全部通过」也有据可查，而不是空口一句没问题
    present: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[HealthIssue]:
        return [item for item in self.issues if item.level == LEVEL_ERROR]

    @property
    def warnings(self) -> list[HealthIssue]:
        return [item for item in self.issues if item.level == LEVEL_WARN]

    @property
    def ok(self) -> bool:
        """没有 error 级问题就算可用：warn 只表示某个可选能力降级。"""
        return not self.errors

    def render(self) -> str:
        """聊天里发的正文：结论在前，逐条问题在后。"""
        if not self.issues:
            return (
                f"✅ 环境自检通过：{len(self.present)} 项依赖齐全，"
                "当前配置没有冲突"
            )
        errors = len(self.errors)
        warns = len(self.warnings)
        if errors:
            head = f"🩺 环境自检：{errors} 个问题会让下载白费"
            if warns:
                head += f"，另有 {warns} 项降级"
        else:
            head = f"🩺 环境自检：能跑，但有 {warns} 项降级"
        lines = [head]
        # error 排在前面：它们会让整次下载作废，比「某个可选能力降级」要紧
        for issue in self.errors + self.warnings:
            lines.append(issue.render())
        if self.present:
            lines.append(f"已在位：{'、'.join(self.present)}")
        lines.append("提示：这只是本机检查，站点通不通请看面板「状态 → 渠道测试」")
        return "\n".join(lines)

    def snapshot(self) -> dict[str, Any]:
        """面板接口用的结构：页面据 level 决定徽章颜色。"""
        return {
            "ok": self.ok,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "present": list(self.present),
            "issues": [
                {
                    "level": item.level,
                    "title": item.title,
                    "impact": item.impact,
                    "fix": item.fix,
                }
                for item in self.issues
            ],
        }


def _needs_censor(settings: Any) -> bool:
    """打码运行时会不会被用到：与 `main._needs_censor_runtime` 同一套判断。"""
    if not getattr(settings, "cover_censor", False):
        return False
    if getattr(settings, "cover_send", False):
        return True
    return bool(
        getattr(settings, "ranking_enabled", False)
        and getattr(settings, "ranking_output", "text") in ("image", "both")
    )


def _pack_format(settings: Any) -> str:
    return str(getattr(settings, "pack_format", "") or "")


def check(
    settings: Any,
    channels: Mapping[str, Any] | Iterable[Any] = (),
    *,
    probe: Probe = default_probe,
) -> HealthReport:
    """按当前配置检查依赖与配置冲突。

    Args:
        settings: ``PluginSettings``；只读属性，缺字段按默认值处理（便于单测传简单对象）。
        channels: 渠道映射或渠道对象序列；用于发现「渠道启用了但依赖没装」。
        probe: 依赖探测函数，默认 ``importlib.util.find_spec``；单测注入假实现。

    Returns:
        HealthReport: 问题清单与已就位依赖。
    """
    report = HealthReport()
    found: dict[str, bool] = {}
    for dep in DEPENDENCIES + (CENSOR_DEPENDENCY,):
        found[dep.module] = bool(probe(dep.module))

    for dep in DEPENDENCIES:
        if found[dep.module]:
            report.present.append(dep.package)
            continue
        # 缺了不一定出错：只有当前配置真的会走到它时才算问题。
        # 程度分两档——打包链路上的缺失会让整次下载作废（error），
        # 只影响兜底路径的缺失只是降级（warn）
        judged = _requirement_reason(dep, settings)
        if judged is None:
            continue
        level, impact = judged
        report.issues.append(
            HealthIssue(
                level=level,
                title=f"缺少 {dep.package}（{dep.purpose}）",
                impact=impact,
                fix=f"pip install {dep.package}，装好后重载插件",
            )
        )

    if _needs_censor(settings):
        if found[CENSOR_DEPENDENCY.module]:
            report.present.append(CENSOR_DEPENDENCY.package)
        elif not getattr(settings, "nsfw_auto_download", False):
            report.issues.append(
                HealthIssue(
                    level=LEVEL_WARN,
                    title="缺少 onnxruntime（封面打码）",
                    impact="打码跑不起来，封面与榜单图会退回不发 / 整张糊掉",
                    fix="打开「缺失时自动下载权重」让它自己补装，或 pip install onnxruntime",
                )
            )

    report.issues.extend(_channel_issues(channels))
    report.issues.extend(_blacklist_issues(settings, channels))
    return report


def _requirement_reason(
    dep: Dependency, settings: Any
) -> tuple[str, str] | None:
    """当前配置会在哪里踩到这个缺失依赖。

    Returns:
        ``(level, impact)``；不会踩到时返回 None。level 取 ``LEVEL_ERROR``（这次下载会作废）
        或 ``LEVEL_WARN``（只降级，主路径仍可用）。
    """
    fmt = _pack_format(settings)
    if dep.module == "pyzipper":
        if getattr(settings, "pack_password", ""):
            return (
                LEVEL_ERROR,
                "已设置打包密码，但缺加密组件：每次下载都会在打包那一刻失败",
            )
        return None
    if dep.module == "fitz":
        if fmt == "pdf":
            return (LEVEL_ERROR, "打包格式选的是 PDF，缺 pymupdf：每次下载都打不出文件")
        return None
    if dep.module == "PIL":
        # Pillow 在多个模块的顶层导入（封面、面板、榜单卡片、搜图），缺了插件根本加载不起来，
        # 所以不能像以前那样「只有选了长图或开了打码才算问题」——那会把致命问题说成没事
        return (
            LEVEL_ERROR,
            "缺少 Pillow：封面、面板、榜单卡片与搜图都依赖它，插件可能加载不起来",
        )
    if dep.module == "cloudscraper":
        # 只是 Cloudflare 兜底路径：主路径（官方 API / 直连）不依赖它，
        # 所以永远不算 error——但也不能像以前那样直接不提，否则用户永远查不到
        # 「为什么紳士漫畫偶发 403」的原因
        return (
            LEVEL_WARN,
            "紳士漫畫 / nhentai 被 Cloudflare 拦下时没有兜底可走，"
            "这两个渠道遇到风控就只能报错",
        )
    if dep.module == "httpx":
        # 哔咔渠道与搜图都只在真要用时才 import；两个功能都关着就不算问题
        enabled = getattr(settings, "enabled_channel_names", ()) or ()
        if "picacg" in enabled or getattr(settings, "image_search_enabled", False):
            return (
                LEVEL_ERROR,
                "哔咔渠道或搜图已启用，缺 httpx：这两处一调用就报缺依赖",
            )
        return None
    if dep.module == "jmcomic":
        # 渠道级的缺失由 _channel_issues 按「是否启用」判定，这里不重复报
        return None
    return None


def _blacklist_issues(
    settings: Any, channels: Mapping[str, Any] | Iterable[Any]
) -> list[HealthIssue]:
    """作品黑名单写坏了没有：这类条目不会报错，只会静默不生效。

    两种写法看起来都对、实际什么也不拦：前缀写成 `jmcomic1:123`（渠道名拼错）、
    或者把作用范围全部取消勾选。前者用户在命令里根本看不出来，后者只是「黑名单关着」。
    """
    entries = list(getattr(settings, "id_blacklist", []) or [])
    if not entries:
        return []
    from .id_blacklist import IdBlacklist  # 局部导入：health 与它互不依赖启动顺序

    blacklist = IdBlacklist.from_settings(
        settings, channels if isinstance(channels, Mapping) else {}
    )
    issues: list[HealthIssue] = []
    if not blacklist.actions:
        issues.append(
            HealthIssue(
                level=LEVEL_WARN,
                title="作品 ID 黑名单没有作用范围",
                impact="「黑名单作用范围」一项都没勾，黑名单里填的作品不会被拦",
                fix="在插件配置里把要拦的项勾上（下载 / 简介 / 封面）",
            )
        )
    if blacklist.unresolved:
        broken = "、".join(blacklist.unresolved[:3])
        more = "等" if len(blacklist.unresolved) > 3 else ""
        issues.append(
            HealthIssue(
                level=LEVEL_WARN,
                title=f"作品 ID 黑名单有 {len(blacklist.unresolved)} 条认不出渠道",
                impact=f"这些条目可能拦不住作品：{broken}{more}",
                fix="按「渠道:ID」或「作品链接」重填，渠道名可用 jm / nh / pica / hm / ssmh / eh",
            )
        )
    return issues


def _iter_channels(channels: Mapping[str, Any] | Iterable[Any]) -> list[Any]:
    if isinstance(channels, Mapping):
        return list(channels.values())
    try:
        return list(channels)
    except TypeError:
        return []


def _channel_issues(channels: Mapping[str, Any] | Iterable[Any]) -> list[HealthIssue]:
    """启用了却缺依赖的渠道：用户敲命令才会发现「这个渠道用不了」，提前说出来。"""
    issues: list[HealthIssue] = []
    for channel in _iter_channels(channels):
        if not getattr(channel, "enabled", False):
            continue
        missing = getattr(channel, "missing_requirement", None)
        if not callable(missing):
            continue
        try:
            module = missing()
        except Exception:  # noqa: BLE001 - 自检本身绝不能因为渠道实现异常而失败
            continue
        if not module:
            continue
        display = getattr(channel, "display_name", None) or getattr(
            channel, "name", "某渠道"
        )
        issues.append(
            HealthIssue(
                level=LEVEL_ERROR,
                title=f"{display} 渠道缺少 {module}",
                impact="这个渠道的命令会被拒绝，其它渠道不受影响",
                fix=f"pip install {module}，或在配置里关掉该渠道",
            )
        )
    return issues

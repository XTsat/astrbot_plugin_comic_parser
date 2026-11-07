"""打码运行时的按需安装。

`onnxruntime` 装完约 100MB，而不用打码的人完全用不到它，所以它不进
`requirements.txt`：插件每次加载（含重载）都看一眼依赖在不在，确认要用打码又缺依赖时
才在后台装一次，其余情况下插件零额外体积。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys

from astrbot.api import logger

from ...domain.constants import PLUGIN_NAME

# 和代码里用到的 API 对齐：1.17 起 InferenceSession 的接口稳定
ONNXRUNTIME_PACKAGE = "onnxruntime>=1.17.0"
# 慢网下几十 MB 的 wheel 也可能拉很久，但不能无限期挂着后台任务
INSTALL_TIMEOUT = 900
# 只允许一个安装流程：重载很快时上一轮的任务可能还没退出
_INSTALL_LOCK = asyncio.Lock()


def onnxruntime_installed() -> bool:
    """依赖是否就位。用 find_spec 而不是 import，省掉几十 MB 的加载开销。"""
    try:
        return importlib.util.find_spec("onnxruntime") is not None
    except (ImportError, ValueError):
        return False


async def ensure_onnxruntime() -> bool:
    """确保 onnxruntime 可用，缺了就现场装。

    Returns:
        bool: 能否继续做检测。装失败只记日志并返回 False——上层据此按「不发封面」
        处理，下载与打包不受影响。
    """
    if onnxruntime_installed():
        return True

    async with _INSTALL_LOCK:
        # 排队期间可能已经被另一轮装好了
        if onnxruntime_installed():
            return True

        logger.info(
            f"[{PLUGIN_NAME}] 已开启封面打码但缺少 onnxruntime，"
            f"后台安装 {ONNXRUNTIME_PACKAGE}（约 100MB）…"
        )
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-input",
            "--disable-pip-version-check",
            ONNXRUNTIME_PACKAGE,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except (OSError, ValueError) as exc:
            # 镜像里没有 pip、或解释器路径不可执行：只能让用户手动装
            logger.warning(
                f"[{PLUGIN_NAME}] ⚠️ 无法执行 pip 安装 onnxruntime（{exc}）；"
                "请手动 pip install onnxruntime 后重载插件"
            )
            return False

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=INSTALL_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(
                f"[{PLUGIN_NAME}] ⚠️ 安装 onnxruntime 超过 {INSTALL_TIMEOUT} 秒未完成，已中止；"
                "请手动 pip install onnxruntime 后重载插件"
            )
            return False

        output = (stdout or b"").decode("utf-8", "replace")
        if proc.returncode != 0:
            logger.warning(
                f"[{PLUGIN_NAME}] ⚠️ onnxruntime 安装失败（返回码 {proc.returncode}）："
                f"{output[-600:] or '无输出'}；可手动 pip install onnxruntime 后重载插件"
            )
            return False

        # 新装的包要清掉 import 缓存，否则本进程里 find_spec 还看不到
        importlib.invalidate_caches()
        if not onnxruntime_installed():
            logger.warning(
                f"[{PLUGIN_NAME}] ⚠️ pip 报告安装成功，但本进程仍找不到 onnxruntime；"
                "建议重载插件后再试"
            )
            return False

        logger.info(f"[{PLUGIN_NAME}] onnxruntime 安装完成，封面打码可以用了")
        return True


__all__ = ["ensure_onnxruntime", "onnxruntime_installed"]

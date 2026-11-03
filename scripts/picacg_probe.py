#!/usr/bin/env python3
"""哔咔下载链路实测脚本（在能访问哔咔的机器上手动跑）。

只做一件事：用真实账号把「登录 → 取元信息 → 取章节 → 下载整本」这条链路跑一遍，
把每一步的结果打出来，用来判断这台机器到底能不能从哔咔把图片下下来。

逻辑本身复用渠道自测里的真站探针（`core/channels/picacg/selftest.py --live`），这里只负责
把命令行参数翻译成环境变量，避免同一段探针代码写两份。

用法：
    python scripts/picacg_probe.py 5f1e2d3c4b5a69788796a5b4 \\
        --account 邮箱 --password 密码 [--proxy http://127.0.0.1:7890]

不带参数时从 PICA_ACCOUNT / PICA_PASSWORD / PICA_COMIC_ID / PICA_PROXY 读。

退出码：0 = 至少下到一张图；1 = 链路某一步失败；2 = 参数不全。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SELFTEST = PLUGIN_ROOT / "core" / "channels" / "picacg" / "selftest.py"


def main() -> int:
    parser = argparse.ArgumentParser(description="哔咔下载链路实测")
    parser.add_argument("comic_id", nargs="?", default="", help="哔咔漫画 ID（24 位）")
    parser.add_argument("--account", default="", help="哔咔账号邮箱")
    parser.add_argument("--password", default="", help="哔咔密码")
    parser.add_argument("--proxy", default="", help="代理地址，例如 http://127.0.0.1:7890")
    args = parser.parse_args()

    for key, value in (
        ("PICA_COMIC_ID", args.comic_id),
        ("PICA_ACCOUNT", args.account),
        ("PICA_PASSWORD", args.password),
        ("PICA_PROXY", args.proxy),
    ):
        if value:
            os.environ[key] = value

    spec = importlib.util.spec_from_file_location("picacg_selftest", SELFTEST)
    if spec is None or spec.loader is None:  # 文件被挪走时给出可读的提示
        print(f"找不到渠道自测脚本：{SELFTEST}")
        return 2
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return int(module.asyncio.run(module._run_live()))


if __name__ == "__main__":
    sys.exit(main())

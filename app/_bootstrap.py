# -*- coding: utf-8 -*-
"""_bootstrap.py —— 让源码运行也能找到仓库里自带的第三方库。

背景（真实故障）
----------------
仓库里带着两份本地依赖目录（都不进 git，但打包时会被收进产物）：

    .pylibs/     Pillow / numpy / mss / pygame / pystray / winotify …
    .devtools/   rapidfuzz 等开发/测试用库

以前它们**只靠环境变量** `PYTHONPATH` 生效 —— 开发终端里设了就跑得起来，
直接敲 `python main.py`（或双击、快捷方式、换了终端）就找不到。表现是：

    [WARNING] 依赖 Pillow 缺失

而依赖缺失的后果是**整个功能静默失效**（提示浮层画不出来、匹配退化成
很慢的 difflib），界面上却没有任何指向真正原因的提示（用户实测反馈）。

所以入口脚本先在 `sys.path` 前面补上这两个目录：找得到就用，找不到就跳过
（系统 Python 里装好了依赖的情况完全不受影响 —— 只是多两个候选路径）。
"""
from __future__ import annotations

import os
import sys

#: 仓库自带的依赖目录（顺序 = 优先级）
VENDORED_DIRS = (".pylibs", ".devtools")


def add_vendored_libs(root: str) -> list:
    """把 `<root>/.pylibs`、`<root>/.devtools` 插到 `sys.path` 最前面。

    返回真正加进去的目录列表（找不到的不算）。
    """
    added = []
    for name in VENDORED_DIRS:
        path = os.path.join(root, name)
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
            added.append(path)
    return added


def vendored_libs_missing(root: str) -> bool:
    """两个目录都不在（打包产物 / 系统 Python 环境）时为 True。"""
    return not any(os.path.isdir(os.path.join(root, n)) for n in VENDORED_DIRS)

# -*- coding: utf-8 -*-
"""main.py —— 入口脚本（薄壳）。

真正的应用在 `app/application.py`。这里只做三件事：
把仓库根放进 `sys.path`、补上仓库自带的依赖目录（`.pylibs` / `.devtools`），
然后把控制权交给 `app.application.main()`。

用法：
    python main.py                      正常启动 GUI
    python main.py --check              环境自检（逐项 OK/FAIL）
    python main.py --watch-debug        前台实时打印插件日志的每一行
    python main.py --replay 日志文件     离线回放一份日志（不写库，只看结论）
    python main.py --preview-notification   离线渲染一张通知预览图
    python main.py --debug              打开 DEBUG 日志

**不需要**先设 PYTHONPATH：仓库根与自带的依赖目录都会在这里补上
（见 app/_bootstrap.py —— 少了这一步就会出现"提示浮层画不出来 / 匹配退化"
这类看起来毫无道理的静默故障）。

PyInstaller 也以本文件为入口（见 build.py），
`from app.application import ...` 会被静态分析顺着收进包里。
"""
from __future__ import annotations

import os
import sys

# 允许从任意工作目录启动（双击、快捷方式、命令行都行）
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 仓库自带的第三方库（.pylibs / .devtools）——必须在导入 app.* 之前加
from app._bootstrap import add_vendored_libs                     # noqa: E402

add_vendored_libs(_ROOT)

from app.application import main                     # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""main.py —— 入口脚本（薄壳）。

真正的应用在 `app/application.py`。这里只做两件事：
把仓库根放进 `sys.path`，然后把控制权交给 `app.application.main()`。

用法：
    python main.py                      正常启动 GUI
    python main.py --check              环境自检（逐项 OK/FAIL）
    python main.py --capture-debug      抓三个监视区域 + OCR，输出调试图
    python main.py --preview-notification   离线渲染一张通知预览图
    python main.py --debug              打开 DEBUG 日志

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

from app.application import main                     # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

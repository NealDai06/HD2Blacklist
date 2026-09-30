# -*- coding: utf-8 -*-
"""build.py —— PyInstaller 打包脚本。

用法：
    python build.py                 # 默认 onedir（推荐：启动快、杀软误报少）
    python build.py --onefile       # 单文件 exe
    python build.py --clean         # 先清理本次产物（用户 data/ 会先保留再放回）
    python build.py --console       # 保留控制台窗口（排查问题时用）

产物（都写到源码目录上一级的「发布包/」里）：
    ../发布包/HD2Blacklist/HD2Blacklist.exe      (onedir 文件夹版)
    ../发布包/HD2Blacklist.exe                   (onefile 单文件版)

并且会自动在产物旁边放好：
    · data/ 种子目录（示例配置 + 默认提示图 + 应用图标）
    · 使用说明.txt / LICENSE.txt（从仓库根目录复制，永远与源码同步）

注意：config.py 以 **exe 所在目录** 作为 BASE_DIR，
      所以 data/ 必须和 exe 放在一起（本脚本会自动复制）。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
#: 产物输出到源码目录**上一级**的 发布包/（与 source_code/ 平级）
OUT_ROOT = os.path.dirname(ROOT)
APP_NAME = "HD2Blacklist"
ENTRY = os.path.join(ROOT, "main.py")
DIST = os.path.join(OUT_ROOT, "发布包")
BUILD = os.path.join(ROOT, "build")
DATA = os.path.join(ROOT, "data")
#: 随包分发给人看的文档（源文件在仓库根目录里，打包时复制到产物旁边）
DOC_FILES = (("使用说明.txt", "使用说明.txt"),
             ("LICENSE", "LICENSE.txt"))


def log(msg):
    print(f"[build] {msg}")


def which_rapidocr():
    """返回实际安装的 rapidocr 包名（不同 Python 版本装的包不同）。"""
    for name in ("rapidocr_onnxruntime", "rapidocr"):
        if importlib.util.find_spec(name) is not None:
            return name
    return None


def ensure_seed_files():
    """确保 data/ 里有示例配置、默认提示图和应用图标，供打包一起带走。"""
    sys.path.insert(0, ROOT)
    import config                                    # noqa: E402
    config.ensure_dirs()
    from notification_config import ensure_notification_file   # noqa: E402
    from notifier import ensure_default_icon          # noqa: E402
    ensure_notification_file()
    ensure_default_icon()
    ensure_app_icon()
    log(f"种子文件就绪: {config.DATA_DIR}")


def ensure_app_icon() -> bool:
    """确保存在 app_icon.ico（PyInstaller 需要它作为 exe 图标）。

    有 PNG 没 ICO 时自动生成多尺寸 ICO。
    """
    import config                                    # noqa: E402
    if os.path.exists(config.APP_ICON_ICO):
        return True
    if not os.path.exists(config.APP_ICON_PATH):
        log("警告：没有 app_icon.png，exe 将使用默认图标")
        return False
    try:
        from PIL import Image
        with Image.open(config.APP_ICON_PATH) as im:
            im.convert("RGBA").save(
                config.APP_ICON_ICO, format="ICO",
                sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
                       (48, 48), (64, 64), (128, 128), (256, 256)])
        log(f"已生成 exe 图标: {config.APP_ICON_ICO}")
        return True
    except Exception as e:                               # noqa: BLE001
        log(f"生成 ICO 失败: {e}")
        return False


def build_command(onefile: bool, console: bool) -> list:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", APP_NAME,
        "--distpath", DIST,
        "--workpath", BUILD,
        "--specpath", BUILD,
        # 入口脚本
        ENTRY,
    ]
    cmd.append("--onefile" if onefile else "--onedir")
    cmd.append("--console" if console else "--noconsole")

    # ---- exe 图标 ----
    import config                                    # noqa: E402
    if os.path.exists(config.APP_ICON_ICO):
        cmd += ["--icon", config.APP_ICON_ICO]

    # ---- 隐式依赖 ----
    hidden = [
        "pystray._win32",
        "winotify",
        "wmi",
        "win32api", "win32con", "win32gui", "pythoncom", "pywintypes",
        "mss.windows",
        "pygame",
    ]
    rapid = which_rapidocr()
    if rapid:
        hidden.append(rapid)
        cmd += ["--collect-all", rapid]
        log(f"OCR 引擎: {rapid}（已 collect-all）")
    else:
        log("警告：未检测到 rapidocr，打包后 OCR 功能不可用")

    if importlib.util.find_spec("onnxruntime") is not None:
        cmd += ["--collect-all", "onnxruntime"]
    if importlib.util.find_spec("rapidfuzz") is not None:
        cmd += ["--collect-all", "rapidfuzz"]

    for h in hidden:
        cmd += ["--hidden-import", h]

    # ---- 静态资源 ----
    # 这三个都要打进 _internal/data/assets：用户删掉 data/ 目录重置后，
    # config.ensure_dirs() 会把它们补回去（否则图标丢失、自检报错）。
    import config                                    # noqa: E402
    for asset in (config.DEFAULT_ICON_PATH, config.APP_ICON_PATH,
                  config.APP_ICON_ICO):
        if os.path.exists(asset):
            cmd += ["--add-data", f"{asset}{os.pathsep}data/assets"]
        else:
            log(f"警告：缺少静态资源 {asset}")

    # ---- 排除用不到的大块头 ----
    # 重要：onnxruntime 里有 `try: import torch` 的 PyTorch 后端分支，
    # PyInstaller 会把整个 torch 拖进来（实测多出 ~500MB 且构建慢一倍）。
    # 本项目的 OCR 走 onnxruntime 的 CPU 执行器，完全不需要 torch。
    for mod in (
        # 深度学习框架（onnxruntime 的可选后端，实际不用）
        "torch", "torchvision", "torchaudio", "torchgen", "functorch",
        "transformers", "tokenizers", "safetensors", "huggingface_hub",
        "numba", "llvmlite", "sympy", "networkx",
        # 科学计算 / 绘图 / 数据分析
        "matplotlib", "scipy", "pandas", "IPython", "notebook", "jupyter",
        # GUI 框架（本项目只用 tkinter）
        "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
        # 其它明显无关的
        "tkinter.test", "test",
        "fsspec", "aiohttp", "yarl", "multidict", "frozenlist",
        "aiohappyeyeballs", "propcache", "pydantic", "annotated_types",
        "typing_inspection", "tabulate", "Cython", "pytest",
    ):
        cmd += ["--exclude-module", mod]

    return cmd


def copy_seed_data(target_dir: str):
    """把 data/ 种子目录复制到产物旁边（不覆盖用户已有数据）。"""
    dest = os.path.join(target_dir, "data")
    os.makedirs(dest, exist_ok=True)
    for sub in ("assets", "evidence", "logs"):
        os.makedirs(os.path.join(dest, sub), exist_ok=True)

    for name in ("notification.json", "user_config.json"):
        src = os.path.join(DATA, name)
        if os.path.exists(src) and not os.path.exists(os.path.join(dest, name)):
            shutil.copy2(src, os.path.join(dest, name))
            log(f"复制 {name}")

    for icon_name in ("default_icon.png", "app_icon.png", "app_icon.ico"):
        src_icon = os.path.join(DATA, "assets", icon_name)
        dst_icon = os.path.join(dest, "assets", icon_name)
        if os.path.exists(src_icon) and not os.path.exists(dst_icon):
            shutil.copy2(src_icon, dst_icon)
            log(f"复制 {icon_name}")


def copy_docs(target_dir: str):
    """把 使用说明.txt / LICENSE 复制到产物旁边（每次覆盖，保证与源码同步）。

    这两个文件是**仓库里的源文件**（`source_code/使用说明.txt`、`source_code/LICENSE`），
    打包时复制一份到 exe 同级，于是「仓库内容全在 source_code/ 里」和
    「分发包自带说明书与许可证」两件事同时成立。
    """
    os.makedirs(target_dir, exist_ok=True)
    for src_name, dst_name in DOC_FILES:
        src = os.path.join(ROOT, src_name)
        if not os.path.exists(src):
            log(f"警告：缺少 {src_name}，产物里不会有 {dst_name}")
            continue
        try:
            shutil.copy2(src, os.path.join(target_dir, dst_name))
            log(f"复制 {src_name} → {dst_name}")
        except OSError as e:
            log(f"复制 {src_name} 失败: {e}")


def _restore_kept_data(keep_dir: str, dest_dir: str) -> None:
    """把 --clean 前搬走的用户 data/ 放回去（用户数据整体优先）。"""
    if not keep_dir or not os.path.isdir(keep_dir):
        return
    dest = os.path.join(dest_dir, "data")
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(keep_dir):
        src = os.path.join(keep_dir, name)
        dst = os.path.join(dest, name)
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        elif os.path.exists(dst):
            os.remove(dst)
        shutil.move(src, dst)
    shutil.rmtree(keep_dir, ignore_errors=True)
    log(f"已恢复用户数据 → {dest}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="打包 HD2 黑名单")
    parser.add_argument("--onefile", action="store_true",
                        help="打包成单个 exe")
    parser.add_argument("--clean", action="store_true", help="先清理构建目录")
    parser.add_argument("--console", action="store_true",
                        help="保留控制台窗口（调试用）")
    args = parser.parse_args(argv)

    if importlib.util.find_spec("PyInstaller") is None:
        print("缺少 PyInstaller，请先执行：pip install pyinstaller")
        return 1

    keep_dir = None
    so_dir = os.path.join(DIST, APP_NAME)
    if args.clean:
        # 只清理「本次要产出的那一份」+ 构建缓存。
        # 注意不要整个删掉 发布包/：那里还放着解压版、发布压缩包和使用说明。
        targets = [BUILD,
                   os.path.join(DIST, f"{APP_NAME}.exe") if args.onefile
                   else so_dir]
        # 文件夹版的 data/ 就长在产物目录里面 —— 里面是用户的真实数据
        # （黑名单、证据截图、通知配置），清理前先搬出来，构建完再搬回去。
        if not args.onefile:
            src_data = os.path.join(so_dir, "data")
            if os.path.isdir(src_data):
                keep_dir = os.path.join(DIST, f"_{APP_NAME}_data_keep")
                shutil.rmtree(keep_dir, ignore_errors=True)
                shutil.move(src_data, keep_dir)
                log(f"先保留用户数据 {src_data}")
        for d in targets:
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
                log(f"已删除目录 {d}")
            elif os.path.exists(d):
                os.remove(d)
                log(f"已删除文件 {d}")

    ensure_seed_files()

    cmd = build_command(args.onefile, args.console)
    log("执行: " + " ".join(cmd))
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc != 0:
        _restore_kept_data(keep_dir, so_dir)      # 打包失败也不能丢数据
        print("打包失败")
        return rc

    if args.onefile:
        exe = os.path.join(DIST, f"{APP_NAME}.exe")
        copy_seed_data(DIST)
        copy_docs(DIST)
    else:
        _restore_kept_data(keep_dir, so_dir)
        exe = os.path.join(so_dir, f"{APP_NAME}.exe")
        copy_seed_data(so_dir)
        copy_docs(so_dir)          # exe 旁边一份（发给别人时随包带走）
        copy_docs(DIST)            # 发布包/ 顶层一份（自己看目录时一眼可见）

    print()
    if os.path.exists(exe):
        log(f"完成 ✅  {exe}")
    else:
        log("完成，但未找到 exe，请检查 PyInstaller 输出")
    log("首次运行会自动创建数据库、日志与证据目录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

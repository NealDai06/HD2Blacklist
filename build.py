# -*- coding: utf-8 -*-
"""build.py —— PyInstaller 打包脚本。

用法：
    python build.py                 # 默认 onedir（推荐：启动快、杀软误报少）
    python build.py --onefile       # 单文件 exe
    python build.py --clean         # 先清理本次产物（用户 data/ 会先备份走）
    python build.py --keep-data     # 保留产物里已有的 data/（开发自用，不推荐）
    python build.py --console       # 保留控制台窗口（排查问题时用）

产物（都写到源码目录上一级的「发布包/」里）：
    ../发布包/HD2Blacklist/HD2Blacklist.exe      (onedir 文件夹版)
    ../发布包/HD2Blacklist.exe                   (onefile 单文件版)

并且会自动在产物旁边放好：
    · data/ 种子目录（**只有三个图标**；配置与名单由程序首次运行时自己生成）
    · 使用说明.txt / LICENSE.txt（从仓库根目录复制，永远与源码同步）

⚠ **打包不带任何用户数据**：`data/` 里的 blacklist.db、notification.json、
   user_config.json、logs/、以及用户自己放进去的图片音频，统统不进产物 ——
   里面有本机使用痕迹（名单、自定义文件路径）。
   需要保留旧产物里的 data/ 时用 `--keep-data`（仅开发自用）。

注意：config.py 以 **exe 所在目录** 作为 BASE_DIR，
      所以 data/ 必须和 exe 放在一起（本脚本会自动创建种子目录）。
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
#: 产物输出到源码目录**上一级**的 发布包/（与 source_code/ 平级）
OUT_ROOT = os.path.dirname(ROOT)
APP_NAME = "HD2Blacklist"
ENTRY = os.path.join(ROOT, "main.py")
DIST = os.path.join(OUT_ROOT, "发布包")
BUILD = os.path.join(ROOT, "build")
DATA = os.path.join(ROOT, "data")
BACKUP_ROOT = os.path.join(OUT_ROOT, "_packaged_data_backup")
#: 随包分发给人看的文档（源文件在仓库根目录里，打包时复制到产物旁边）
#: THIRD_PARTY.md 必须跟着走：里面有配套插件的上游出处与两份上游许可正文
#: （MIT 要求保留版权声明与许可正文），还有 LGPL 依赖（pystray/pygame）的说明。
DOC_FILES = (("使用说明.txt", "使用说明.txt"),
             ("LICENSE", "LICENSE.txt"),
             ("THIRD_PARTY.md", "THIRD_PARTY.md"))
#: 允许进产物的静态资源（其余 data/assets 里的东西都是用户自己的图片/音频）
SEED_ASSETS = ("default_icon.png", "app_icon.png", "app_icon.ico")
#: 明确属于"用户痕迹"的文件，产物里见到就删
USER_DATA_FILES = ("blacklist.db", "notification.json", "user_config.json")


def log(msg):
    """打印构建日志。

    ⚠ 控制台编码可能是 GBK（中文版 Windows 的 cmd / PowerShell 默认如此），
    这时 `print("…✅")` 会抛 UnicodeEncodeError。以前它发生在**打包已经成功
    之后**的收尾步骤里，用户看到的就是"构建失败"，其实产物是好的 ——
    真实踩过一次（打包完成，最后一行日志把脚本打断了）。这里退化成
    编码安全的输出，绝不因为一个字符中断整个构建。
    """
    text = f"[build] {msg}"
    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"))


def ensure_seed_files():
    """确保 data/ 里有示例配置、默认提示图和应用图标，供打包一起带走。"""
    sys.path.insert(0, ROOT)
    from app import config  # noqa: E402
    config.ensure_dirs()
    from app.settings.notification_config import ensure_notification_file   # noqa: E402
    from app.notify.notifier import ensure_default_icon          # noqa: E402
    ensure_notification_file()
    ensure_default_icon()
    ensure_app_icon()
    log(f"种子文件就绪: {config.DATA_DIR}")


def ensure_app_icon() -> bool:
    """确保存在 app_icon.ico（PyInstaller 需要它作为 exe 图标）。

    有 PNG 没 ICO 时自动生成多尺寸 ICO。
    """
    from app import config  # noqa: E402
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

    # ---- exe 版本属性（右键属性 → 详细信息里能看到版本）----
    try:
        cmd += ["--version-file", version_file()]
    except Exception as e:                                   # noqa: BLE001
        log(f"版本资源生成失败（不影响打包）: {e}")

    # ---- exe 图标 ----
    from app import config  # noqa: E402
    if os.path.exists(config.APP_ICON_ICO):
        cmd += ["--icon", config.APP_ICON_ICO]

    # ---- 隐式依赖 ----
    # v2 不再截图 / OCR / 订阅 WMI 事件，所以 wmi、pywin32、onnxruntime 都不用带。
    # mss 还在：提示浮层要靠它拿显示器几何（不是抓游戏画面）。
    hidden = [
        "pystray._win32",
        "winotify",
        "mss.windows",
        "pygame",
    ]
    if importlib.util.find_spec("rapidfuzz") is not None:
        cmd += ["--collect-all", "rapidfuzz"]

    for h in hidden:
        cmd += ["--hidden-import", h]

    # ---- 静态资源 ----
    # 这三个都要打进 _internal/data/assets：用户删掉 data/ 目录重置后，
    # config.ensure_dirs() 会把它们补回去（否则图标丢失、自检报错）。
    from app import config  # noqa: E402
    for asset in (config.DEFAULT_ICON_PATH, config.APP_ICON_PATH,
                  config.APP_ICON_ICO):
        if os.path.exists(asset):
            cmd += ["--add-data", f"{asset}{os.pathsep}data/assets"]
        else:
            log(f"警告：缺少静态资源 {asset}")

    # ---- 排除用不到的大块头 ----
    # 本项目的第三方依赖只有 Pillow / numpy / mss / rapidfuzz / pystray /
    # winotify / pygame，下面这些都是被间接拖进来的无关货（几百 MB 起）。
    for mod in (
        # 深度学习 / 科学计算（旧版本靠 onnxruntime 做 OCR，v2 已经全部下线）
        "torch", "torchvision", "torchaudio", "torchgen", "functorch",
        "transformers", "tokenizers", "safetensors", "huggingface_hub",
        "onnxruntime", "cv2", "rapidocr", "rapidocr_onnxruntime",
        "numba", "llvmlite", "sympy", "networkx",
        "matplotlib", "scipy", "pandas", "IPython", "notebook", "jupyter",
        # GUI 框架（本项目只用 tkinter）
        "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
        # 其它明显无关的
        "tkinter.test", "test",
        "fsspec", "aiohttp", "yarl", "multidict", "frozenlist",
        "aiohappyeyeballs", "propcache", "pydantic", "annotated_types",
        "typing_inspection", "tabulate", "Cython", "pytest",
        # v2 不再订阅 WMI 进程事件、不再用 win32api 读按键
        "wmi", "win32api", "win32con", "win32gui", "pythoncom", "pywintypes",
    ):
        cmd += ["--exclude-module", mod]

    return cmd


def copy_seed_data(target_dir: str):
    """只放**静态资源**到产物旁边；一个字节的用户数据都不带。

    notification.json / user_config.json 里可能有本机的东西（自定义提示图/音效
    的绝对路径），所以不进产物 —— 程序首次运行会自己生成默认配置
    （`ensure_notification_file`）。
    """
    dest = os.path.join(target_dir, "data")
    os.makedirs(dest, exist_ok=True)
    for sub in ("assets", "logs"):
        os.makedirs(os.path.join(dest, sub), exist_ok=True)

    for icon_name in SEED_ASSETS:
        src = os.path.join(DATA, "assets", icon_name)
        dst = os.path.join(dest, "assets", icon_name)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            log(f"复制静态资源 {icon_name}")


def strip_user_data(target_dir: str) -> int:
    """产物落地后再扫一遍：任何本机使用痕迹都不许留在分发目录里。

    返回清掉的条目数。宁可多删（程序会自己重建），不能把用户记录带出去。
    """
    dest = os.path.join(target_dir, "data")
    if not os.path.isdir(dest):
        return 0
    removed = []

    for name in USER_DATA_FILES:
        p = os.path.join(dest, name)
        if os.path.isfile(p):
            os.remove(p)
            removed.append(name)

    for sub in ("logs",):
        d = os.path.join(dest, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
            removed.append(f"{sub}/{name}")

    # assets 里除了三个随包图标，其它都是用户自己挑的图片/音频
    assets = os.path.join(dest, "assets")
    if os.path.isdir(assets):
        for name in os.listdir(assets):
            if name in SEED_ASSETS:
                continue
            os.remove(os.path.join(assets, name))
            removed.append(f"assets/{name}")

    if removed:
        head = ", ".join(removed[:8])
        more = "" if len(removed) <= 8 else f" …共 {len(removed)} 项"
        log(f"已清除产物里的用户数据: {head}{more}")
    else:
        log("产物里没有用户数据 ✅")
    return len(removed)


def version_file() -> str:
    """生成 PyInstaller 版本资源，让 exe 属性里能看到版本号。"""
    from app import config                                  # noqa: E402
    ver = config.VERSION
    parts = (ver.split(".") + ["0", "0", "0", "0"])[:4]
    quad = ", ".join(parts)
    path = os.path.join(BUILD, "version_info.txt")
    os.makedirs(BUILD, exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="\n").write(f"""\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({quad}), prodvers=({quad}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'HD2 Blacklist'),
        StringStruct('FileDescription', 'HD2 黑名单 v{ver}'),
        StringStruct('FileVersion', '{ver}'),
        StringStruct('InternalName', 'HD2Blacklist'),
        StringStruct('OriginalFilename', 'HD2Blacklist.exe'),
        StringStruct('ProductName', 'HD2 黑名单'),
        StringStruct('ProductVersion', '{ver}'),
        StringStruct('Comments', '')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])]
)
""")
    log(f"版本资源: {ver}")
    return path


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
    parser.add_argument("--keep-data", action="store_true",
                        help="保留产物里已有的 data/（开发自用；默认会清掉用户数据）")
    parser.add_argument("--console", action="store_true",
                        help="保留控制台窗口（调试用）")
    args = parser.parse_args(argv)

    if importlib.util.find_spec("PyInstaller") is None:
        print("缺少 PyInstaller，请先执行：pip install pyinstaller")
        return 1

    keep_dir = None
    so_dir = os.path.join(DIST, APP_NAME)
    keep_data = bool(args.keep_data)
    if args.clean:
        # 只清理「本次要产出的那一份」+ 构建缓存。
        # 注意不要整个删掉 发布包/：那里还放着解压版、发布压缩包和使用说明。
        targets = [BUILD,
                   os.path.join(DIST, f"{APP_NAME}.exe") if args.onefile
                   else so_dir]
        # 文件夹版的 data/ 长在产物目录里面 —— 里面是本机使用痕迹
        # （黑名单、证据截图、区域坐标、自定义文件路径）。
        # 默认**不**带进新产物：先备份到 _packaged_data_backup/（不删），
        # 免得哪天要找回旧名单；`--keep-data` 则照旧搬回来（开发自用）。
        if not args.onefile:
            src_data = os.path.join(so_dir, "data")
            if os.path.isdir(src_data):
                if keep_data:
                    keep_dir = os.path.join(DIST, f"_{APP_NAME}_data_keep")
                    shutil.rmtree(keep_dir, ignore_errors=True)
                    shutil.move(src_data, keep_dir)
                    log(f"按 --keep-data 先保留用户数据 {src_data}")
                else:
                    stamp = time.strftime("%Y%m%d_%H%M%S")
                    backup = os.path.join(BACKUP_ROOT, f"packaged_{stamp}")
                    os.makedirs(BACKUP_ROOT, exist_ok=True)
                    shutil.move(src_data, backup)
                    log(f"旧产物里的用户数据已备份到 {backup}（新版不带它）")
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
        if not keep_data:
            strip_user_data(DIST)
    else:
        _restore_kept_data(keep_dir, so_dir)
        exe = os.path.join(so_dir, f"{APP_NAME}.exe")
        copy_seed_data(so_dir)
        copy_docs(so_dir)          # exe 旁边一份（发给别人时随包带走）
        copy_docs(DIST)            # 发布包/ 顶层一份（自己看目录时一眼可见）
        if not keep_data:
            strip_user_data(so_dir)

    print()
    if os.path.exists(exe):
        log(f"完成 ✅  {exe}")
    else:
        log("完成，但未找到 exe，请检查 PyInstaller 输出")
    log("首次运行会自动创建数据库与日志目录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

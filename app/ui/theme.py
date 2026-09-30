# -*- coding: utf-8 -*-
"""theme.py —— 统一的暗色主题（配色 / ttk 样式 / 窗口图标 / 托盘图标）。

设计目标：
    * 深色底 + 金色点缀（取自帽徽配色），和游戏本体的"军绿/金色"气质一致
    * 所有界面（主窗口、通知设置、校准器、各类对话框）共用一套配色
    * **只改外观**：不改任何控件行为，也不改测试依赖的 style 名
      （`Hit.TLabel` / `Paused.TLabel` 保留原名）

对外接口：
    PALETTE                  配色表（dict）
    apply_theme(root)        在主窗口创建后调用一次
    make_menu(parent)        建一个已配色的 tk.Menu
    style_text(widget)       给 tk.Text 上色
    style_canvas(widget, bg) 给 tk.Canvas 上色
    apply_window_icon(win)   设置标题栏 / 任务栏图标
    load_icon_image(size)    取 PIL 图标（给托盘、内嵌标题用）
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from app.config import APP_ICON_ICO, APP_ICON_PATH, get_logger

# ==========================================================================
# 配色
# ==========================================================================
PALETTE = {
    "bg":         "#1b1e23",   # 窗口底色
    "panel":      "#23272e",   # 面板 / 工具栏 / 状态栏
    "panel_alt":  "#2b3038",   # 悬停
    "field":      "#2b313a",   # 输入框
    "border":     "#3a414c",
    "fg":         "#e6e9ee",
    "fg_muted":   "#8f98a3",
    "fg_dim":     "#6b737d",
    "accent":     "#d9b44a",   # 金色（帽徽）
    "accent_hi":  "#ecc75c",
    "accent_lo":  "#b8963a",
    "on_accent":  "#1b1e23",   # 金底上的文字
    "danger":     "#e05a4f",
    "ok":         "#5fbf6a",
    "row_odd":    "#20242a",
    "row_sel":    "#3d4653",
    "flash":      "#6d2f2f",   # 命中闪烁背景
    "heading":    "#2a2f37",   # 表头
}

FONT_FAMILY = "Microsoft YaHei UI"
FONT_BASE = (FONT_FAMILY, 9)
FONT_BOLD = (FONT_FAMILY, 9, "bold")
FONT_TITLE = (FONT_FAMILY, 12, "bold")
FONT_SMALL = (FONT_FAMILY, 8)
FONT_MONO = ("Consolas", 9)

#: 图标引用必须一直持有，否则会被 GC 掉、窗口图标变空白
_icon_refs = []


# ==========================================================================
def apply_theme(root: tk.Misc) -> dict:
    """配置全局 ttk 样式 + tk 默认颜色。返回配色表。"""
    p = PALETTE
    style = ttk.Style(root)

    # clam 主题才支持下面这些精细配色；
    # 而且 Treeview 的行背景 tag（命中闪烁）也只有在 clam 下才渲染。
    try:
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=p["bg"])
    try:
        root.option_add("*Font", FONT_BASE)
    except tk.TclError:
        pass

    # 全局兜底
    style.configure(".", background=p["bg"], foreground=p["fg"],
                    fieldbackground=p["field"], bordercolor=p["border"],
                    lightcolor=p["panel"], darkcolor=p["panel"],
                    troughcolor=p["bg"], focuscolor=p["accent"],
                    font=FONT_BASE)

    # ---- 容器 ----
    style.configure("TFrame", background=p["bg"])
    style.configure("Panel.TFrame", background=p["panel"])
    style.configure("TLabel", background=p["bg"], foreground=p["fg"])
    style.configure("Muted.TLabel", background=p["bg"], foreground=p["fg_muted"])
    style.configure("PanelMuted.TLabel", background=p["panel"],
                    foreground=p["fg_muted"])
    style.configure("Title.TLabel", background=p["panel"],
                    foreground=p["accent"], font=FONT_TITLE)
    style.configure("Version.TLabel", background=p["panel"],
                    foreground=p["fg_dim"], font=FONT_SMALL)

    style.configure("TLabelframe", background=p["bg"],
                    bordercolor=p["border"], relief="solid")
    style.configure("TLabelframe.Label", background=p["bg"],
                    foreground=p["accent"], font=FONT_BOLD)
    style.configure("TSeparator", background=p["border"])

    # ---- 按钮 ----
    style.configure("TButton", background=p["panel"], foreground=p["fg"],
                    bordercolor=p["border"], focuscolor=p["accent"],
                    relief="flat", padding=(10, 5), font=FONT_BASE)
    style.map("TButton",
              background=[("pressed", p["accent_lo"]),
                          ("active", p["panel_alt"]),
                          ("disabled", p["panel"])],
              foreground=[("disabled", p["fg_dim"])],
              bordercolor=[("active", p["accent"]),
                           ("focus", p["accent"])])

    # 主操作按钮（金色实底）
    style.configure("Accent.TButton", background=p["accent"],
                    foreground=p["on_accent"], bordercolor=p["accent"],
                    relief="flat", padding=(10, 5), font=FONT_BOLD)
    style.map("Accent.TButton",
              background=[("pressed", p["accent_lo"]),
                          ("active", p["accent_hi"]),
                          ("disabled", p["panel"])],
              foreground=[("disabled", p["fg_dim"])])

    # 危险操作按钮（红色描边）
    style.configure("Danger.TButton", background=p["panel"],
                    foreground=p["danger"], bordercolor=p["border"],
                    relief="flat", padding=(10, 5))
    style.map("Danger.TButton",
              background=[("active", p["panel_alt"])],
              foreground=[("active", p["danger"])],
              bordercolor=[("active", p["danger"])])

    # ---- 输入类 ----
    style.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"],
                    bordercolor=p["border"], insertcolor=p["accent"],
                    padding=4, relief="flat")
    style.map("TEntry",
              bordercolor=[("focus", p["accent"])],
              lightcolor=[("focus", p["accent"])],
              darkcolor=[("focus", p["accent"])])

    style.configure("TCombobox", fieldbackground=p["field"],
                    background=p["panel"], foreground=p["fg"],
                    arrowcolor=p["accent"], bordercolor=p["border"],
                    padding=4, relief="flat")
    style.map("TCombobox",
              fieldbackground=[("readonly", p["field"]),
                               ("disabled", p["panel"])],
              foreground=[("readonly", p["fg"]), ("disabled", p["fg_dim"])],
              arrowcolor=[("active", p["accent_hi"])],
              bordercolor=[("focus", p["accent"])])

    style.configure("TSpinbox", fieldbackground=p["field"],
                    background=p["panel"], foreground=p["fg"],
                    arrowcolor=p["accent"], bordercolor=p["border"],
                    insertcolor=p["accent"], padding=4, relief="flat")
    style.map("TSpinbox",
              arrowcolor=[("active", p["accent_hi"])],
              bordercolor=[("focus", p["accent"])])

    # 下拉列表是独立 Tk listbox，只能通过 option database 上色
    try:
        root.option_add("*TCombobox*Listbox.background", p["field"])
        root.option_add("*TCombobox*Listbox.foreground", p["fg"])
        root.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
        root.option_add("*TCombobox*Listbox.selectForeground", p["on_accent"])
        root.option_add("*TCombobox*Listbox.font", FONT_BASE)
        root.option_add("*TCombobox*Listbox.borderWidth", 0)
    except tk.TclError:
        pass

    # ---- 勾选 / 单选 ----
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=p["bg"], foreground=p["fg"],
                        focuscolor=p["accent"], indicatorcolor=p["field"],
                        bordercolor=p["border"], padding=2)
        style.map(name,
                  background=[("active", p["bg"])],
                  foreground=[("active", p["accent"]),
                              ("disabled", p["fg_dim"])],
                  indicatorcolor=[("selected", p["accent"]),
                                  ("pressed", p["accent_lo"])])

    # ---- 刻度条 ----
    style.configure("Horizontal.TScale", background=p["bg"],
                    troughcolor=p["field"], bordercolor=p["border"],
                    lightcolor=p["accent"], darkcolor=p["accent_lo"])

    # ---- 分页 ----
    style.configure("TNotebook", background=p["bg"], bordercolor=p["border"],
                    tabmargins=(2, 4, 2, 0), relief="flat")
    style.configure("TNotebook.Tab", background=p["panel"],
                    foreground=p["fg_muted"], padding=(16, 7),
                    bordercolor=p["border"], font=FONT_BASE)
    style.map("TNotebook.Tab",
              background=[("selected", p["bg"]), ("active", p["panel_alt"])],
              foreground=[("selected", p["accent"])],
              expand=[("selected", (0, 0, 0, 0))])

    # ---- 列表 ----
    style.configure("Treeview", background=p["panel"],
                    fieldbackground=p["panel"], foreground=p["fg"],
                    bordercolor=p["border"], borderwidth=0,
                    rowheight=26, font=FONT_BASE, relief="flat")
    style.map("Treeview",
              background=[("selected", p["row_sel"])],
              foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=p["heading"],
                    foreground=p["accent"], relief="flat",
                    padding=(6, 7), font=FONT_BOLD, bordercolor=p["border"])
    style.map("Treeview.Heading",
              background=[("active", p["panel_alt"])],
              foreground=[("active", p["accent_hi"])])

    # ---- 滚动条 ----
    style.configure("Vertical.TScrollbar", background=p["panel"],
                    troughcolor=p["bg"], bordercolor=p["bg"],
                    arrowcolor=p["fg_muted"], relief="flat", width=12)
    style.configure("Horizontal.TScrollbar", background=p["panel"],
                    troughcolor=p["bg"], bordercolor=p["bg"],
                    arrowcolor=p["fg_muted"], relief="flat")
    style.map("Vertical.TScrollbar",
              background=[("active", p["panel_alt"]), ("pressed", p["accent_lo"])])
    style.map("Horizontal.TScrollbar",
              background=[("active", p["panel_alt"]), ("pressed", p["accent_lo"])])

    # ---- 状态指示（保留原 style 名，测试依赖）----
    style.configure("Hit.TLabel", background=p["panel"],
                    foreground=p["ok"], font=FONT_BOLD)
    style.configure("Paused.TLabel", background=p["panel"],
                    foreground=p["accent"], font=FONT_BOLD)

    return p


# ==========================================================================
# tk 原生控件上色（ttk 样式管不到它们）
# ==========================================================================
def make_menu(parent: tk.Misc, **kwargs) -> tk.Menu:
    """建一个已配色的 tk.Menu。"""
    p = PALETTE
    opts = dict(tearoff=0, bg=p["panel"], fg=p["fg"],
                activebackground=p["accent"], activeforeground=p["on_accent"],
                bd=0, relief="flat", activeborderwidth=0,
                disabledforeground=p["fg_dim"], font=FONT_BASE)
    opts.update(kwargs)
    return tk.Menu(parent, **opts)


def style_text(widget: tk.Text, mono: bool = False) -> None:
    """给 tk.Text 上色。"""
    p = PALETTE
    widget.configure(bg=p["field"], fg=p["fg"],
                     insertbackground=p["accent"],
                     selectbackground=p["accent"],
                     selectforeground=p["on_accent"],
                     relief="flat", bd=0,
                     highlightthickness=1,
                     highlightbackground=p["border"],
                     highlightcolor=p["accent"],
                     font=FONT_MONO if mono else FONT_BASE)


def style_canvas(widget: tk.Canvas, bg: str = None) -> None:
    widget.configure(bg=bg or PALETTE["bg"], highlightthickness=0)


def style_listbox(widget: tk.Listbox) -> None:
    p = PALETTE
    widget.configure(bg=PALETTE["field"], fg=p["fg"],
                     selectbackground=p["accent"],
                     selectforeground=p["on_accent"],
                     relief="flat", bd=0, highlightthickness=1,
                     highlightbackground=p["border"])


# ==========================================================================
# 图标
# ==========================================================================
def load_icon_image(size: int = 64):
    """返回一个正方形 PIL 图标（给托盘 / 内嵌标题 / header 用）。"""
    try:
        from PIL import Image
        img = Image.open(APP_ICON_PATH).convert("RGBA")
        if img.width != img.height:
            side = min(img.size)
            left = (img.width - side) // 2
            top = (img.height - side) // 2
            img = img.crop((left, top, left + side, top + side))
        return img.resize((size, size), Image.LANCZOS)
    except Exception as e:                               # noqa: BLE001
        get_logger("theme").warning("读取应用图标失败: %s", e)
        return None


def apply_window_icon(window: tk.Misc) -> None:
    """给窗口设置标题栏 / 任务栏图标。

    * `iconphoto` 走 WM_SETICON，任务栏与 Alt-Tab 都用它
    * `iconbitmap(default=...)` 用多尺寸 .ico，小图标更锐利
    两者都做，并且把 PhotoImage 存起来防止被 GC。
    """
    log = get_logger("theme")

    if os.path.exists(APP_ICON_PATH):
        try:
            img = tk.PhotoImage(file=APP_ICON_PATH)
            window.iconphoto(True, img)
            _icon_refs.append(img)
        except Exception as e:                           # noqa: BLE001
            log.debug("iconphoto 失败: %s", e)

    if os.path.exists(APP_ICON_ICO):
        try:
            window.iconbitmap(default=APP_ICON_ICO)
        except Exception as e:                           # noqa: BLE001
            log.debug("iconbitmap 失败: %s", e)


def set_taskbar_identity(app_id: str) -> None:
    """设置 AppUserModelID。

    必须在创建窗口**之前**调用，否则任务栏会把程序当成 python.exe，
    图标也会变成 Python 的图标。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception as e:                               # noqa: BLE001
        get_logger("theme").debug("设置 AppUserModelID 失败: %s", e)

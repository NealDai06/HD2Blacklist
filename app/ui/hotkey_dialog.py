# -*- coding: utf-8 -*-
"""hotkey_dialog.py —— 自定义「聊天框扫描」快捷键。

交互：
    点一下按键框 → 直接按下你想用的组合键 → 自动识别并显示
    （只按 Ctrl/Alt/Shift 会切换对应的修饰键复选框，不会当成主键）

所有 tkinter 操作都在主线程。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from app.ui import theme
from app.config import get_logger
from app.settings.hotkey_config import (FORBIDDEN_VK, combo_name, keysym_to_vk,
                           modifier_of_keysym)

VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10


def _live_modifiers() -> dict:
    """直接问系统当前按着哪些修饰键（比解析 Tk 的 state 位更可靠）。"""
    try:
        import win32api
        return {
            "ctrl": bool(win32api.GetAsyncKeyState(VK_CONTROL) & 0x8000),
            "alt": bool(win32api.GetAsyncKeyState(VK_MENU) & 0x8000),
            "shift": bool(win32api.GetAsyncKeyState(VK_SHIFT) & 0x8000),
        }
    except Exception:                                    # noqa: BLE001
        return {"ctrl": False, "alt": False, "shift": False}


class HotkeyDialog:
    """快捷键设置对话框（模态）。"""

    def __init__(self, master, hotkey_config, on_saved=None):
        self.cfg = hotkey_config
        self.on_saved = on_saved
        self.log = get_logger("hotkey")
        self.result = None

        current = self.cfg.get()
        self.vk_var = tk.IntVar(value=current["vk"])
        self.ctrl_var = tk.BooleanVar(value=current["ctrl"])
        self.alt_var = tk.BooleanVar(value=current["alt"])
        self.shift_var = tk.BooleanVar(value=current["shift"])
        self.enabled_var = tk.BooleanVar(value=current["enabled"])
        self.key_text = tk.StringVar()
        self.status_var = tk.StringVar(value="")

        self.top = tk.Toplevel(master)
        self.top.title("聊天框扫描快捷键")
        self.top.transient(master)
        self.top.resizable(False, False)
        self.top.configure(bg=theme.PALETTE["bg"])
        theme.apply_window_icon(self.top)

        self._build()
        self._refresh_key_text()
        self.top.grab_set()
        self.top.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.top.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.top.winfo_height()) // 3
        self.top.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.top.wait_window()

    # ---------------------------------------------------------------- 构建
    def _build(self):
        p = theme.PALETTE
        frm = ttk.Frame(self.top, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Checkbutton(frm, text="启用热键扫描聊天框",
                        variable=self.enabled_var,
                        command=self._refresh_key_text).pack(anchor="w")

        box = ttk.LabelFrame(frm, text="按键", padding=12)
        box.pack(fill="x", pady=(10, 6))

        ttk.Label(box, text="点击下面的框，然后按下你想用的组合键：",
                  style="Muted.TLabel").pack(anchor="w")

        self.key_label = tk.Label(
            box, textvariable=self.key_text, width=24, height=2,
            bg=p["field"], fg=p["accent"], font=(theme.FONT_FAMILY, 15, "bold"),
            relief="flat", bd=0, highlightthickness=2,
            highlightbackground=p["border"], highlightcolor=p["accent"],
            cursor="hand2")
        self.key_label.pack(fill="x", pady=8)
        self.key_label.bind("<Button-1>", self._start_capture)
        self.key_label.bind("<FocusIn>", lambda e: self._set_hint("请按下组合键…"))
        self.key_label.bind("<KeyPress>", self._on_key)

        mods = ttk.Frame(box)
        mods.pack(anchor="w")
        ttk.Checkbutton(mods, text="Ctrl", variable=self.ctrl_var,
                        command=self._refresh_key_text).pack(side="left")
        ttk.Checkbutton(mods, text="Alt", variable=self.alt_var,
                        command=self._refresh_key_text).pack(side="left",
                                                             padx=(12, 0))
        ttk.Checkbutton(mods, text="Shift", variable=self.shift_var,
                        command=self._refresh_key_text).pack(side="left",
                                                             padx=(12, 0))

        ttk.Label(frm,
                  text=("提示：\n"
                        "  · 热键由系统注册，**游戏里（前台全屏）也能触发**；\n"
                        "    注册成功后系统会独占该键，其它程序收不到它，\n"
                        "    所以请避开游戏内的按键，比如 Q / E / 空格\n"
                        "  · 只按 Ctrl / Alt / Shift 只会切换上面的复选框\n"
                        "  · Ctrl 与 Alt 必须完全一致（绑了就得按，没绑就不能按）\n"
                        "  · Shift 只在绑定时才要求 —— 这样游戏中按着 Shift 跑动\n"
                        "    也不会影响 F8 这类不带 Shift 的热键\n"
                        "  · ESC 已被「菜单扫描」占用，不能绑定\n"
                        "  · 万一该键被别的程序占用会退回按键轮询模式，\n"
                        "    状态栏会给出提示（轮询模式下只有本程序前台时有效）"),
                  style="Muted.TLabel", justify="left").pack(anchor="w",
                                                             pady=(8, 0))

        ttk.Label(frm, textvariable=self.status_var,
                  style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=(14, 0))
        ttk.Button(bar, text="取消", command=self.close).pack(side="right",
                                                             padx=4)
        ttk.Button(bar, text="保存", style="Accent.TButton",
                   command=self.save).pack(side="right")
        ttk.Button(bar, text="恢复默认 F8",
                   command=self.restore_default).pack(side="right", padx=4)

    # ---------------------------------------------------------------- 捕获
    def _start_capture(self, _event=None):
        self.key_label.focus_set()
        self._set_hint("请按下组合键…")

    def _set_hint(self, text: str):
        self.status_var.set(text)

    def _on_key(self, event):
        ks = event.keysym

        # 纯修饰键：切换复选框
        mod = modifier_of_keysym(ks)
        if mod is not None:
            live = _live_modifiers()
            {  "ctrl": self.ctrl_var,
               "alt": self.alt_var,
               "shift": self.shift_var}[mod].set(live[mod] or True)
            self._refresh_key_text()
            self._set_hint("已记录修饰键，继续按主键（如 F8、S、1）")
            return "break"

        if ks == "Escape":
            self._set_hint("已取消输入")
            self.top.focus_set()
            return "break"

        vk = keysym_to_vk(ks)
        if vk is None:
            self._set_hint(f"不支持这个键：{ks}")
            return "break"

        if vk in FORBIDDEN_VK:
            self._set_hint(FORBIDDEN_VK[vk])
            return "break"

        # 用系统状态决定修饰键，避免依赖 Tk 的 state 位定义
        live = _live_modifiers()
        self.ctrl_var.set(live["ctrl"])
        self.alt_var.set(live["alt"])
        self.shift_var.set(live["shift"])
        self.vk_var.set(vk)
        self._refresh_key_text()
        self._set_hint(f"已选择 {self._current_name()}，点「保存」生效")
        return "break"

    def _current_name(self) -> str:
        return combo_name(self.vk_var.get(), self.ctrl_var.get(),
                          self.alt_var.get(), self.shift_var.get())

    def _refresh_key_text(self):
        name = self._current_name()
        if not self.enabled_var.get():
            self.key_text.set(f"{name}（未启用）")
        else:
            self.key_text.set(name)

    # ---------------------------------------------------------------- 动作
    def restore_default(self):
        from app.config import DEFAULT_HOTKEY
        self.vk_var.set(DEFAULT_HOTKEY["vk"])
        self.ctrl_var.set(False)
        self.alt_var.set(False)
        self.shift_var.set(False)
        self._refresh_key_text()
        self._set_hint("已恢复默认 F8")

    def save(self):
        try:
            self.cfg.set_binding(self.vk_var.get(), self.ctrl_var.get(),
                                 self.alt_var.get(), self.shift_var.get())
            self.cfg.set_enabled(self.enabled_var.get())
        except ValueError as e:
            messagebox.showwarning("无法保存", str(e), parent=self.top)
            return
        self.result = self.cfg.get()
        self.log.info("聊天框扫描快捷键已保存: %s（启用=%s）",
                      self.cfg.display, self.result["enabled"])
        if self.on_saved:
            try:
                self.on_saved(self.result)
            except Exception as e:                       # noqa: BLE001
                self.log.warning("on_saved 回调异常: %s", e)
        self.top.destroy()

    def close(self):
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()

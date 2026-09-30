# -*- coding: utf-8 -*-
"""calibrator.py —— 监视区域校准器。

功能：
    * 下拉选择区域（聊天框事件区 / HUD 玩家列表 / ESC 菜单玩家列表）
    * 显示当前坐标 + 来源（默认 / 用户自定义）
    * [框选新区域] —— 全屏半透明遮罩上拖拽框选，实时显示坐标与尺寸
    * [恢复默认]   —— 清除该区域的用户自定义
    * [实时预览]   —— 立即截图并显示该区域当前画面

所有 tkinter 操作都在主线程（本模块只由 GUI 主线程调用）。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from app.ui import theme
from app.config import get_logger
from app.settings.region_config import RegionConfig


class RegionSelector:
    """全屏框选遮罩：在虚拟桌面上拖拽出一个矩形。"""

    def __init__(self, master, on_done, on_cancel=None):
        self.master = master
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.log = get_logger("calibrator")

        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-alpha", 0.25)
        except tk.TclError:
            pass

        # 覆盖整个虚拟桌面（支持多显示器 / 负坐标）
        vx = master.winfo_vrootx()
        vy = master.winfo_vrooty()
        vw = master.winfo_vrootwidth()
        vh = master.winfo_vrootheight()
        self.origin = (vx, vy)
        self.top.geometry(f"{vw}x{vh}+{vx}+{vy}")

        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            vw // 2, 40,
            text="按住鼠标左键拖拽框选区域　·　ESC 取消",
            fill="#ffffff", font=("Microsoft YaHei UI", 16))

        self.start = None
        self.rect_id = None
        self.label_id = None

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.top.bind("<Escape>", self._on_escape)

        self.top.focus_force()
        self.top.grab_set()

    # ---------------------------------------------------------------- 事件
    def _on_press(self, event):
        self.start = (event.x_root, event.y_root)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        if self.label_id:
            self.canvas.delete(self.label_id)
        cx, cy = self._to_canvas(*self.start)
        self.rect_id = self.canvas.create_rectangle(
            cx, cy, cx, cy, outline="#00ff88", width=2)
        self.label_id = self.canvas.create_text(
            cx + 8, cy + 16, anchor="w", fill="#00ff88", text="",
            font=("Consolas", 12))

    def _to_canvas(self, x_root, y_root):
        return x_root - self.origin[0], y_root - self.origin[1]

    def _on_drag(self, event):
        if not self.start:
            return
        x0, y0 = self._to_canvas(*self.start)
        x1, y1 = self._to_canvas(event.x_root, event.y_root)
        self.canvas.coords(self.rect_id, x0, y0, x1, y1)
        left = min(self.start[0], event.x_root)
        top = min(self.start[1], event.y_root)
        w = abs(event.x_root - self.start[0])
        h = abs(event.y_root - self.start[1])
        self.canvas.coords(self.label_id, x0 + 8, y0 + 16)
        self.canvas.itemconfigure(
            self.label_id, text=f"x={left} y={top}  {w}×{h}")

    def _on_release(self, event):
        if not self.start:
            return
        left = min(self.start[0], event.x_root)
        top = min(self.start[1], event.y_root)
        w = abs(event.x_root - self.start[0])
        h = abs(event.y_root - self.start[1])
        self._close()
        if w < 8 or h < 8:
            messagebox.showwarning("框选无效", "区域太小（至少 8×8 像素），请重新框选。",
                                   parent=self.master)
            if self.on_cancel:
                self.on_cancel()
            return
        self.on_done({"left": int(left), "top": int(top),
                      "width": int(w), "height": int(h)})

    def _on_escape(self, _event=None):
        self._close()
        if self.on_cancel:
            self.on_cancel()

    def _close(self):
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        try:
            self.top.destroy()
        except tk.TclError:
            pass


class CalibratorDialog:
    """区域校准对话框。"""

    def __init__(self, master, region_config: RegionConfig, capture,
                 on_changed=None):
        self.master = master
        self.region_config = region_config
        self.capture = capture
        self.on_changed = on_changed
        self.log = get_logger("calibrator")
        self._preview_img = None
        self._preview_win = None

        self.top = tk.Toplevel(master)
        self.top.title("校准监视区域")
        self.top.transient(master)
        self.top.configure(bg=theme.PALETTE["bg"])
        theme.apply_window_icon(self.top)
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self.close)

        self.key_var = tk.StringVar(value=RegionConfig.keys()[0])
        self.coords_var = tk.StringVar()
        self.source_var = tk.StringVar()
        self.status_var = tk.StringVar(value="选择区域后点击 [框选新区域]")

        self._build()
        self._refresh()
        self._center()

    # ---------------------------------------------------------------- 构建
    def _build(self):
        pad = {"padx": 10, "pady": 6}
        frm = ttk.LabelFrame(self.top, text="区域")
        frm.pack(fill="x", **pad)

        row = ttk.Frame(frm)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Label(row, text="监视区域：").pack(side="left")
        labels = [f"{RegionConfig.label(k)}  ({k})" for k in RegionConfig.keys()]
        self._keys = RegionConfig.keys()
        self.combo = ttk.Combobox(row, values=labels, state="readonly",
                                  width=38)
        self.combo.current(0)
        self.combo.pack(side="left", padx=6)
        self.combo.bind("<<ComboboxSelected>>", lambda e: self._refresh())

        info = ttk.Frame(frm)
        info.pack(fill="x", padx=8)
        ttk.Label(info, text="当前坐标：").grid(row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.coords_var,
                  font=("Consolas", 10)).grid(row=0, column=1, sticky="w")
        ttk.Label(info, text="来源：").grid(row=1, column=0, sticky="w")
        ttk.Label(info, textvariable=self.source_var).grid(row=1, column=1,
                                                           sticky="w")
        desc = ttk.Label(frm, text="", style="Muted.TLabel", wraplength=380,
                         justify="left")
        desc.pack(fill="x", padx=8, pady=(4, 8))
        self._desc = desc

        btns = ttk.Frame(self.top)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="框选新区域", command=self.select_region
                   ).pack(side="left", padx=4)
        ttk.Button(btns, text="恢复默认", command=self.reset_region
                   ).pack(side="left", padx=4)
        ttk.Button(btns, text="恢复全部默认", command=self.reset_all
                   ).pack(side="left", padx=4)
        ttk.Button(btns, text="实时预览", command=self.preview
                   ).pack(side="left", padx=4)

        ttk.Label(self.top, textvariable=self.status_var,
                  style="Muted.TLabel").pack(fill="x", padx=12, pady=(0, 4))

        bottom = ttk.Frame(self.top)
        bottom.pack(fill="x", **pad)
        ttk.Button(bottom, text="关闭", command=self.close).pack(side="right")

    def _center(self):
        self.top.update_idletasks()
        w, h = self.top.winfo_width(), self.top.winfo_height()
        x = self.master.winfo_rootx() + (self.master.winfo_width() - w) // 2
        y = self.master.winfo_rooty() + (self.master.winfo_height() - h) // 2
        self.top.geometry(f"+{max(0, x)}+{max(0, y)}")

    # ---------------------------------------------------------------- 状态
    def _current_key(self) -> str:
        return self._keys[self.combo.current()]

    def _refresh(self):
        key = self._current_key()
        r = self.region_config.get(key)
        self.coords_var.set(
            f"left={r['left']}  top={r['top']}  "
            f"width={r['width']}  height={r['height']}"
        )
        src = self.region_config.source_of(key)
        self.source_var.set("用户自定义" if src == "user" else "内置默认值")
        self._desc.configure(text=RegionConfig.meta(key).get("desc", ""))

    # ---------------------------------------------------------------- 操作
    def select_region(self):
        key = self._current_key()
        self.top.withdraw()
        self.top.update()

        def done(rect):
            self.top.deiconify()
            try:
                self.region_config.set(key, rect)
            except ValueError as e:
                messagebox.showerror("保存失败", str(e), parent=self.top)
                return
            self.status_var.set(
                f"已保存 {RegionConfig.label(key)}："
                f"{rect['width']}×{rect['height']} @ ({rect['left']},{rect['top']})"
            )
            self._refresh()
            self._notify_changed()

        def cancel():
            self.top.deiconify()
            self.status_var.set("已取消框选")

        self.top.after(200, lambda: RegionSelector(self.top, done, cancel))

    def reset_region(self):
        key = self._current_key()
        if not self.region_config.is_customized(key):
            self.status_var.set("该区域本来就在用默认值")
            return
        self.region_config.reset(key)
        self._refresh()
        self.status_var.set(f"{RegionConfig.label(key)} 已恢复默认")
        self._notify_changed()

    def reset_all(self):
        if not messagebox.askyesno("确认", "恢复全部区域为默认值？", parent=self.top):
            return
        self.region_config.reset_all()
        self._refresh()
        self.status_var.set("全部区域已恢复默认")
        self._notify_changed()

    def preview(self):
        key = self._current_key()
        if self.capture is None:
            messagebox.showinfo("提示", "截图模块不可用", parent=self.top)
            return
        img = self.capture.grab(key)
        if img is None:
            messagebox.showwarning("预览失败",
                                   "无法截取该区域，请确认坐标是否超出屏幕。",
                                   parent=self.top)
            return
        self._show_preview(key, img)

    def _show_preview(self, key, img):
        from PIL import Image, ImageTk
        r = self.region_config.get(key)

        max_w, max_h = 560, 320
        scale = min(1.0, max_w / max(1, img.width), max_h / max(1, img.height))
        disp = img
        if scale < 1.0:
            disp = img.resize((max(1, int(img.width * scale)),
                               max(1, int(img.height * scale))),
                              Image.LANCZOS)

        self._preview_img = ImageTk.PhotoImage(disp)

        if self._preview_win is not None and self._preview_win.winfo_exists():
            self._preview_win.destroy()
        win = tk.Toplevel(self.top)
        self._preview_win = win
        win.title(f"预览 - {RegionConfig.label(key)}")
        win.transient(self.top)
        win.configure(bg=theme.PALETTE["bg"])
        tk.Label(win, bg=theme.PALETTE["bg"], fg=theme.PALETTE["fg"],
                 text=(f"{RegionConfig.label(key)}   "
                       f"{r['width']}×{r['height']} @ "
                       f"({r['left']},{r['top']})")).pack(padx=8, pady=6)
        tk.Label(win, image=self._preview_img, bd=1, relief="solid").pack(
            padx=8, pady=4)
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=8)

    def _notify_changed(self):
        if self.on_changed:
            try:
                self.on_changed()
            except Exception as e:                       # noqa: BLE001
                self.log.warning("on_changed 回调异常: %s", e)

    def close(self):
        if self._preview_win is not None:
            try:
                self._preview_win.destroy()
            except tk.TclError:
                pass
        try:
            self.top.destroy()
        except tk.TclError:
            pass

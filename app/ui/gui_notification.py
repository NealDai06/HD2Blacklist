# -*- coding: utf-8 -*-
"""gui_notification.py —— 通知设置（文案 / 图片 / 外观 / 音效）。

- 左侧分页编辑，右侧实时预览（PIL 渲染 → ImageTk 显示，不弹真实窗口）
- [实时预览] 真的弹一次无焦点 Overlay
- [测试通知] 用假数据完整走一遍（Overlay + 音效）
- [恢复默认] 删除 data/notification.json
- [保存] 深合并写入 data/notification.json，无需重启即生效

所有 tkinter 操作都在主线程。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from app.config import (DEFAULT_BODY_FONT_SIZE, DEFAULT_TITLE_FONT_SIZE,
                    FONT_SIZE_MAX, FONT_SIZE_MIN, NOTIFICATION_PLACEHOLDERS,
                    NOTIFICATION_POSITIONS, get_logger)
from app.ui import theme
from app.notify.notifier import clamp_font_size, hex_to_rgb

_POSITION_LABELS = {
    "top_left": "左上", "top_center": "顶部居中", "top_right": "右上",
    "bottom_left": "左下", "bottom_center": "底部居中",
    "bottom_right": "右下", "center": "屏幕正中",
}


class NotificationSettingsDialog:
    """通知设置对话框。"""

    def __init__(self, master, notification_config, notifier, on_saved=None):
        self.master = master
        self.cfg = notification_config
        self.notifier = notifier
        self.on_saved = on_saved
        self.log = get_logger("gui_notify")

        self.preview_photo = None
        self._preview_job = None
        self._loading = True

        self.top = tk.Toplevel(master)
        self.top.title("通知设置")
        self.top.transient(master)
        self.top.configure(bg=theme.PALETTE["bg"])
        theme.apply_window_icon(self.top)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.resizable(False, False)

        self._init_vars()
        self._build()
        self._load_from_config()
        self._loading = False
        self._schedule_preview()
        self._center()

    # ---------------------------------------------------------------- 变量
    def _init_vars(self):
        d = self.cfg.get()
        self.title_var = tk.StringVar()
        self.show_vars = {k: tk.BooleanVar() for k in
                          ("note", "tk_count", "match_score", "time",
                           "source", "last_seen")}
        self.image_mode_var = tk.StringVar()
        self.image_path_var = tk.StringVar()
        self.image_w_var = tk.IntVar()
        self.image_h_var = tk.IntVar()

        self.bg_var = tk.StringVar()
        self.fg_var = tk.StringVar()
        self.title_color_var = tk.StringVar()
        self.opacity_var = tk.DoubleVar()
        self.width_var = tk.IntVar()
        self.height_var = tk.IntVar()
        self.title_size_var = tk.IntVar()
        self.body_size_var = tk.IntVar()
        self.position_var = tk.StringVar()
        self.monitor_var = tk.IntVar()
        self.duration_var = tk.DoubleVar()

        self.sound_enabled_var = tk.BooleanVar()
        self.sound_mode_var = tk.StringVar()
        self.sound_path_var = tk.StringVar()
        self.beep_freq_var = tk.IntVar()
        self.beep_dur_var = tk.IntVar()

        self.status_var = tk.StringVar(value="")

        # 任意变量变化 → 刷新预览
        for var in (self.title_var, self.image_mode_var, self.image_path_var,
                    self.image_w_var, self.image_h_var, self.bg_var,
                    self.fg_var, self.title_color_var, self.opacity_var,
                    self.width_var, self.height_var,
                    self.title_size_var, self.body_size_var,
                    self.position_var, self.monitor_var, self.duration_var,
                    self.sound_enabled_var, self.sound_mode_var,
                    self.sound_path_var, self.beep_freq_var,
                    self.beep_dur_var, *self.show_vars.values()):
            var.trace_add("write", lambda *_: self._schedule_preview())

    # ---------------------------------------------------------------- 构建
    def _build(self):
        body = ttk.Frame(self.top)
        body.pack(fill="both", expand=True, padx=10, pady=8)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(body)
        right.pack(side="left", fill="y", padx=(12, 0))

        nb = ttk.Notebook(left)
        nb.pack(fill="both", expand=True)
        self._build_text_tab(nb)
        self._build_image_tab(nb)
        self._build_appearance_tab(nb)
        self._build_sound_tab(nb)

        self._build_preview(right)
        self._build_buttons()

    # ---- 文案 ----
    def _build_text_tab(self, nb):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="文案")

        ttk.Label(tab, text="标题模板：").grid(row=0, column=0, sticky="nw")
        ttk.Entry(tab, textvariable=self.title_var, width=44).grid(
            row=0, column=1, sticky="we", pady=2)

        ttk.Label(tab, text="正文模板：").grid(row=1, column=0, sticky="nw")
        self.body_text = tk.Text(tab, width=44, height=5, wrap="word")
        theme.style_text(self.body_text, mono=True)
        self.body_text.grid(row=1, column=1, sticky="we", pady=2)

        ttk.Label(tab, text="可用占位符：", style="Muted.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(tab, text="  ".join(NOTIFICATION_PLACEHOLDERS),
                  foreground="#0a6", font=("Consolas", 9),
                  wraplength=380, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w")

        box = ttk.LabelFrame(tab, text="字段显示开关")
        box.grid(row=4, column=0, columnspan=2, sticky="we", pady=(10, 0))
        names = {"note": "备注", "tk_count": "TK次数",
                 "match_score": "匹配度", "time": "时间",
                 "source": "来源", "last_seen": "最后遇见"}
        for i, (k, label) in enumerate(names.items()):
            ttk.Checkbutton(box, text=label, variable=self.show_vars[k]).grid(
                row=i // 3, column=i % 3, sticky="w", padx=8, pady=2)

        ttk.Label(tab, text="（关闭某字段后，正文模板里只包含该字段的整行会被自动去掉）",
                  style="Muted.TLabel", wraplength=380, justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

    # ---- 图片 ----
    def _build_image_tab(self, nb):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="图片")

        for i, (val, label) in enumerate((("default", "使用默认提示图"),
                                          ("custom", "使用自定义图片"),
                                          ("none", "不显示图片"))):
            ttk.Radiobutton(tab, text=label, value=val,
                            variable=self.image_mode_var).grid(
                row=i, column=0, columnspan=3, sticky="w", pady=2)

        ttk.Label(tab, text="图片路径：").grid(row=3, column=0, sticky="w",
                                             pady=(10, 0))
        ttk.Entry(tab, textvariable=self.image_path_var, width=32).grid(
            row=3, column=1, sticky="we", pady=(10, 0))
        ttk.Button(tab, text="浏览…", command=self._pick_image).grid(
            row=3, column=2, padx=4, pady=(10, 0))

        size = ttk.Frame(tab)
        size.grid(row=4, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Label(size, text="显示尺寸：").pack(side="left")
        ttk.Spinbox(size, from_=8, to=256, width=5,
                    textvariable=self.image_w_var).pack(side="left")
        ttk.Label(size, text=" × ").pack(side="left")
        ttk.Spinbox(size, from_=8, to=256, width=5,
                    textvariable=self.image_h_var).pack(side="left")
        ttk.Label(size, text=" 像素").pack(side="left")

        ttk.Label(tab, text="支持 PNG / JPG / BMP / GIF。自定义图片会被缓存，"
                            "修改文件后自动重新加载。",
                  style="Muted.TLabel", wraplength=380, justify="left").grid(
            row=5, column=0, columnspan=3, sticky="w")

    # ---- 外观 ----
    def _build_appearance_tab(self, nb):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="外观")

        rows = (("背景色：", self.bg_var),
                ("文字色：", self.fg_var),
                ("标题色：", self.title_color_var))
        for i, (label, var) in enumerate(rows):
            ttk.Label(tab, text=label).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(tab, textvariable=var, width=12).grid(
                row=i, column=1, sticky="w", pady=2)
            sw = tk.Label(tab, width=3, relief="solid", bd=1)
            sw.grid(row=i, column=2, padx=6)
            self._bind_swatch(sw, var)
            ttk.Button(tab, text="选色…",
                       command=lambda v=var: self._pick_color(v)).grid(
                row=i, column=3, padx=4)

        ttk.Label(tab, text="透明度：").grid(row=3, column=0, sticky="w",
                                            pady=(10, 0))
        ttk.Scale(tab, from_=0.1, to=1.0, variable=self.opacity_var,
                  orient="horizontal", length=180).grid(
            row=3, column=1, columnspan=2, sticky="we", pady=(10, 0))
        ttk.Label(tab, textvariable=self.opacity_var, width=6).grid(
            row=3, column=3, sticky="w", pady=(10, 0))

        # ---- 字号（本轮新增：消息文字大小可编辑）----
        fonts = ttk.LabelFrame(tab, text="文字大小")
        fonts.grid(row=4, column=0, columnspan=4, sticky="we", pady=(10, 4))

        line1 = ttk.Frame(fonts)
        line1.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(line1, text="标题字号：").pack(side="left")
        ttk.Spinbox(line1, from_=FONT_SIZE_MIN, to=FONT_SIZE_MAX, width=5,
                    textvariable=self.title_size_var).pack(side="left")
        ttk.Label(line1, text="   正文字号：").pack(side="left")
        ttk.Spinbox(line1, from_=FONT_SIZE_MIN, to=FONT_SIZE_MAX, width=5,
                    textvariable=self.body_size_var).pack(side="left")
        ttk.Label(line1, text=" 像素").pack(side="left")

        line2 = ttk.Frame(fonts)
        line2.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(line2, text="快捷：").pack(side="left")
        for label, t_size, b_size in (("小", 13, 11), ("标准", 16, 13),
                                      ("大", 22, 18), ("特大", 30, 24)):
            ttk.Button(line2, text=label, width=5,
                       command=lambda t=t_size, b=b_size:
                       self._apply_font_preset(t, b)).pack(side="left", padx=2)
        ttk.Label(line2, text="  拖动或输入数字即可，右侧实时预览会立刻变化",
                  style="Muted.TLabel").pack(side="left")

        geo = ttk.Frame(tab)
        geo.grid(row=5, column=0, columnspan=4, sticky="w", pady=8)
        ttk.Label(geo, text="尺寸：").pack(side="left")
        ttk.Spinbox(geo, from_=160, to=1600, width=6,
                    textvariable=self.width_var).pack(side="left")
        ttk.Label(geo, text=" × ").pack(side="left")
        ttk.Spinbox(geo, from_=60, to=800, width=6,
                    textvariable=self.height_var).pack(side="left")
        ttk.Label(geo, text=" 像素（正文过长时高度会自动增高）").pack(side="left")

        pos = ttk.Frame(tab)
        pos.grid(row=6, column=0, columnspan=4, sticky="w", pady=4)
        ttk.Label(pos, text="位置：").pack(side="left")
        self.pos_combo = ttk.Combobox(
            pos, state="readonly", width=12,
            values=[_POSITION_LABELS[p] for p in NOTIFICATION_POSITIONS])
        self.pos_combo.pack(side="left", padx=4)
        ttk.Label(pos, text="显示器：").pack(side="left", padx=(14, 0))
        self.monitor_combo = ttk.Combobox(
            pos, state="readonly", width=18,
            values=["整个虚拟桌面（含副屏）", "指定显示器…"])
        self.monitor_combo.pack(side="left", padx=4)
        ttk.Label(pos, text="序号：").pack(side="left")
        ttk.Spinbox(pos, from_=0, to=8, width=4,
                    textvariable=self.monitor_var).pack(side="left", padx=4)
        ttk.Label(pos, text="(0=整个虚拟桌面)").pack(side="left")

        ttk.Label(tab, text="显示时长：").grid(row=7, column=0, sticky="w",
                                             pady=(8, 0))
        ttk.Spinbox(tab, from_=1.0, to=30.0, increment=0.5, width=6,
                    textvariable=self.duration_var).grid(
            row=7, column=1, sticky="w", pady=(8, 0))
        ttk.Label(tab, text="秒").grid(row=7, column=2, sticky="w",
                                      pady=(8, 0))

    def _apply_font_preset(self, title_size: int, body_size: int):
        """一键套用预设字号。"""
        self.title_size_var.set(int(title_size))
        self.body_size_var.set(int(body_size))

    def _bind_swatch(self, widget, var):
        def update(*_):
            try:
                r, g, b = hex_to_rgb(var.get(), (255, 0, 255))
                widget.configure(bg=f"#{r:02x}{g:02x}{b:02x}")
            except Exception:                            # noqa: BLE001
                widget.configure(bg="#ff00ff")
        var.trace_add("write", update)
        update()

    # ---- 音效 ----
    def _build_sound_tab(self, nb):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="音效")

        ttk.Checkbutton(tab, text="启用提示音",
                        variable=self.sound_enabled_var).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=2)

        ttk.Radiobutton(tab, text="系统 Beep", value="beep",
                        variable=self.sound_mode_var).grid(
            row=1, column=0, sticky="w", pady=2)
        ttk.Radiobutton(tab, text="自定义 WAV", value="wav",
                        variable=self.sound_mode_var).grid(
            row=2, column=0, sticky="w", pady=2)
        ttk.Radiobutton(tab, text="静音", value="none",
                        variable=self.sound_mode_var).grid(
            row=3, column=0, sticky="w", pady=2)

        beep = ttk.Frame(tab)
        beep.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 2))
        ttk.Label(beep, text="频率(Hz)：").pack(side="left")
        ttk.Spinbox(beep, from_=37, to=32767, increment=50, width=7,
                    textvariable=self.beep_freq_var).pack(side="left")
        ttk.Label(beep, text="  时长(ms)：").pack(side="left")
        ttk.Spinbox(beep, from_=30, to=2000, increment=10, width=7,
                    textvariable=self.beep_dur_var).pack(side="left")

        ttk.Label(tab, text="WAV 文件：").grid(row=5, column=0, sticky="w",
                                              pady=(8, 0))
        ttk.Entry(tab, textvariable=self.sound_path_var, width=30).grid(
            row=5, column=1, sticky="we", pady=(8, 0))
        ttk.Button(tab, text="浏览…", command=self._pick_wav).grid(
            row=5, column=2, padx=4, pady=(8, 0))
        ttk.Button(tab, text="试听", command=self._test_sound).grid(
            row=6, column=1, sticky="w", pady=6)

        ttk.Label(tab, text="WAV 建议使用 16bit PCM、44.1kHz 单声道/立体声。",
                  style="Muted.TLabel").grid(row=7, column=0, columnspan=3,
                                             sticky="w", pady=(8, 0))

    # ---- 预览 ----
    def _build_preview(self, parent):
        box = ttk.LabelFrame(parent, text="实时预览")
        box.pack(fill="both", expand=True)
        self.preview_label = tk.Label(box, width=380, height=120,
                                      bg=theme.PALETTE["bg"], bd=0)
        self.preview_label.pack(padx=8, pady=8)
        ttk.Label(box, text="预览使用模拟数据（示例玩家 / 恶意TK）",
                  style="Muted.TLabel").pack(pady=(0, 8))

    def _build_buttons(self):
        bar = ttk.Frame(self.top)
        bar.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(bar, textvariable=self.status_var,
                  foreground="#0a6").pack(side="left")
        ttk.Button(bar, text="取消", command=self.close).pack(side="right",
                                                             padx=3)
        ttk.Button(bar, text="保存", command=self.save).pack(side="right",
                                                            padx=3)
        ttk.Button(bar, text="恢复默认", command=self.reset).pack(side="right",
                                                                padx=3)
        ttk.Button(bar, text="测试通知", command=self.test_alert).pack(
            side="right", padx=3)
        ttk.Button(bar, text="实时预览", command=self.show_overlay_preview
                   ).pack(side="right", padx=3)

    def _center(self):
        self.top.update_idletasks()
        w, h = self.top.winfo_width(), self.top.winfo_height()
        x = self.master.winfo_rootx() + (self.master.winfo_width() - w) // 2
        y = self.master.winfo_rooty() + (self.master.winfo_height() - h) // 3
        self.top.geometry(f"+{max(0, x)}+{max(0, y)}")

    # ------------------------------------------------------------ 读写配置
    def _load_from_config(self):
        d = self.cfg.get()
        self.title_var.set(d.get("title_template", ""))
        self.body_text.delete("1.0", "end")
        self.body_text.insert("1.0", d.get("body_template", ""))

        show = d.get("show_fields") or {}
        for k, var in self.show_vars.items():
            var.set(bool(show.get(k, False)))

        self.image_mode_var.set(d.get("image_mode", "default"))
        self.image_path_var.set(d.get("image_path", "") or "")
        size = d.get("image_size") or [64, 64]
        self.image_w_var.set(int(size[0]))
        self.image_h_var.set(int(size[1]))

        ap = d.get("appearance") or {}
        self.bg_var.set(ap.get("background_color", "#2b2b2b"))
        self.fg_var.set(ap.get("text_color", "#ffffff"))
        self.title_color_var.set(ap.get("title_color", "#ff5555"))
        self.opacity_var.set(float(ap.get("opacity", 0.85)))
        self.width_var.set(int(ap.get("width", 360)))
        self.height_var.set(int(ap.get("height", 100)))
        self.title_size_var.set(clamp_font_size(
            ap.get("title_font_size"), DEFAULT_TITLE_FONT_SIZE))
        self.body_size_var.set(clamp_font_size(
            ap.get("body_font_size"), DEFAULT_BODY_FONT_SIZE))
        pos = str(ap.get("position", "top_right"))
        self.pos_combo.set(_POSITION_LABELS.get(pos, _POSITION_LABELS["top_right"]))
        self.monitor_var.set(int(ap.get("monitor", 0) or 0))
        self.duration_var.set(float((d.get("timing") or {}).get("duration", 4.0)))

        snd = d.get("sound") or {}
        self.sound_enabled_var.set(bool(snd.get("enabled", True)))
        self.sound_mode_var.set(snd.get("mode", "beep"))
        self.sound_path_var.set(snd.get("path", "") or "")
        self.beep_freq_var.set(int(snd.get("beep_freq", 1200)))
        self.beep_dur_var.set(int(snd.get("beep_duration", 120)))

    def _collect(self) -> dict:
        pos = self.pos_combo.get()
        position = "top_right"
        for key, label in _POSITION_LABELS.items():
            if label == pos:
                position = key
                break

        def _safe_int(var, default):
            try:
                return int(var.get())
            except Exception:                            # noqa: BLE001
                return default

        def _safe_float(var, default):
            try:
                return float(var.get())
            except Exception:                            # noqa: BLE001
                return default

        return {
            "version": 1,
            "title_template": self.title_var.get(),
            "body_template": self.body_text.get("1.0", "end-1c"),
            "show_fields": {k: bool(v.get()) for k, v in self.show_vars.items()},
            "image_mode": self.image_mode_var.get() or "default",
            "image_path": self.image_path_var.get().strip(),
            "image_size": [max(8, _safe_int(self.image_w_var, 64)),
                           max(8, _safe_int(self.image_h_var, 64))],
            "appearance": {
                "background_color": self.bg_var.get().strip() or "#2b2b2b",
                "text_color": self.fg_var.get().strip() or "#ffffff",
                "title_color": self.title_color_var.get().strip() or "#ff5555",
                "opacity": min(1.0, max(0.1, _safe_float(self.opacity_var, 0.85))),
                "width": max(160, _safe_int(self.width_var, 360)),
                "height": max(60, _safe_int(self.height_var, 100)),
                "title_font_size": clamp_font_size(
                    _safe_int(self.title_size_var, DEFAULT_TITLE_FONT_SIZE),
                    DEFAULT_TITLE_FONT_SIZE),
                "body_font_size": clamp_font_size(
                    _safe_int(self.body_size_var, DEFAULT_BODY_FONT_SIZE),
                    DEFAULT_BODY_FONT_SIZE),
                "position": position,
                "monitor": max(0, _safe_int(self.monitor_var, 0)),
            },
            "timing": {"duration": min(60.0, max(0.5,
                                                 _safe_float(self.duration_var, 4.0)))},
            "sound": {
                "enabled": bool(self.sound_enabled_var.get()),
                "mode": self.sound_mode_var.get() or "beep",
                "path": self.sound_path_var.get().strip(),
                "beep_freq": min(32767, max(37, _safe_int(self.beep_freq_var, 1200))),
                "beep_duration": min(2000, max(30, _safe_int(self.beep_dur_var, 120))),
            },
        }

    # ---------------------------------------------------------------- 预览
    def _schedule_preview(self):
        if self._loading:
            return
        if self._preview_job is not None:
            try:
                self.top.after_cancel(self._preview_job)
            except Exception:                            # noqa: BLE001
                pass
        self._preview_job = self.top.after(200, self._refresh_preview)

    def _refresh_preview(self):
        self._preview_job = None
        if not self.top.winfo_exists():
            return
        try:
            from PIL import ImageTk
            cfg = self._collect()
            img = self.notifier.render_preview_image(cfg)
            max_w, max_h = 380, 220
            if img.width > max_w or img.height > max_h:
                from PIL import Image
                scale = min(max_w / img.width, max_h / img.height)
                img = img.resize((int(img.width * scale),
                                  int(img.height * scale)), Image.LANCZOS)
            self.preview_photo = ImageTk.PhotoImage(img)
            self.preview_label.configure(image=self.preview_photo, width=0,
                                         height=0)
        except Exception as e:                           # noqa: BLE001
            self.status_var.set(f"预览渲染失败：{e}")

    def show_overlay_preview(self):
        try:
            ok = self.notifier.preview(self._collect())
            self.status_var.set("已发送实时预览" if ok
                                else "Overlay 不可用，无法预览")
        except Exception as e:                           # noqa: BLE001
            messagebox.showerror("预览失败", str(e), parent=self.top)

    def test_alert(self):
        try:
            self.notifier.preview(self._collect(), score=92.0, source="test")
            self.notifier.play_sound(self._collect().get("sound"))
            self.status_var.set("已发送测试通知（含音效）")
        except Exception as e:                           # noqa: BLE001
            messagebox.showerror("测试失败", str(e), parent=self.top)

    def _test_sound(self):
        try:
            self.notifier.play_sound(self._collect().get("sound"))
        except Exception as e:                           # noqa: BLE001
            messagebox.showerror("试听失败", str(e), parent=self.top)

    # ---------------------------------------------------------------- 动作
    def _pick_image(self):
        path = filedialog.askopenfilename(
            parent=self.top, title="选择提示图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.gif"),
                       ("所有文件", "*.*")])
        if path:
            self.image_path_var.set(path)
            self.image_mode_var.set("custom")

    def _pick_wav(self):
        path = filedialog.askopenfilename(
            parent=self.top, title="选择音效文件",
            filetypes=[("WAV 音频", "*.wav"), ("所有文件", "*.*")])
        if path:
            self.sound_path_var.set(path)
            self.sound_mode_var.set("wav")

    def _pick_color(self, var):
        cur = var.get() or "#ffffff"
        rgb, _ = colorchooser.askcolor(color=cur, parent=self.top,
                                       title="选择颜色")
        if rgb:
            var.set("#%02x%02x%02x" % tuple(int(c) for c in rgb))

    def save(self):
        try:
            data = self._collect()
            self.cfg.save(data)
            self.status_var.set("已保存到 data/notification.json（立即生效）")
            if self.on_saved:
                self.on_saved()
        except Exception as e:                           # noqa: BLE001
            messagebox.showerror("保存失败", str(e), parent=self.top)

    def reset(self):
        if not messagebox.askyesno("确认", "恢复全部通知设置为默认值？\n"
                                          "（会删除 data/notification.json）",
                                   parent=self.top):
            return
        try:
            self.cfg.reset()
            self._loading = True
            self._load_from_config()
            self._loading = False
            self._refresh_preview()
            self.status_var.set("已恢复默认设置")
            if self.on_saved:
                self.on_saved()
        except Exception as e:                           # noqa: BLE001
            messagebox.showerror("恢复失败", str(e), parent=self.top)

    def close(self):
        # 取消挂起的预览任务：否则窗口销毁后 Tcl 还会去调用已消失的回调，
        # 控制台会刷 `invalid command name "..."`。
        if self._preview_job is not None:
            try:
                self.top.after_cancel(self._preview_job)
            except Exception:                            # noqa: BLE001
                pass
            self._preview_job = None
        try:
            self.top.destroy()
        except tk.TclError:
            pass

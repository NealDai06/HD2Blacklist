# -*- coding: utf-8 -*-
"""gui.py —— 主界面。

红线：**所有 tkinter 操作都在主线程**。
后台线程（扫描会话 / 进程监控 / ESC / 托盘）只能往两个 queue 里塞东西：
    * _update_queue —— 命中数据（enqueue_encounter_update）
    * _command_queue —— 需要主线程执行的可调用对象（post）
主线程用 root.after(GUI_QUEUE_POLL_MS) 周期性排空它们。
"""
from __future__ import annotations

import csv
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app import config
from app.ui import theme
from app.config import GUI_QUEUE_POLL_MS, get_logger

COLUMNS = ("player_id", "player_name", "note", "tk_count",
           "encounter_count", "created_at", "last_seen")
HEADERS = {
    "player_id": "玩家ID", "player_name": "名称", "note": "备注",
    "tk_count": "TK次数", "encounter_count": "遇到次数",
    "created_at": "添加时间", "last_seen": "最后遇见",
}
WIDTHS = {"player_id": 170, "player_name": 150, "note": 240, "tk_count": 70,
          "encounter_count": 80, "created_at": 145, "last_seen": 145}

SORT_OPTIONS = (("last_seen", "按最后遇见"), ("encounter_count", "按遇到次数"),
                ("created_at", "按添加时间"), ("tk_count", "按TK次数"))


# ==========================================================================
class EntryDialog:
    """新增 / 编辑黑名单条目。"""

    def __init__(self, master, title="添加黑名单", entry=None):
        self.master = master
        self.result = None

        self.top = tk.Toplevel(master)
        self.top.title(title)
        self.top.transient(master)
        self.top.resizable(False, False)
        self.top.configure(bg=theme.PALETTE["bg"])
        theme.apply_window_icon(self.top)
        self.top.grab_set()

        self.vars = {
            "player_id": tk.StringVar(value=(entry or {}).get("player_id") or ""),
            "player_name": tk.StringVar(value=(entry or {}).get("player_name") or ""),
            "tk_count": tk.StringVar(value=str((entry or {}).get("tk_count") or 0)),
            "evidence_path": tk.StringVar(
                value=(entry or {}).get("evidence_path") or ""),
        }

        frm = ttk.Frame(self.top, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="玩家ID：").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.vars["player_id"], width=38).grid(
            row=0, column=1, columnspan=2, sticky="we", pady=4)

        ttk.Label(frm, text="玩家名称：").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.vars["player_name"], width=38).grid(
            row=1, column=1, columnspan=2, sticky="we", pady=4)

        ttk.Label(frm, text="TK次数：").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(frm, from_=0, to=9999, width=8,
                    textvariable=self.vars["tk_count"]).grid(
            row=2, column=1, sticky="w", pady=4)

        ttk.Label(frm, text="备注描述：").grid(row=3, column=0, sticky="nw", pady=4)
        self.note_text = tk.Text(frm, width=40, height=5, wrap="word")
        theme.style_text(self.note_text)
        self.note_text.grid(row=3, column=1, columnspan=2, sticky="we", pady=4)
        if entry and entry.get("note"):
            self.note_text.insert("1.0", entry["note"])

        ttk.Label(frm, text="证据截图：").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.vars["evidence_path"]).grid(
            row=4, column=1, sticky="we", pady=4)
        ttk.Button(frm, text="浏览…", command=self._pick).grid(
            row=4, column=2, padx=4)

        ttk.Label(frm, text="玩家ID 与 名称 至少填一项；(ID, 名称) 组合不可重复。",
                  foreground="#888888").grid(row=5, column=0, columnspan=3,
                                             sticky="w", pady=(6, 0))

        bar = ttk.Frame(frm)
        bar.grid(row=6, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(bar, text="取消", command=self._cancel).pack(side="right",
                                                               padx=4)
        ttk.Button(bar, text="确定", command=self._ok).pack(side="right")

        self.top.bind("<Return>", lambda e: self._ok())
        self.top.bind("<Escape>", lambda e: self._cancel())
        self.top.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.top.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.top.winfo_height()) // 3
        self.top.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.top.wait_window()

    def _pick(self):
        path = filedialog.askopenfilename(
            parent=self.top, title="选择证据截图",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp"), ("所有文件", "*.*")])
        if path:
            self.vars["evidence_path"].set(path)

    def _ok(self):
        data = {
            "player_id": self.vars["player_id"].get().strip(),
            "player_name": self.vars["player_name"].get().strip(),
            "note": self.note_text.get("1.0", "end-1c").strip(),
            "evidence_path": self.vars["evidence_path"].get().strip(),
        }
        try:
            data["tk_count"] = max(0, int(self.vars["tk_count"].get() or 0))
        except ValueError:
            messagebox.showwarning("输入有误", "TK次数必须是整数", parent=self.top)
            return
        if not data["player_id"] and not data["player_name"]:
            messagebox.showwarning("输入有误", "玩家ID 与 名称 至少填一项",
                                   parent=self.top)
            return
        self.result = data
        self.top.destroy()

    def _cancel(self):
        self.result = None
        self.top.destroy()


# ==========================================================================
# 名单导入导出（模块级函数，方便单独测试）
# ==========================================================================
#: 导出/导入字段顺序（与 database.export_all 一致）
IO_FIELDS = ("player_id", "player_name", "note", "tk_count",
             "encounter_count", "created_at", "last_seen")


def write_csv(path: str, rows) -> None:
    """写 CSV。使用 utf-8-sig（带 BOM），Excel 直接双击不乱码。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(IO_FIELDS),
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k))
                        for k in IO_FIELDS})


def write_json(path: str, rows) -> None:
    """写 JSON（结构化，便于人工编辑与多机同步）。"""
    payload = {
        "version": 1,
        "exported_count": len(rows),
        "fields": list(IO_FIELDS),
        "entries": [{k: r.get(k) for k in IO_FIELDS} for r in rows],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def parse_csv(path: str) -> list:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def parse_json(path: str) -> list:
    # utf-8-sig：用户用记事本另存过的名单（带 BOM）也能正常导入
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("entries", "blacklist", "items", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    raise ValueError("JSON 格式不正确：需要是数组，或包含 entries 数组的对象")


def read_entries(path: str) -> list:
    """按扩展名选择解析器；.json 走 JSON，其余按 CSV 处理。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        return parse_json(path)
    return parse_csv(path)


# ==========================================================================
class ImportStrategyDialog:
    """导入冲突策略选择对话框（模态）。"""

    STRATEGIES = (
        ("skip", "跳过已存在的条目（推荐）",
         "只新增文件里有、数据库里没有的；(player_id, 名称) 相同就跳过"),
        ("update_note", "更新备注与 TK 次数",
         "保留本机已累积的「遇到次数」与「最后遇见」，只覆盖备注 / TK次数"),
        ("overwrite", "完全覆盖",
         "连「遇到次数」「最后遇见」也一起用文件里的值覆盖"),
    )

    def __init__(self, master, filename: str = ""):
        self.result = None

        self.top = tk.Toplevel(master)
        self.top.title("导入名单")
        self.top.transient(master)
        self.top.resizable(False, False)
        self.top.configure(bg=theme.PALETTE["bg"])
        theme.apply_window_icon(self.top)
        self.top.grab_set()

        frm = ttk.Frame(self.top, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=f"文件：{filename}", style="Muted.TLabel",
                  wraplength=420, justify="left").pack(anchor="w")
        ttk.Label(frm, text="遇到 (玩家ID, 名称) 相同的条目时：",
                  font=theme.FONT_BOLD).pack(anchor="w", pady=(10, 4))

        self.var = tk.StringVar(value="skip")
        for i, (value, label, desc) in enumerate(self.STRATEGIES):
            row = ttk.Frame(frm)
            row.pack(fill="x", anchor="w", pady=2)
            ttk.Radiobutton(row, text=label, value=value,
                            variable=self.var).pack(anchor="w")
            ttk.Label(row, text="      " + desc, style="Muted.TLabel",
                      wraplength=400, justify="left").pack(anchor="w")

        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text="取消", command=self._cancel).pack(side="right",
                                                               padx=4)
        ttk.Button(bar, text="开始导入", command=self._ok).pack(side="right")

        self.top.bind("<Escape>", lambda e: self._cancel())
        self.top.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.top.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.top.winfo_height()) // 3
        self.top.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.top.wait_window()

    def _ok(self):
        self.result = self.var.get()
        self.top.destroy()

    def _cancel(self):
        self.result = None
        self.top.destroy()


# ==========================================================================
class BlacklistGUI:
    """主窗口。"""

    def __init__(self, db, notification_config, region_config,
                 matcher=None, scheduler=None, capture=None, notifier=None,
                 chat_scanner=None, hotkey_config=None, on_quit=None,
                 on_pause=None, on_chat_hotkey=None, app=None):
        self.db = db
        self.notification_config = notification_config
        self.region_config = region_config
        self.matcher = matcher
        self.scheduler = scheduler
        self.capture = capture
        self.notifier = notifier
        self.chat_scanner = chat_scanner
        self.hotkey_config = hotkey_config
        self.on_quit = on_quit
        self.on_pause = on_pause
        self.on_chat_hotkey = on_chat_hotkey
        self.app = app
        self.log = get_logger("gui")

        # 跨线程通信
        self._update_queue = queue.Queue()
        self._command_queue = queue.Queue()

        self.session_hit_count = 0
        self.paused = False
        self.sort_key = "last_seen"
        self.sort_desc = True
        self.search_text = ""
        self._tray = None
        self._tray_thread = None
        self._closing = False
        self._flash_jobs = {}
        self._drain_job = None
        self._flash_job = None

        self.root = tk.Tk()
        self.root.title(config.WINDOW_TITLE)
        self.root.geometry("1120x660")
        # 最小宽度 960：保证三行工具栏 + 底部汇总栏都不会被挤掉
        self.root.minsize(960, 520)

        theme.apply_theme(self.root)
        theme.apply_window_icon(self.root)

        self._build_header()
        self._build_menu()
        self._build_toolbar()
        self._build_tree()
        self._build_statusbar()

        self._load_data()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._drain_job = self.root.after(GUI_QUEUE_POLL_MS, self._drain_queues)

    # ------------------------------------------------------------ 顶部标题栏
    def _build_header(self):
        """一条细细的标题带：应用图标 + 名称 + 版本 + 监控状态。"""
        p = theme.PALETTE
        bar = ttk.Frame(self.root, style="Panel.TFrame", padding=(10, 6))
        bar.pack(fill="x")

        # 应用图标
        try:
            from PIL import ImageTk
            icon = theme.load_icon_image(26)
            if icon is not None:
                self._header_icon = ImageTk.PhotoImage(icon)
                tk.Label(bar, image=self._header_icon, bg=p["panel"],
                         bd=0).pack(side="left", padx=(0, 8))
        except Exception as e:                           # noqa: BLE001
            self.log.debug("标题栏图标加载失败: %s", e)

        ttk.Label(bar, text=config.APP_NAME, style="Title.TLabel").pack(
            side="left")
        ttk.Label(bar, text=f"v{config.VERSION}", style="Version.TLabel").pack(
            side="left", padx=(8, 0), pady=(6, 0))

        self.monitor_var = tk.StringVar(value="● 监控中")
        self.monitor_label = ttk.Label(bar, textvariable=self.monitor_var,
                                       style="Hit.TLabel")
        self.monitor_label.pack(side="right")

    # ---------------------------------------------------------------- 菜单
    def _build_menu(self):
        menubar = theme.make_menu(self.root)

        m_file = theme.make_menu(menubar)
        m_file.add_command(label="添加条目", command=self.add_entry)
        m_file.add_command(label="导出列表…", command=self.export_list)
        m_file.add_separator()
        m_file.add_command(label="最小化到托盘", command=self.hide_to_tray)
        m_file.add_command(label="退出", command=self.quit_app)
        menubar.add_cascade(label="文件", menu=m_file)

        m_set = theme.make_menu(menubar)
        m_set.add_command(label="校准区域…", command=self.open_calibrator)
        m_set.add_command(label="恢复全部默认区域", command=self.reset_regions)
        m_set.add_command(label="通知设置…", command=self.open_notification_settings)
        m_set.add_separator()

        # 聊天框扫描快捷键子菜单
        m_chat = theme.make_menu(m_set)
        m_chat.add_command(label="立即扫描聊天框", command=self.scan_chat_now)
        m_chat.add_separator()
        self.chat_hotkey_var = tk.BooleanVar(
            value=bool(self.hotkey_config.enabled)
            if self.hotkey_config is not None else False)
        m_chat.add_checkbutton(label="启用热键扫描",
                               variable=self.chat_hotkey_var,
                               command=self._toggle_chat_hotkey)
        self._hotkey_menu_index = m_chat.index("end")
        m_chat.add_command(label="自定义快捷键…",
                           command=self.open_hotkey_settings)
        m_chat.add_separator()
        m_chat.add_command(label="清空命中去重缓存",
                           command=self._clear_dedup_cache)
        m_set.add_cascade(label="聊天框扫描快捷键", menu=m_chat)
        self.chat_menu = m_chat          # 便于测试与后续扩展
        self._sync_hotkey_menu_label()

        m_set.add_separator()
        self.pause_var = tk.BooleanVar(value=False)
        m_set.add_checkbutton(label="暂停监控", variable=self.pause_var,
                              command=self.toggle_pause)
        m_set.add_separator()
        m_set.add_command(label="打开数据目录", command=self.open_data_dir)
        m_set.add_command(label="打开日志", command=self.open_log)
        menubar.add_cascade(label="设置", menu=m_set)

        m_help = theme.make_menu(menubar)
        m_help.add_command(label="使用说明", command=self.show_help)
        m_help.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=m_help)

        self.root.config(menu=menubar)

    # ---------------------------------------------------------------- 工具栏
    def _build_toolbar(self):
        """三行工具栏。

        ttk 按钮在 clam 主题下比较宽，挤在一行会被裁掉，所以按功能分行：
            第一行：增删改 + 搜索
            第二行：排序 + 升降序 ............ 扫描聊天框 / 导入 / 导出
            第三行：......................... 校准 / 恢复默认 / 通知 / 托盘
        窗口最小宽度 960px，保证三行都不会被挤掉。
        """
        outer = ttk.Frame(self.root, style="Panel.TFrame",
                          padding=(10, 8, 10, 6))
        outer.pack(fill="x")

        # ---- 第一行：黑名单增删改查 ----
        bar = ttk.Frame(outer, style="Panel.TFrame")
        bar.pack(fill="x")

        ttk.Button(bar, text="添加", command=self.add_entry).pack(side="left")
        ttk.Button(bar, text="编辑", command=self.edit_selected).pack(
            side="left", padx=3)
        ttk.Button(bar, text="删除", style="Danger.TButton",
                   command=self.delete_selected).pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                   padx=10)
        ttk.Label(bar, text="搜索：", style="PanelMuted.TLabel").pack(
            side="left")
        self.search_var = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.search_var, width=14)
        ent.pack(side="left", padx=4)
        ent.bind("<Return>", lambda e: self.do_search())
        ttk.Button(bar, text="搜索", command=self.do_search).pack(side="left")
        ttk.Button(bar, text="清空", command=self.clear_search).pack(
            side="left", padx=3)

        # ---- 第二行：排序 + 聊天框按需扫描 + 导入导出 ----
        bar2 = ttk.Frame(outer, style="Panel.TFrame")
        bar2.pack(fill="x", pady=(6, 0))

        ttk.Button(bar2, text="导出", command=self.export_list).pack(
            side="right")
        ttk.Button(bar2, text="导入", command=self.import_list).pack(
            side="right", padx=3)
        self.scan_chat_btn = ttk.Button(bar2, text="扫描聊天框",
                                        style="Accent.TButton",
                                        command=self.scan_chat_now)
        self.scan_chat_btn.pack(side="right", padx=(0, 3))
        ttk.Separator(bar2, orient="vertical").pack(side="right", fill="y",
                                                    padx=10)

        ttk.Label(bar2, text="排序：", style="PanelMuted.TLabel").pack(
            side="left")
        self.sort_var = tk.StringVar(value=dict(SORT_OPTIONS)["last_seen"])
        combo = ttk.Combobox(bar2, state="readonly", width=13,
                             textvariable=self.sort_var,
                             values=[label for _, label in SORT_OPTIONS])
        combo.pack(side="left", padx=4)
        combo.bind("<<ComboboxSelected>>", lambda e: self._on_sort_selected())
        self.sort_dir_btn = ttk.Button(bar2, text="↓ 降序",
                                       command=self.toggle_sort_dir)
        self.sort_dir_btn.pack(side="left")

        # ---- 第三行：区域 / 通知 / 托盘 ----
        bar3 = ttk.Frame(outer, style="Panel.TFrame")
        bar3.pack(fill="x", pady=(6, 0))
        ttk.Button(bar3, text="最小化到托盘",
                   command=self.hide_to_tray).pack(side="right")
        ttk.Button(bar3, text="通知设置",
                   command=self.open_notification_settings).pack(side="right",
                                                                 padx=3)
        ttk.Button(bar3, text="恢复全部默认", command=self.reset_regions).pack(
            side="right")
        ttk.Button(bar3, text="校准区域", command=self.open_calibrator).pack(
            side="right", padx=3)
        ttk.Label(bar3, style="PanelMuted.TLabel",
                  text="聊天框为按需扫描：点 [扫描聊天框] 才抓图 + OCR").pack(
            side="left", padx=6)

    # ---------------------------------------------------------------- 列表
    def _build_tree(self):
        p = theme.PALETTE
        frm = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        frm.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(frm, columns=COLUMNS, show="headings",
                                 selectmode="extended")
        for col in COLUMNS:
            self.tree.heading(col, text=HEADERS[col],
                              command=lambda c=col: self.sort_by_column(c))
            self.tree.column(col, width=WIDTHS[col],
                             anchor="center" if col != "note" else "w",
                             stretch=(col == "note"))

        vsb = ttk.Scrollbar(frm, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(frm, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="we")
        frm.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)

        self.tree.tag_configure("hit", background=p["flash"])
        self.tree.tag_configure("odd", background=p["row_odd"])
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Double-1>", lambda e: self.edit_selected())

        self.menu = theme.make_menu(self.root)
        self.menu.add_command(label="编辑", command=self.edit_selected)
        self.menu.add_command(label="删除", command=self.delete_selected)
        self.menu.add_separator()
        self.menu.add_command(label="清零遇到次数",
                              command=lambda: self._reset_encounter(self._ctx_iid))
        self.menu.add_command(label="重置最后遇见时间",
                              command=lambda: self._reset_last_seen(self._ctx_iid))
        self._ctx_iid = None

    def _build_statusbar(self):
        p = theme.PALETTE
        ttk.Separator(self.root, orient="horizontal").pack(fill="x",
                                                           side="bottom")
        bar = ttk.Frame(self.root, style="Panel.TFrame",
                        padding=(10, 5))
        bar.pack(fill="x", side="bottom")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var, anchor="w",
                  style="PanelMuted.TLabel").pack(side="left")

        self.stats_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.stats_var, anchor="e",
                  style="Panel.TLabel").pack(side="right")
        self._refresh_stats()

    # ------------------------------------------------------------ 数据加载
    def _load_data(self):
        self.tree.delete(*self.tree.get_children())
        rows = (self.db.search(self.search_text, order_by=self.sort_key,
                               desc=self.sort_desc)
                if self.search_text
                else self.db.get_all(order_by=self.sort_key, desc=self.sort_desc))
        for i, row in enumerate(rows):
            self.tree.insert("", "end", iid=str(row["id"]),
                             values=self._row_values(row),
                             tags=("odd",) if i % 2 else ())
        self._refresh_stats()

    @staticmethod
    def _row_values(row):
        return (row.get("player_id") or "", row.get("player_name") or "",
                row.get("note") or "", row.get("tk_count") or 0,
                row.get("encounter_count") or 0,
                row.get("created_at") or "", row.get("last_seen") or "")

    def refresh(self):
        self._load_data()

    # ------------------------------------------------------------ 排序搜索
    def sort_by_column(self, col):
        if col not in ("id", "player_id", "player_name", "note", "tk_count",
                       "encounter_count", "created_at", "last_seen"):
            return
        if self.sort_key == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key = col
            self.sort_desc = True
        self._sync_sort_widgets()
        self._load_data()

    def _sync_sort_widgets(self):
        for key, label in SORT_OPTIONS:
            if key == self.sort_key:
                self.sort_var.set(label)
                break
        self.sort_dir_btn.configure(
            text="↓ 降序" if self.sort_desc else "↑ 升序")

    def _on_sort_selected(self):
        for key, label in SORT_OPTIONS:
            if label == self.sort_var.get():
                self.sort_key = key
                break
        self.sort_desc = True
        self.sort_dir_btn.configure(text="↓ 降序")
        self._load_data()

    def toggle_sort_dir(self):
        self.sort_desc = not self.sort_desc
        self._sync_sort_widgets()
        self._load_data()

    def do_search(self):
        self.search_text = self.search_var.get().strip()
        self._load_data()
        self.set_status(f"搜索“{self.search_text}”：{len(self.tree.get_children())} 条"
                        if self.search_text else "已清空搜索")

    def clear_search(self):
        self.search_var.set("")
        self.search_text = ""
        self._load_data()
        self.set_status("已清空搜索")

    # ---------------------------------------------------------------- CRUD
    def add_entry(self):
        dlg = EntryDialog(self.root, "添加黑名单条目")
        if not dlg.result:
            return
        try:
            entry_id = self.db.add(**dlg.result)
        except ValueError as e:
            messagebox.showerror("添加失败", str(e), parent=self.root)
            return
        self._after_db_change()
        self.set_status(f"已添加条目 #{entry_id}")
        self._select_and_reveal(str(entry_id))

    def edit_selected(self):
        iid = self._selected_iid()
        if not iid:
            messagebox.showinfo("提示", "请先选中一条记录", parent=self.root)
            return
        entry = self.db.get(int(iid))
        if not entry:
            self.refresh()
            return
        dlg = EntryDialog(self.root, "编辑黑名单条目", entry)
        if not dlg.result:
            return
        try:
            self.db.update(int(iid), **dlg.result)
        except ValueError as e:
            messagebox.showerror("保存失败", str(e), parent=self.root)
            return
        self._after_db_change()
        self.set_status(f"已更新条目 #{iid}")
        self._select_and_reveal(iid)

    def delete_selected(self):
        iids = list(self.tree.selection())
        if not iids:
            messagebox.showinfo("提示", "请先选中要删除的记录", parent=self.root)
            return
        if not messagebox.askyesno("确认删除",
                                   f"确定删除选中的 {len(iids)} 条记录？\n"
                                   "同时会删除这些条目的命中历史。",
                                   parent=self.root):
            return
        for iid in iids:
            self.db.delete(int(iid))
        self._after_db_change()
        self.set_status(f"已删除 {len(iids)} 条记录")

    def _reset_encounter(self, iid):
        if not iid:
            return
        self.db.reset_encounter_count(int(iid))
        if self.tree.exists(iid):
            self.tree.set(iid, "encounter_count", "0")
        self._refresh_stats()
        self.set_status(f"已清零条目 #{iid} 的遇到次数")

    def _reset_last_seen(self, iid):
        if not iid:
            return
        self.db.reset_last_seen(int(iid))
        if self.tree.exists(iid):
            self.tree.set(iid, "last_seen", "")
        self.set_status(f"已重置条目 #{iid} 的最后遇见时间")

    def _after_db_change(self):
        self._load_data()
        if self.scheduler is not None:
            try:
                self.scheduler.reload_blacklist()
            except Exception as e:                       # noqa: BLE001
                self.log.warning("刷新匹配索引失败: %s", e)
        if self.matcher is not None and self.scheduler is None:
            try:
                self.matcher.reload()
            except Exception:                            # noqa: BLE001
                pass

    def _selected_iid(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _select_and_reveal(self, iid):
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)

    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self._ctx_iid = iid
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # -------------------------------------------------- 后台线程 → 主线程
    def enqueue_encounter_update(self, data: dict) -> None:
        """供 ScanScheduler 的后台线程调用（唯一允许的跨线程入口）。"""
        self._update_queue.put(data)

    def post(self, func) -> None:
        """把任意可调用对象排到主线程执行（托盘线程用）。"""
        self._command_queue.put(func)

    def _drain_queues(self):
        """定时器回调：排空一次，然后安排下一次。

        注意与 `_drain_once` 的分工：只有本方法负责 **排定时器**。
        如果别处直接调用本方法（或本方法被重复调用），必须先把自己待执行的
        定时器取消掉，否则会留下永远不会被取消的孤儿定时器
        （窗口销毁后 Tcl 会刷 `invalid command name "..._drain_queues"`）。
        """
        if self._closing:
            return
        # 真正取消上一次排的定时器（由定时器自己触发时，这个 id 已经失效，
        # after_cancel 是安全的空操作），避免留下孤儿定时器。
        if self._drain_job is not None:
            try:
                self.root.after_cancel(self._drain_job)
            except Exception:                            # noqa: BLE001
                pass
            self._drain_job = None
        self._drain_once()
        if not self._closing:
            self._drain_job = self.root.after(GUI_QUEUE_POLL_MS,
                                              self._drain_queues)

    def _drain_once(self):
        """排空两个队列（不排定时器，可安全地被测试或其它代码直接调用）。"""
        try:
            while True:
                data = self._update_queue.get_nowait()
                try:
                    self._apply_encounter_update(data)
                except Exception as e:                   # noqa: BLE001
                    self.log.warning("应用命中更新失败: %s", e)
        except queue.Empty:
            pass

        try:
            while True:
                func = self._command_queue.get_nowait()
                try:
                    func()
                except Exception as e:                   # noqa: BLE001
                    self.log.warning("主线程任务异常: %s", e)
        except queue.Empty:
            pass

    def _apply_encounter_update(self, data: dict):
        iid = str(data.get("entry_id"))
        if self.tree.exists(iid):
            self.tree.set(iid, "encounter_count",
                          str(data.get("encounter_count", 0)))
            self.tree.set(iid, "last_seen", data.get("last_seen") or "")
            self._flash_row(iid, times=config.FLASH_TIMES)
        self.session_hit_count += 1
        self._refresh_stats()
        name = data.get("player_name") or data.get("player_id") or "未知"
        self.set_status(f"命中黑名单：{name}（{data.get('source', '')}，"
                        f"匹配度 {float(data.get('score') or 0):.0f}）")

    def _flash_row(self, iid, times=None, interval=None):
        """命中后行高亮闪烁（约 3 秒后恢复）。"""
        times = config.FLASH_TIMES if times is None else times
        interval = config.FLASH_INTERVAL_MS if interval is None else interval
        if not self.tree.exists(iid):
            return
        if iid in self._flash_jobs:
            try:
                self.root.after_cancel(self._flash_jobs.pop(iid))
            except Exception:                            # noqa: BLE001
                pass

        def step(remaining):
            self._flash_jobs.pop(iid, None)
            if not self.tree.exists(iid):
                return
            try:
                if remaining <= 0:
                    self.tree.item(iid, tags=())
                else:
                    self.tree.item(iid, tags=("hit",))
                    self._flash_jobs[iid] = self.root.after(
                        interval, step, remaining - 1)
            except tk.TclError:
                pass

        step(times)

    # ---------------------------------------------------------------- 统计
    def _refresh_stats(self):
        total = len(self.tree.get_children())
        try:
            today = self.db.get_today_encounter_count()
        except Exception:                                # noqa: BLE001
            today = 0
        self.stats_var.set(
            f"黑名单总数：{total}　|　本局命中：{self.session_hit_count}"
            f"　|　今日命中：{today}"
        )

    def set_status(self, text: str):
        self.status_var.set(text)

    def _flash_status(self, text: str, ms: int = 2500):
        """状态栏短暂提示：到时间后恢复成「就绪」类文案。"""
        self.status_var.set(text)
        if getattr(self, "_flash_job", None):
            try:
                self.root.after_cancel(self._flash_job)
            except Exception:                            # noqa: BLE001
                pass
        self._flash_job = self.root.after(ms, self._restore_status)

    def _restore_status(self):
        self._flash_job = None
        total = len(self.tree.get_children())
        self.status_var.set(f"就绪（当前列出 {total} 条）")

    def reset_session_stats(self):
        """一局开始时清零“本局命中”。"""
        self.session_hit_count = 0
        self._refresh_stats()

    def set_monitoring(self, active: bool, reason: str = ""):
        """供 app 层更新监控指示灯。"""
        if active:
            self.monitor_var.set("● 监控中")
            self.monitor_label.configure(style="Hit.TLabel")
        else:
            self.monitor_var.set(f"● 已暂停{(' (' + reason + ')') if reason else ''}")
            self.monitor_label.configure(style="Paused.TLabel")

    def toggle_pause(self):
        self.paused = bool(self.pause_var.get())
        if self.on_pause:
            self.on_pause(self.paused)
        self.set_monitoring(not self.paused, "手动暂停" if self.paused else "")
        self.set_status("已暂停监控" if self.paused else "已恢复监控")

    # ---------------------------------------------------------------- 设置
    def open_calibrator(self):
        from app.ui.calibrator import CalibratorDialog
        CalibratorDialog(self.root, self.region_config, self.capture,
                         on_changed=lambda: self.set_status("区域配置已更新"))

    def reset_regions(self):
        if not messagebox.askyesno("确认", "恢复全部监视区域为默认坐标？",
                                   parent=self.root):
            return
        self.region_config.reset_all()
        self.set_status("全部区域已恢复默认")

    def open_notification_settings(self):
        if self.notifier is None:
            messagebox.showinfo("提示", "通知模块不可用", parent=self.root)
            return
        from app.ui.gui_notification import NotificationSettingsDialog
        NotificationSettingsDialog(self.root, self.notification_config,
                                   self.notifier,
                                   on_saved=lambda: self.set_status("通知设置已保存"))

    # ------------------------------------------------------- 聊天框按需扫描
    def scan_chat_now(self):
        """[扫描聊天框]：抓一张聊天框截图、跑一次 OCR、匹配黑名单。

        OCR 放到后台线程，避免阻塞 UI；结果通过 post() 回到主线程更新状态栏。
        """
        if self.chat_scanner is None:
            self._flash_status("聊天框扫描不可用（未接入 ChatScanner）")
            return
        if self.chat_scanner.is_busy():
            self._flash_status("上一次聊天框扫描还没结束，已忽略本次点击")
            return
        self._flash_status("已触发聊天框扫描…", 6000)
        threading.Thread(target=self._scan_chat_worker, daemon=True,
                         name="ChatScan").start()

    def _scan_chat_worker(self):
        try:
            result = self.chat_scanner.scan_now()
        except Exception as e:                           # noqa: BLE001
            result = {"status": "error", "error": str(e)}
        self.post(lambda: self._on_scan_chat_done(result))

    def _on_scan_chat_done(self, result: dict):
        status = result.get("status")
        hits = int(result.get("hits") or 0)
        ms = result.get("elapsed_ms") or 0
        text = {
            "busy": "上一次扫描尚未完成，本次已跳过",
            "empty": "聊天框为空（OCR 没识别到文字）",
            "no_hit": f"聊天框扫描完成：未命中黑名单（{ms:.0f}ms）",
            "ok": f"聊天框扫描完成：命中 {hits} 条（{ms:.0f}ms）",
            "error": f"聊天框扫描失败：{result.get('error', '未知错误')}",
        }.get(status, "聊天框扫描完成")
        self._flash_status(text, 5000)

    def _clear_dedup_cache(self):
        if self.scheduler is None:
            return
        self.scheduler.clear_dedup_cache()
        self._flash_status("已清空命中去重缓存（立即可以重新计数）")

    def _toggle_chat_hotkey(self):
        """菜单里的「启用热键扫描」勾选：写盘并通知 app 重启轮询线程。"""
        enabled = bool(self.chat_hotkey_var.get())
        if self.hotkey_config is None:
            self._flash_status("热键配置不可用")
            return
        cfg = self.hotkey_config.set_enabled(enabled)
        self._notify_hotkey_changed(cfg)

    def open_hotkey_settings(self):
        """打开「自定义快捷键…」对话框。"""
        if self.hotkey_config is None:
            self._flash_status("热键配置不可用")
            return
        from app.ui.hotkey_dialog import HotkeyDialog
        HotkeyDialog(self.root, self.hotkey_config,
                     on_saved=self._on_hotkey_saved)

    def _on_hotkey_saved(self, cfg: dict):
        self.chat_hotkey_var.set(bool(cfg.get("enabled")))
        self._sync_hotkey_menu_label()
        self._notify_hotkey_changed(cfg)

    def _notify_hotkey_changed(self, cfg: dict):
        self._sync_hotkey_menu_label()
        if self.on_chat_hotkey is None:
            self._flash_status("热键开关不可用（未接入 app）")
            return
        try:
            # 回调可以返回一句状态说明（例如「已注册为系统全局热键」）
            status = self.on_chat_hotkey(cfg)
            name = cfg.get("name") or "热键"
            text = status if isinstance(status, str) and status.strip() else (
                f"扫描热键 {name} 已{'启用' if cfg.get('enabled') else '停用'}")
            self._flash_status(text, 8000)
        except Exception as e:                           # noqa: BLE001
            messagebox.showerror("热键切换失败", str(e), parent=self.root)

    def _sync_hotkey_menu_label(self):
        """把当前按键名显示在菜单项上，例如「启用热键扫描（Ctrl+F8）」。"""
        if self.hotkey_config is None or not hasattr(self, "chat_menu"):
            return
        try:
            self.chat_menu.entryconfigure(
                self._hotkey_menu_index,
                label=f"启用热键扫描（{self.hotkey_config.display}）")
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- 导入导出
    def export_list(self):
        """导出黑名单为 CSV 或 JSON（后台线程写盘，完成后回主线程提示）。"""
        path = filedialog.asksaveasfilename(
            parent=self.root, title="导出黑名单", defaultextension=".csv",
            filetypes=[("CSV 文件（Excel 兼容）", "*.csv"),
                       ("JSON 文件", "*.json"),
                       ("所有文件", "*.*")])
        if not path:
            return
        self._flash_status(f"正在导出到 {os.path.basename(path)} …", 8000)
        threading.Thread(target=self._export_worker, args=(path,),
                         daemon=True, name="ExportList").start()

    def _export_worker(self, path: str):
        error = None
        count = 0
        try:
            rows = self.db.export_all()
            ext = os.path.splitext(path)[1].lower()
            if ext == ".json":
                write_json(path, rows)
            else:
                write_csv(path, rows)
            count = len(rows)
            self.log.info("已导出 %d 条到 %s", count, path)
        except Exception as exc:                         # noqa: BLE001
            # 注意：不能把 `exc` 直接闭包进 lambda —— Python 在 except 块结束
            # 时会删除这个变量名，延迟执行的回调会拿到 NameError。
            error = exc
            self.log.warning("导出失败: %s", exc)
        self.post(lambda: self._on_export_done(count, path, error))

    def _on_export_done(self, count: int, path: str, error):
        if error is not None:
            messagebox.showerror("导出失败", str(error), parent=self.root)
            self._flash_status("导出失败")
            return
        self._flash_status(f"已导出 {count} 条到 {os.path.basename(path)}", 6000)

    def import_list(self):
        """从 CSV / JSON 导入黑名单（先选冲突策略，再后台导入）。"""
        path = filedialog.askopenfilename(
            parent=self.root, title="导入黑名单",
            filetypes=[("名单文件", "*.csv *.json"),
                       ("CSV 文件", "*.csv"),
                       ("JSON 文件", "*.json"),
                       ("所有文件", "*.*")])
        if not path:
            return

        dlg = ImportStrategyDialog(self.root, os.path.basename(path))
        if dlg.result is None:
            return

        self._flash_status(f"正在导入 {os.path.basename(path)} …", 8000)
        threading.Thread(target=self._import_worker, args=(path, dlg.result),
                         daemon=True, name="ImportList").start()

    def _import_worker(self, path: str, strategy: str):
        error = None
        result = None
        try:
            entries = read_entries(path)
            if not entries:
                error = ValueError("文件里没有可导入的条目")
            else:
                result = self.db.import_entries(entries, strategy=strategy)
                self.log.info("导入 %s 策略=%s 结果=%s", path, strategy,
                              result)
        except Exception as exc:                         # noqa: BLE001
            error = exc          # 同上：不要闭包 except 的变量名
            self.log.warning("导入失败: %s", exc)
        self.post(lambda: self._on_import_done(result, path, strategy, error))

    def _on_import_done(self, result, path: str, strategy: str, error):
        if error is not None:
            messagebox.showerror("导入失败", str(error), parent=self.root)
            self._flash_status("导入失败（已回滚，数据库未改动）")
            return
        self._after_db_change()          # 必须在主线程刷新 Treeview
        msg = (f"导入完成：新增 {result['inserted']}，"
               f"更新 {result['updated']}，跳过 {result['skipped']}")
        self.log.info("%s（文件=%s 策略=%s）", msg, path, strategy)
        messagebox.showinfo("导入完成", msg, parent=self.root)
        self._flash_status(msg, 8000)

    def open_data_dir(self):
        try:
            os.startfile(config.DATA_DIR)                # noqa: S606
        except Exception as e:                           # noqa: BLE001
            messagebox.showerror("打开失败", str(e), parent=self.root)

    def open_log(self):
        try:
            os.startfile(config.LOG_PATH)                # noqa: S606
        except Exception as e:                           # noqa: BLE001
            messagebox.showerror("打开失败", str(e), parent=self.root)

    def show_help(self):
        messagebox.showinfo(
            "使用说明",
            "触发扫描的方式：\n"
            "  · 聊天框（按需）：点工具栏 [扫描聊天框] 或按 F8（需在\n"
            "    设置 → 聊天框扫描快捷键 里启用）才抓一次图 + 跑一次 OCR；\n"
            "    平时完全不会扫描聊天框，不占 CPU；\n"
            "  · 冷启动：游戏启动后 12 秒自动扫一次 HUD 玩家列表；\n"
            "  · ESC：按 ESC 打开菜单时自动扫一次菜单玩家列表。\n\n"
            f"命中去重：同一玩家 {config.HIT_DEDUP_WINDOW} 秒内只计数一次、"
            "只提示一次，\n  防止连点扫描按钮造成重复计数。\n"
            "批量命中：一次扫描命中多个玩家时，每个玩家一个通知栏，\n"
            "  但只播一次音效（最多同时弹 5 个）。\n\n"
            "名单备份：工具栏 [导出] / [导入]，支持 CSV（Excel 兼容）与 JSON。\n"
            "自定义提示：设置 → 通知设置。\n"
            "校准区域：设置 → 校准区域（或工具栏）。\n\n"
            "本工具不注入、不读内存、不改包，仅截图 + OCR + 本地比对。\n"
            "重复打开不会多开进程：会把已经运行的那个窗口叫到前台。",
            parent=self.root)

    def show_about(self):
        messagebox.showinfo(
            "关于",
            f"{config.APP_NAME} v{config.VERSION}\n\n"
            "屏幕截图 + OCR + 本地黑名单比对，全程不注入游戏进程。\n"
            "提示窗口使用 WS_EX_NOACTIVATE | WS_EX_TRANSPARENT，不抢焦点。\n\n"
            f"数据目录：{config.DATA_DIR}",
            parent=self.root)

    # ---------------------------------------------------------------- 托盘
    def hide_to_tray(self):
        self.root.withdraw()
        self._ensure_tray()
        self.set_status("已最小化到托盘，监控继续运行")

    def _ensure_tray(self):
        if self._tray is not None:
            return
        try:
            import pystray
            icon_image = theme.load_icon_image(64)
            if icon_image is None:
                from app.notify.notifier import ensure_default_icon
                from PIL import Image
                icon_image = Image.open(ensure_default_icon()).convert("RGBA")
            image = icon_image
        except Exception as e:                           # noqa: BLE001
            self.log.warning("系统托盘不可用: %s", e)
            self.set_status("系统托盘不可用（pystray 未安装？）")
            return

        def _show(icon=None, item=None):
            self.post(self.show_window)

        def _quit(icon=None, item=None):
            self.post(self.quit_app)

        def _pause(icon=None, item=None):
            self.post(lambda: (self.pause_var.set(not self.pause_var.get()),
                               self.toggle_pause()))

        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", _show, default=True),
            pystray.MenuItem("暂停/恢复监控", _pause),
            pystray.MenuItem("校准区域", lambda i, it: self.post(self.open_calibrator)),
            pystray.MenuItem("通知设置",
                             lambda i, it: self.post(self.open_notification_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", _quit),
        )
        self._tray = pystray.Icon("hd2_blacklist", image,
                                  f"{config.APP_NAME}  v{config.VERSION}", menu)
        self._tray_thread = threading.Thread(target=self._tray.run, daemon=True,
                                             name="Tray")
        self._tray_thread.start()

    def _stop_tray(self):
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:                            # noqa: BLE001
                pass
            self._tray = None

    def show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self._stop_tray()
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- 关闭
    def on_close(self):
        """点右上角关闭 → 最小化到托盘（不退出）。"""
        self.hide_to_tray()

    def _cancel_pending(self):
        """取消所有挂起的 after 任务。

        否则窗口销毁后 Tcl 还会去调用已消失的回调，控制台会刷
        `invalid command name "..._drain_queues"`。
        """
        jobs = [self._drain_job, self._flash_job] + list(self._flash_jobs.values())
        for job in jobs:
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:                        # noqa: BLE001
                    pass
        self._flash_jobs.clear()
        self._drain_job = None
        self._flash_job = None

    def quit_app(self):
        if self._closing:
            return
        self._closing = True
        self._cancel_pending()
        self._stop_tray()
        try:
            if self.on_quit:
                self.on_quit()
        except Exception as e:                           # noqa: BLE001
            self.log.warning("退出回调异常: %s", e)
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass

    def destroy(self):
        """不触发 on_quit 的静默关闭（测试 / 异常收尾用）。"""
        self._closing = True
        self._cancel_pending()
        self._stop_tray()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- 运行
    def run(self):
        self.root.mainloop()

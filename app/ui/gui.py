# -*- coding: utf-8 -*-
"""gui.py —— 主界面。

布局（单页五段，每个功能只有一个入口）：
    ① 运行状态带   应用标识 + 监控灯 + 统计 + F8 开关 + [扫描聊天框]/[暂停]/[退出]
    ② 名单工具带   增删改查 + 搜索 + 排序 + 导入导出
    ③ 名单主区     表格（占满剩余空间）
    ④ 设置分页     扫描与触发 / 通知 / 监视区域 / 数据 / 帮助（默认收起，可拖拽分隔）
    ⑤ 状态栏       「最近动态」事件流（3 行，带时间戳）+ 当前状态 + 统计

没有菜单栏：菜单里曾经和工具栏重复的入口全部收敛到 ①②④，避免"同一个设置
在两处出现、两处不一致"。

红线：**所有 tkinter 操作都在主线程**。
后台线程（扫描会话 / 进程监控 / ESC / 托盘）只能往两个 queue 里塞东西：
    * _update_queue —— 命中数据（enqueue_encounter_update）
    * _command_queue —— 需要主线程执行的可调用对象（post）
主线程用 root.after(GUI_QUEUE_POLL_MS) 周期性排空它们。
"""
from __future__ import annotations

import collections
import csv
import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app import config
from app.ui import theme
from app.config import GUI_QUEUE_POLL_MS, get_logger

#: 名单只有三列：名字 / 备注 / 添加时间。
#: 玩家ID、TK次数、遇到次数、最后遇见都删掉了（v1.1.2）—— 名单只回答
#: "这个名字要不要提醒"，统计去 app.log 和 evidence/ 里看。
COLUMNS = ("player_name", "note", "created_at")
HEADERS = {"player_name": "名称", "note": "备注", "created_at": "添加时间"}
WIDTHS = {"player_name": 220, "note": 520, "created_at": 165}

SORT_OPTIONS = (("created_at", "按添加时间"), ("player_name", "按名称"))

# --------------------------------------------------------------------------
# 布局常量
# --------------------------------------------------------------------------
#: 默认窗口尺寸与最小尺寸（原来 1120x660 装不下「名单 + 展开的设置区」）
DEFAULT_WIDTH, DEFAULT_HEIGHT = 1280, 820
#: 1060 = 名单工具带自然宽度（约 1030）+ 余量；再窄按钮就会被 pack 挤扁
MIN_WIDTH, MIN_HEIGHT = 1060, 640

#: 下方设置区分页（key, 标签）。顺序 = 标签栏从左到右的顺序
SETTINGS_TABS = (
    ("scan", "扫描与触发"),
    ("notify", "通知"),
    ("region", "监视区域"),
    ("data", "数据"),
    ("help", "帮助"),
)
#: 设置区展开时占的高度
SETTINGS_HEIGHT = 330
#: 设置区收起时只剩一条标签栏
SETTINGS_COLLAPSED_HEIGHT = 38
#: 名单主区至少留这么高，免得设置区把列表挤没
MIN_LIST_HEIGHT = 160
#: 面板高度变化不超过这个像素数就当成"自己挪分隔条造成的抖动"，不再重摆
JITTER_TOLERANCE = 4
#: 收起态高度的上限（标签栏一行；超过说明控件还没被摆成一行、reqheight 虚高）
MAX_COLLAPSED_HEIGHT = 96
#: 状态栏里「最近动态」保留几行（最新在最上面）
EVENT_ROWS = 3

HELP_TEXT = (
    "触发扫描的方式：\n"
    "  · 聊天框（按需）：点顶部 [扫描聊天框] 或按你自己设的快捷键，才抓一次图\n"
    "    + 跑一次 OCR；平时完全不扫聊天框，不占 CPU。\n"
    f"  · 冷启动：游戏启动后自动扫一次 HUD 玩家列表。\n"
    "  · ESC：按 ESC 打开菜单时自动扫一次菜单玩家列表。\n\n"
    f"命中去重：同一玩家 {config.HIT_DEDUP_WINDOW} 秒内只计数一次、只提示一次。\n"
    "批量命中：一次扫到多个黑名单玩家时，每人一个通知栏、只播一次音效\n"
    "  （最多同时弹 5 个，超出的汇总在最后一栏）。\n\n"
    "本工具不注入、不读内存、不改包，仅截图 + OCR + 本地比对。\n"
    "重复打开不会多开进程：会把已经运行的那个窗口叫到前台。\n"
    "关闭窗口 = 最小化到托盘，监控继续；要退出请点顶部 [退出]。"
)


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
            "player_name": tk.StringVar(value=(entry or {}).get("player_name") or ""),
        }

        frm = ttk.Frame(self.top, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="玩家名称：").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.vars["player_name"], width=38).grid(
            row=0, column=1, columnspan=2, sticky="we", pady=4)

        ttk.Label(frm, text="备注描述：").grid(row=1, column=0, sticky="nw", pady=4)
        self.note_text = tk.Text(frm, width=40, height=5, wrap="word")
        theme.style_text(self.note_text)
        self.note_text.grid(row=1, column=1, columnspan=2, sticky="we", pady=4)
        if entry and entry.get("note"):
            self.note_text.insert("1.0", entry["note"])

        ttk.Label(frm, text="只需填玩家名称（就是游戏里显示的那个名字）；同名只能有一条。\n"
                            "命中时的证据截图由程序自动保存到 data\\evidence\\，不用手填。",
                  foreground="#888888", justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        bar = ttk.Frame(frm)
        bar.grid(row=3, column=0, columnspan=3, sticky="e", pady=(10, 0))
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

    def _ok(self):
        data = {
            "player_name": self.vars["player_name"].get().strip(),
            "note": self.note_text.get("1.0", "end-1c").strip(),
        }
        if not data["player_name"]:
            messagebox.showwarning("输入有误", "玩家名称不能为空（就是你看到的那个名字）",
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
IO_FIELDS = ("player_name", "note", "created_at")


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
         "只新增文件里有、这里没有的名字；同名就跳过"),
        ("update_note", "只更新备注",
         "同名条目的备注用文件里的覆盖，添加时间保持本机的"),
        ("overwrite", "完全覆盖",
         "同名条目的备注与添加时间都用文件里的值覆盖"),
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
        ttk.Label(frm, text="遇到同名条目时：",
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
class _ScrollArea(ttk.Frame):
    """竖向滚动容器（内容装得下就自动隐藏滚动条）。

    设置分页的内容高度是固定的（通知页要摆一整个表单），而设置区高度会随
    窗口大小 / 用户拖拽变化 —— 没有滚动的话，[保存] 这类按钮会被裁到可视区
    之外，点都点不到。
    """

    def __init__(self, master):
        super().__init__(master)
        # width/height=1 不是摆设：tk.Canvas 的默认尺寸是 378x265，会把收起态
        # 面板的请求高度撑到 ~350px，于是启动时能看见分隔条先窜上去再落回来。
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0,
                                width=1, height=1, bg=theme.PALETTE["bg"])
        self.vsb = ttk.Scrollbar(self, orient="vertical",
                                 command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        # 滚动条常驻：不做"需要才显示"的自动隐藏 —— 显示/隐藏会改变画布宽度，
        # 画布宽度又会影响换行后的内容高度，来回抖动会变成死循环。宁可常驻。
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self._sync_job = None
        for w in (self.inner, self.canvas):
            w.bind("<Configure>", self._schedule_sync, add="+")

    def _schedule_sync(self, _event=None):
        """合并连续的 Configure：每轮空闲只重算一次滚动区域。"""
        if self._sync_job is not None:
            return
        try:
            self._sync_job = self.after_idle(self._sync)
        except tk.TclError:
            self._sync_job = None

    def _sync(self):
        self._sync_job = None
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            width = self.canvas.winfo_width()
            if width > 1:
                self.canvas.itemconfigure(self._win, width=width)
        except tk.TclError:
            pass

    def bind_wheel(self):
        """给容器内所有控件绑滚轮（Tk 只把滚轮事件发给指针下的控件）。"""
        def bind_all_children(w):
            try:
                w.bind("<MouseWheel>", self._on_wheel)
            except tk.TclError:
                return
            for c in w.winfo_children():
                bind_all_children(c)

        bind_all_children(self.inner)

    def _on_wheel(self, event):
        try:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            pass
        return "break"


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
        self.game_active = False
        self.game_reason = ""
        self.sort_key = "created_at"
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
        self.root.geometry(f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT}")
        self.root.minsize(MIN_WIDTH, MIN_HEIGHT)

        theme.apply_theme(self.root)
        theme.apply_window_icon(self.root)

        # 运行状态带上的开关变量（建好窗口后立刻创建，按钮直接绑定）
        self.chat_hotkey_var = tk.BooleanVar(
            value=bool(hotkey_config.enabled)
            if hotkey_config is not None else False)
        self.pause_var = tk.BooleanVar(value=False)
        self.data_info_var = tk.StringVar(value="")
        self.settings_expanded = False          # 设置区默认收起
        self.settings_page = SETTINGS_TABS[0][0]
        self.settings_pages = {}
        self.settings_tab_btns = {}
        self._last_paned_h = 0
        self._placed_h = 0            # 上次定高度时的面板高度（防抖用）
        self._resync_job = None

        self._build_header()          # ① 运行状态带
        self._build_toolbar()         # ② 名单工具带
        self._build_body()            # ③④ 名单 + 设置分页（可拖拽分隔）
        self._build_statusbar()       # ⑤ 状态栏
        self._sync_hotkey_ui()

        # 聊天框扫描的结果统一由扫描器回调上报：**按钮和 F8 热键走同一条路**，
        # 否则按热键触发的那次扫描在界面上什么都不显示（实测反馈）。
        if self.chat_scanner is not None:
            self.chat_scanner.on_result = self._chat_scan_event

        self._load_data()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._drain_job = self.root.after(GUI_QUEUE_POLL_MS, self._drain_queues)

    # ------------------------------------------------------- ① 运行状态带
    def _build_header(self):
        """顶部一条：应用标识 + 运行状态 + 高频动作。

        这条带子取代了原来的「标题带 + 设置菜单」：F8 开关、暂停、扫描这些
        随时要看、要点的东西现在一直露在外面，不用翻菜单。
        """
        p = theme.PALETTE
        bar = ttk.Frame(self.root, style="Panel.TFrame", padding=(10, 6))
        bar.pack(fill="x")

        # ---- 左：应用标识 ----
        try:
            from PIL import ImageTk
            icon = theme.load_icon_image(24)
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

        # ---- 中：监控指示灯 ----
        self.monitor_var = tk.StringVar(value="● 监控中")
        self.monitor_label = ttk.Label(bar, textvariable=self.monitor_var,
                                       style="Hit.TLabel")
        self.monitor_label.pack(side="left", padx=(16, 0))

        # ---- 右：高频动作（pack 顺序 = 从右往左）----
        ttk.Button(bar, text="退出", style="Bar.TButton",
                   command=self.quit_app).pack(side="right")
        self.pause_btn = ttk.Button(bar, text="暂停监控", style="Bar.TButton",
                                    command=self.toggle_pause)
        self.pause_btn.pack(side="right", padx=(0, 6))
        self.scan_chat_btn = ttk.Button(bar, text="扫描聊天框",
                                        style="Accent.TButton",
                                        command=self.scan_chat_now)
        self.scan_chat_btn.pack(side="right", padx=(0, 6))
        ttk.Separator(bar, orient="vertical").pack(side="right", fill="y",
                                                   padx=10)
        # 热键开关：文字写"动作"（启用/停用 X 扫描），金色 = 已启用。
        # 原来是 textvariable 的"热键扫描：关"，在深色面板上看就是一段灰字，
        # 用户根本不知道那是个按钮（实测反馈）。
        self.hotkey_btn = ttk.Button(bar, text="启用扫描热键",
                                     style="Bar.TButton",
                                     command=self.toggle_hotkey)
        self.hotkey_btn.pack(side="right", padx=(0, 6))

    # ------------------------------------------------------- ② 名单工具带
    def _build_toolbar(self):
        """一条：黑名单的增删改查 + 排序 + 导入导出。

        原来三行、混着运行控制和低频设置；现在只放「对这张名单的操作」，
        运行控制在上面 ①，低频设置收进下面 ④。
        """
        outer = ttk.Frame(self.root, style="Panel.TFrame",
                          padding=(10, 6, 10, 6))
        outer.pack(fill="x")
        bar = ttk.Frame(outer, style="Panel.TFrame")
        bar.pack(fill="x")

        ttk.Button(bar, text="添加", style="Bar.TButton",
                   command=self.add_entry).pack(side="left")
        ttk.Button(bar, text="编辑", style="Bar.TButton",
                   command=self.edit_selected).pack(side="left", padx=3)
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
        ttk.Button(bar, text="搜索", style="Bar.TButton",
                   command=self.do_search).pack(side="left")
        ttk.Label(bar, text="（按名称 / 备注）", style="PanelMuted.TLabel").pack(
            side="left", padx=(6, 0))
        ttk.Button(bar, text="清空", style="Bar.TButton",
                   command=self.clear_search).pack(side="left", padx=3)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                   padx=10)

        ttk.Label(bar, text="排序：", style="PanelMuted.TLabel").pack(
            side="left")
        self.sort_var = tk.StringVar(value=dict(SORT_OPTIONS)["created_at"])
        combo = ttk.Combobox(bar, state="readonly", width=12,
                             textvariable=self.sort_var,
                             values=[label for _, label in SORT_OPTIONS])
        combo.pack(side="left", padx=4)
        combo.bind("<<ComboboxSelected>>", lambda e: self._on_sort_selected())
        self.sort_dir_btn = ttk.Button(bar, text="↓ 降序", style="Bar.TButton",
                                       command=self.toggle_sort_dir)
        self.sort_dir_btn.pack(side="left")

        ttk.Button(bar, text="导出", style="Bar.TButton",
                   command=self.export_list).pack(side="right")
        ttk.Button(bar, text="导入", style="Bar.TButton",
                   command=self.import_list).pack(side="right", padx=3)

    # --------------------------------------- ③④ 名单 + 设置（可拖拽分隔）
    def _build_body(self):
        """下半部分：上面是名单主区，下面是设置分页，中间分隔条可以拖。

        设置区默认收起成一条标签栏，把高度全让给名单；点标签或 [展开设置]
        才展开。用经典 tk.PanedWindow 而不是 ttk 的，因为只有它能把面板
        压到比内容请求尺寸更小（收起时只留标签栏）。
        """
        self.paned = tk.PanedWindow(
            self.root, orient="vertical", sashwidth=6, sashrelief="flat",
            bd=0, bg=theme.PALETTE["border"],
            opaqueresize=True, showhandle=False)
        self.paned.pack(fill="both", expand=True, padx=10, pady=(0, 4))

        list_frame = ttk.Frame(self.paned)
        self.paned.add(list_frame, stretch="always", minsize=140)
        self._build_tree(list_frame)

        self.settings_frame = ttk.Frame(self.paned)
        self.paned.add(self.settings_frame, stretch="never",
                       minsize=SETTINGS_COLLAPSED_HEIGHT)
        self._build_settings(self.settings_frame)
        self.paned.paneconfigure(self.settings_frame, minsize=self._collapsed_h)
        self.set_settings_expanded(False)          # 默认收起：高度全给名单

        self.paned.bind("<Configure>", self._on_paned_configure)
        self.paned.bind("<ButtonRelease-1>", self._on_sash_released)
        # 建好首屏分页内容，但**保持收起**（默认折叠）
        self.root.after(150, lambda: self.show_settings_page(
            self.settings_page, expand=False))

    # ------------------------------------------------------------ ④ 设置区
    def _build_settings(self, parent):
        """下方设置区：一条始终可见的分页标签栏 + 可折叠的内容区。"""
        self.settings_bar = ttk.Frame(parent, style="Panel.TFrame",
                                      padding=(6, 4))
        self.settings_bar.pack(fill="x", side="top")

        self.settings_toggle_btn = ttk.Button(
            self.settings_bar, text="▴ 展开设置", width=12,
            style="Bar.TButton", command=self.toggle_settings)
        self.settings_toggle_btn.pack(side="right", padx=(8, 4))

        for key, label in SETTINGS_TABS:
            btn = ttk.Button(self.settings_bar, text=label, width=10,
                             style="Bar.TButton",
                             command=lambda k=key: self.show_settings_page(k))
            btn.pack(side="left", padx=2)
            self.settings_tab_btns[key] = btn

        # 分页内容放在滚动容器里：页面再高也不会把按钮裁到看不见的地方
        self.settings_content = _ScrollArea(parent)
        self.settings_content.pack(fill="both", expand=True)
        self.settings_body = self.settings_content.inner
        # 收起时的高度按标签栏的**实际**请求高度算，否则会被切掉半行。
        # 首次计算时控件可能还没完成尺寸推导，_sync_settings_layout 里会再纠正。
        self._collapsed_h = max(SETTINGS_COLLAPSED_HEIGHT,
                                self.settings_bar.winfo_reqheight() + 6)

    def _make_page(self, key):
        """按需创建分页内容（懒建：通知页要渲染预览，不必开机就建）。"""
        page = self.settings_pages.get(key)
        if page is not None:
            return page
        page = ttk.Frame(self.settings_body, padding=2)
        {
            "scan": self._page_scan,
            "notify": self._page_notify,
            "region": self._page_region,
            "data": self._page_data,
            "help": self._page_help,
        }[key](page)
        self.settings_pages[key] = page
        self.settings_content.bind_wheel()
        return page

    def show_settings_page(self, key, expand: bool = True):
        """切到某个设置分页。

        ``expand=False`` 用于开机首屏：把内容建好、但保持收起状态。
        用户点标签时走默认的 expand=True，自动展开。
        """
        if key not in dict(SETTINGS_TABS):
            key = SETTINGS_TABS[0][0]
        self.settings_page = key
        page = self._make_page(key)
        for other_key, frame in self.settings_pages.items():
            if other_key != key and frame.winfo_ismapped():
                frame.pack_forget()
        page.pack(fill="both", expand=True)
        for k, btn in self.settings_tab_btns.items():
            btn.configure(style="Accent.TButton" if k == key else "Bar.TButton")
        if expand and not self.settings_expanded:
            self.set_settings_expanded(True)
        # 每次进「扫描与触发」都对齐一次热键总开关（顶部那个按钮才是开关的正主）
        if key == "scan" and getattr(self, "hotkey_panel", None) is not None:
            try:
                self.hotkey_panel.refresh_enabled(
                    bool(self.chat_hotkey_var.get()))
            except Exception as e:                        # noqa: BLE001
                self.log.warning("刷新自定义快捷键面板失败: %s", e)
        # force：刚建好的分页要等一轮几何计算才量得出内容高度
        self._sync_settings_layout(force=True)

    def toggle_settings(self):
        self.set_settings_expanded(not self.settings_expanded)

    def set_settings_expanded(self, expanded: bool):
        """展开/收起设置区。

        收起时把内容区真的 pack_forget 掉（而不是只靠挪分隔条），这样
        分页内容确实不可见、也不会去参与布局计算。
        """
        self.settings_expanded = bool(expanded)
        if self.settings_expanded:
            if not self.settings_content.winfo_manager():
                self.settings_content.pack(fill="both", expand=True)
        else:
            self.settings_content.pack_forget()
        self.settings_toggle_btn.configure(
            text="▾ 收起设置" if self.settings_expanded else "▴ 展开设置")
        self._sync_settings_layout(force=True)

    def _schedule_resync(self, delay: int = 60):
        """安排一次延后重算（只保留最新的一个，避免同时挂好几个）。"""
        job = getattr(self, "_resync_job", None)
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:                            # noqa: BLE001
                pass
        try:
            self._resync_job = self.root.after(delay, self._sync_settings_layout)
        except tk.TclError:
            self._resync_job = None

    def _sync_settings_layout(self, force: bool = False):
        """把分隔条挪到该在的位置。

        展开高度按**当前分页的内容高度**自适应（上限为面板高度的 60%），
        这样大多数分页展开后刚好够用、不用滚；内容实在太高时靠滚动容器兜底。

        ⚠ ``_placed_h`` 这个容差判断不是优化，是**修 bug**：移动分隔条会让
        tk.PanedWindow 自身高度变化 1px，那 1px 又触发 <Configure> → 再挪 →
        再变 1px…… 无限来回抖（界面抽搐 + 事件循环空转烧 CPU）。
        只有"面板高度真的变了"（用户缩放窗口）才重新摆。
        """
        if not hasattr(self, "paned"):
            return
        if force:
            self._schedule_resync()
        total = self.paned.winfo_height()
        if total <= 1:
            if force:
                self._schedule_resync(80)
            return
        # 标签栏的真实高度要等控件被摆成一行才知道，这里每轮纠正一次。
        # 注意上限判断：摆好之前 winfo_reqheight() 会虚高（按钮折行成几百像素），
        # 那种值一旦被当成收起高度焊死，界面就会在启动时明显抽搐一下。
        try:
            need = self.settings_bar.winfo_reqheight() + 6
            if MAX_COLLAPSED_HEIGHT >= need > self._collapsed_h:
                self._collapsed_h = need
                self.paned.paneconfigure(self.settings_frame, minsize=need)
        except tk.TclError:
            pass
        # 高度只差一两个像素 = 我们自己挪分隔条造成的抖动，直接忽略
        if not force and abs(total - self._placed_h) <= JITTER_TOLERANCE:
            return
        want = (self._preferred_settings_height(total) if self.settings_expanded
                else self._collapsed_h)
        # 窗口不够高时别让设置区把名单挤没
        want = min(want, max(self._collapsed_h, total - MIN_LIST_HEIGHT))
        try:
            # 用 pane 自己的 height 来定高度，**不用 sash_place**：
            # 首帧时 pane 还没真正布局（winfo_height()==1），此时 sash_place 会被
            # Tk 夹到"请求高度"附近的一个错位置 —— 那就是启动时看得见的那下窜动。
            # paneconfigure(height=) 不受这个影响，Tk 下一次布局就摆正，
            # 而且窗口缩放时列表会自己吃掉增减量，设置区保持这一高度。
            self.paned.paneconfigure(self.settings_frame, height=want)
            self._placed_h = total
        except tk.TclError:
            pass

    def _preferred_settings_height(self, total: int) -> int:
        need = SETTINGS_HEIGHT
        page = self.settings_pages.get(self.settings_page)
        if page is not None:
            height = page.winfo_reqheight()
            # height<=1 说明控件还没完成尺寸推导 —— 此时别按内容算，
            # 否则会瞬间塌到下限（等 60ms 后那次重算才纠正，会闪一下）
            if height > 1:
                need = (self.settings_bar.winfo_reqheight()
                        + height + 24)
        cap = max(SETTINGS_HEIGHT, int(total * 0.6))
        return max(self._collapsed_h + 60, min(need, cap))

    def _on_paned_configure(self, event):
        if event.widget is not self.paned or event.height == self._last_paned_h:
            return
        self._last_paned_h = event.height
        # 延到下一轮空闲再算：刚收到 Configure 时 PanedWindow 还没真正布局完
        self._schedule_resync(0)

    def _on_sash_released(self, _event=None):
        """用户手动拖过分隔条后，同步折叠按钮的文字。"""
        try:
            y = self.paned.sash_coord(0)[1]
        except tk.TclError:
            return
        expanded = (self.paned.winfo_height() - y) > (SETTINGS_COLLAPSED_HEIGHT + 30)
        if expanded != self.settings_expanded:
            self.settings_expanded = expanded
            self.settings_toggle_btn.configure(
                text="▾ 收起设置" if expanded else "▴ 展开设置")

    # ---- 设置页：扫描与触发 ----
    def _page_scan(self, parent):
        left = ttk.Frame(parent)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(parent)
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))

        if self.hotkey_config is None:
            ttk.Label(left, text="热键配置不可用", style="Muted.TLabel").pack(
                anchor="w")
        else:
            from app.ui.hotkey_dialog import HotkeyPanel
            # show_enabled=False：开关在顶部状态带上，这里只负责改键
            self.hotkey_panel = HotkeyPanel(
                left, self.hotkey_config, on_saved=self._on_hotkey_saved,
                embedded=True, show_enabled=False)
            self.hotkey_panel.pack(fill="both", expand=True)

        box = ttk.LabelFrame(right, text="扫描节流与去重", padding=10)
        box.pack(fill="x")
        rows = (
            ("命中去重窗口", f"{config.HIT_DEDUP_WINDOW} 秒"),
            ("抓屏间隔", f"{config.ESC_SESSION['interval']} 秒"),
            ("画面没变时跳过 OCR",
             "开" if config.SESSION_SKIP_UNCHANGED else "关"),
            ("画面静止多少帧收工", f"{config.SESSION_MAX_STATIC_FRAMES} 帧"),
            ("模糊匹配阈值", f"{config.MATCH_THRESHOLD} 分"),
        )
        for i, (label, value) in enumerate(rows):
            ttk.Label(box, text=f"{label}：").grid(row=i, column=0, sticky="w",
                                                  pady=2)
            ttk.Label(box, text=value).grid(row=i, column=1, sticky="w",
                                            padx=(8, 0), pady=2)
        ttk.Label(box, text="这些是内置节流参数，改 config.py 后重启生效。",
                  style="Muted.TLabel").grid(row=len(rows), column=0,
                                             columnspan=2, sticky="w",
                                             pady=(6, 0))
        ttk.Button(right, text="清空命中去重缓存",
                   command=self._clear_dedup_cache).pack(anchor="w",
                                                         pady=(10, 0))

    # ---- 设置页：通知 ----
    def _page_notify(self, parent):
        if self.notifier is None:
            ttk.Label(parent, text="通知模块不可用", style="Muted.TLabel").pack(
                anchor="w")
            return
        from app.ui.gui_notification import NotificationPanel
        self.notify_panel = NotificationPanel(
            parent, self.notification_config, self.notifier, embedded=True,
            on_saved=lambda: self._flash_status("通知设置已保存"))
        self.notify_panel.pack(fill="both", expand=True)

    # ---- 设置页：监视区域 ----
    def _page_region(self, parent):
        from app.ui.calibrator import CalibratorPanel
        self.region_panel = CalibratorPanel(
            parent, self.region_config, self.capture, embedded=True,
            on_changed=lambda: self._flash_status("区域配置已更新"))
        self.region_panel.pack(fill="both", expand=True)

    # ---- 设置页：数据 ----
    def _page_data(self, parent):
        ttk.Label(parent, text=f"数据目录：{config.DATA_DIR}",
                  style="Muted.TLabel", wraplength=620,
                  justify="left").pack(anchor="w")
        ttk.Label(parent, textvariable=self.data_info_var,
                  style="Muted.TLabel", justify="left").pack(anchor="w",
                                                             pady=(6, 0))
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="打开数据目录",
                   command=self.open_data_dir).pack(side="left")
        ttk.Button(row, text="打开日志", command=self.open_log).pack(
            side="left", padx=6)
        ttk.Button(row, text="刷新统计", command=self._refresh_data_info).pack(
            side="left", padx=6)
        ttk.Label(parent, text="导入 / 导出在名单工具条上。",
                  style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
        self._refresh_data_info()

    def _refresh_data_info(self):
        """数据页的只读统计：数据库条数/体积 + 证据图数量。"""
        parts = []
        try:
            parts.append(f"黑名单 {len(self.db.get_all())} 条")
        except Exception:                                # noqa: BLE001
            pass
        try:
            size = os.path.getsize(config.DB_PATH) / 1024.0
            parts.append(f"数据库 {size:.0f} KB")
        except OSError:
            pass
        try:
            shots = len(os.listdir(config.EVIDENCE_DIR))
            parts.append(f"证据截图 {shots} 张")
        except OSError:
            pass
        self.data_info_var.set("　·　".join(parts) if parts else "（暂无数据）")

    # ---- 设置页：帮助 ----
    def _page_help(self, parent):
        # 动作放最上面：正文较长时按钮也不会被挤到可视区外
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Button(row, text="使用说明", command=self.show_help).pack(
            side="left")
        ttk.Button(row, text="关于", command=self.show_about).pack(
            side="left", padx=6)
        ttk.Label(parent, text=HELP_TEXT, style="Muted.TLabel",
                  justify="left").pack(anchor="w", pady=(10, 0))

    # ---------------------------------------------------------------- 列表
    def _build_tree(self, parent):
        p = theme.PALETTE
        frm = ttk.Frame(parent)
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
        self._ctx_iid = None

    def _build_statusbar(self):
        """⑤ 状态栏：上面是「最近动态」，下面是当前状态 + 统计。

        为什么要有「最近动态」这一块：原来只有一行会被立刻覆盖的提示，
        用户按了 F8、命中了、被去重跳过了，界面上根本留不下痕迹，只能去翻
        app.log（实测反馈）。现在每次触发/命中/去重/失败都会带时间戳留下来，
        最新的一条在最上面。
        """
        wrap = ttk.Frame(self.root, style="Panel.TFrame")
        wrap.pack(fill="x", side="bottom")
        ttk.Separator(self.root, orient="horizontal").pack(fill="x",
                                                           side="bottom")

        # ---- 最近动态（始终占满 EVENT_ROWS 行，高度稳定，不参与抖动）----
        feed = ttk.Frame(wrap, style="Panel.TFrame", padding=(10, 4, 10, 0))
        feed.pack(fill="x")
        self._events = collections.deque(maxlen=EVENT_ROWS)
        self.event_vars = []
        self.event_labels = []
        for _i in range(EVENT_ROWS):
            var = tk.StringVar(value="")
            lbl = ttk.Label(feed, textvariable=var, anchor="w",
                            style="PanelMuted.TLabel")
            lbl.pack(fill="x")
            self.event_vars.append(var)
            self.event_labels.append(lbl)
        self.event_vars[0].set("（暂无动态）")

        # ---- 当前状态 + 统计 ----
        bar = ttk.Frame(wrap, style="Panel.TFrame", padding=(10, 5))
        bar.pack(fill="x")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var, anchor="w",
                  style="PanelMuted.TLabel").pack(side="left")

        # 统计放状态栏右侧：顶部 ① 只留"状态 + 动作"，避免最小宽度下被挤扁
        self.stats_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.stats_var, anchor="e",
                  style="Panel.TLabel").pack(side="right")

    # ------------------------------------------------------------ 动态流
    def notify_event(self, text: str, level: str = "info"):
        """往「最近动态」里加一条（**只能在主线程调用**）。

        level: info / hit / dedup / warn —— 决定这一行的颜色。
        后台线程请用 `post(lambda: gui.notify_event(...))`，或者让
        ScanScheduler.report() 经 app 层的 `_gui_call` 转过来。
        """
        if self._closing:
            return
        self._events.appendleft((time.strftime("%H:%M:%S"), str(text), level))
        self._render_events()

    def _render_events(self):
        styles = {"hit": "Hit.TLabel", "dedup": "Paused.TLabel",
                  "warn": "Danger.TLabel"}
        for i, var in enumerate(self.event_vars):
            if i < len(self._events):
                ts, text, level = self._events[i]
                var.set(f"{ts}  {text}")
                try:
                    self.event_labels[i].configure(
                        style=styles.get(level, "PanelMuted.TLabel"))
                except tk.TclError:
                    pass
            else:
                var.set("")

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
        return (row.get("player_name") or "", row.get("note") or "",
                row.get("created_at") or "")

    def refresh(self):
        self._load_data()

    # ------------------------------------------------------------ 排序搜索
    def sort_by_column(self, col):
        if col not in ("player_name", "note", "created_at"):
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
        """命中后由主线程更新界面：闪烁那一行 + 状态栏 + 动态流。

        名单里已经没有「遇到次数 / 最后遇见」这些列了，所以这里不写单元格，
        只把命中那一行闪一下，具体事实（时间 / 匹配度 / 来源 / 证据图）
        记在数据库的 encounters 表与 app.log 里。
        """
        iid = str(data.get("entry_id"))
        if self.tree.exists(iid):
            self._flash_row(iid, times=config.FLASH_TIMES)
        self.session_hit_count += 1
        self._refresh_stats()
        name = data.get("player_name") or "未知"
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
        """供 app 层更新「游戏在不在跑」——注意这里只说游戏状态。

        指示灯最终显示什么由 _render_monitor() 决定：手动暂停优先于游戏状态，
        这样"游戏没开 → 点暂停 → 灯变监控中"那种自相矛盾不会出现。
        """
        self.game_active = bool(active)
        self.game_reason = reason
        self._render_monitor()

    def _render_monitor(self):
        """按 (游戏是否在跑, 用户是否暂停) 两个真值渲染指示灯。"""
        if self.paused:
            reason = "手动暂停"
            if not self.game_active and self.game_reason:
                reason = f"手动暂停；{self.game_reason}"
            self.monitor_var.set(f"● 已暂停 ({reason})")
            self.monitor_label.configure(style="Paused.TLabel")
        elif not self.game_active:
            self.monitor_var.set(
                f"● 已暂停{(' (' + self.game_reason + ')') if self.game_reason else ''}")
            self.monitor_label.configure(style="Paused.TLabel")
        else:
            self.monitor_var.set("● 监控中")
            self.monitor_label.configure(style="Hit.TLabel")

    def toggle_pause(self):
        """[暂停监控]/[恢复监控]：切换**用户暂停**开关。

        注意必须自己取反：旧实现是读 pause_var，而主界面这个按钮从来不去写
        它 —— 于是每次点都只发一次"恢复"，永远暂停不上（用户实测的那个 bug）。
        """
        self.set_paused(not self.paused)

    def set_paused(self, paused: bool, notify: bool = True):
        self.paused = bool(paused)
        self.pause_var.set(self.paused)
        if notify and self.on_pause:
            self.on_pause(self.paused)
        self._render_monitor()
        self.set_status("已暂停监控" if self.paused else "已恢复监控")
        self._sync_pause_ui()

    def _sync_pause_ui(self):
        """顶部 [暂停监控] 按钮的文字/样式跟着状态走。"""
        btn = getattr(self, "pause_btn", None)
        if btn is None:
            return
        try:
            btn.configure(text="恢复监控" if self.paused else "暂停监控",
                          style="Accent.TButton" if self.paused else "Bar.TButton")
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- 设置
    def open_calibrator(self):
        """打开设置区的「监视区域」分页（原来是个独立对话框）。"""
        self.show_settings_page("region")

    def reset_regions(self):
        if not messagebox.askyesno("确认", "恢复全部监视区域为默认坐标？",
                                   parent=self.root):
            return
        self.region_config.reset_all()
        self.set_status("全部区域已恢复默认")

    def open_notification_settings(self):
        """打开设置区的「通知」分页（原来是个独立对话框）。"""
        if self.notifier is None:
            messagebox.showinfo("提示", "通知模块不可用", parent=self.root)
            return
        self.show_settings_page("notify")

    # ------------------------------------------------------- 聊天框按需扫描
    def scan_chat_now(self):
        """[扫描聊天框]：抓一张聊天框截图、跑一次 OCR、匹配黑名单。

        OCR 放到后台线程，避免阻塞 UI；结果由 ChatScanner.on_result 回调
        回到主线程播报（和 F8 热键走的是同一条路）。
        """
        if self.chat_scanner is None:
            self._flash_status("聊天框扫描不可用（未接入 ChatScanner）")
            self.notify_event("聊天框扫描不可用（未接入 ChatScanner）", "warn")
            return
        if self.chat_scanner.is_busy():
            self._flash_status("上一次聊天框扫描还没结束，已忽略本次点击")
            self.notify_event("上一次聊天框扫描还没结束，本次点击已忽略",
                              "warn")
            return
        threading.Thread(target=self._scan_chat_worker, daemon=True,
                         name="ChatScan").start()

    def _scan_chat_worker(self):
        try:
            self.chat_scanner.scan_now()
        except Exception as e:                           # noqa: BLE001
            # 正常路径下结果由 ChatScanner.on_result 上报；这里只兜住
            # "scan_now 自己炸了"这种情况，否则界面上会什么都没有。
            # 注意：不能把 e 直接闭包进 lambda —— except 块结束后这个名字
            # 会被删掉，延迟执行的回调只会拿到 NameError（踩过）。
            message = str(e)
            self.log.warning("聊天框扫描线程异常: %s", message)
            self.post(lambda: self._on_scan_chat_done(
                {"status": "error", "error": message}))

    def _chat_scan_event(self, result: dict):
        """ChatScanner.on_result 回调 —— **在扫描线程里**被调用。

        这里只做一件事：把结果排进命令队列，真正的界面更新在主线程。
        """
        self.post(lambda: self._on_scan_chat_done(result))

    def _scan_source_label(self, result: dict) -> str:
        """把来源翻成一句人话：按 F8 / 点按钮 / 热键名。"""
        source = result.get("source") or ""
        if source == config.CHAT_SCAN_HOTKEY_SOURCE:
            name = "热键"
            if self.hotkey_config is not None:
                try:
                    name = self.hotkey_config.display or "热键"
                except Exception:                        # noqa: BLE001
                    pass
            return f"按 {name}"
        if source == config.CHAT_SCAN_SOURCE:
            return "点 [扫描聊天框]"
        return "聊天框扫描"

    def _scan_chat_message(self, result: dict) -> tuple:
        """把一次聊天框扫描的结果翻成（给人看的一句话, 级别）。"""
        status = result.get("status")
        hits = int(result.get("hits") or 0)
        ms = float(result.get("elapsed_ms") or 0)
        label = self._scan_source_label(result)

        if status == "started":
            return (f"{label}：开始扫描聊天框…", "info")
        if status == "busy":
            return ("上一次扫描尚未完成，本次已跳过", "warn")
        if status == "empty":
            return (f"{label}：聊天框扫描完成，但聊天框为空"
                    f"（没识别到文字，{ms:.0f}ms）", "info")
        if status == "no_hit":
            return (f"{label}：聊天框扫描完成，未命中黑名单（{ms:.0f}ms）",
                    "info")
        if status == "ok":
            names = "、".join(str(n) for n in (result.get("names") or []))
            recorded = int(result.get("recorded") or 0)
            deduped = int(result.get("deduped") or 0)
            head = f"{label}：聊天框扫描完成，命中 {hits} 条"
            if names:
                head += f"：{names}"
            if recorded and deduped:
                detail = (f"{recorded} 条已提示并记录，"
                          f"{deduped} 条在 {config.HIT_DEDUP_WINDOW} 秒去重窗口内已跳过")
                return (f"{head}（{detail}，{ms:.0f}ms）", "hit")
            if recorded:
                return (f"{head}（已提示 + 已记录，{ms:.0f}ms）", "hit")
            if deduped:
                return (f"{head}（{config.HIT_DEDUP_WINDOW} 秒内已提示过 → "
                        f"本次跳过，未重复计数，{ms:.0f}ms）", "dedup")
            # 命中了但一条都没落库（数据库被占用之类）
            return (f"{head}（命中但全部写入失败，详见日志，{ms:.0f}ms）",
                    "warn")
        if status == "error":
            return (f"{label}：聊天框扫描失败：{result.get('error', '未知错误')}",
                    "warn")
        if status == "ocr_unavailable":
            return (f"{label}：OCR 引擎不可用，扫描不会有结果 —— "
                    f"{result.get('error', '未安装 rapidocr')}", "warn")
        return ("聊天框扫描完成", "info")

    def _on_scan_chat_done(self, result: dict):
        text, level = self._scan_chat_message(result)
        if result.get("status") == "started":
            # "正在扫"只是个过渡态，别把结果行顶掉，所以不进动态流
            self.set_status(text)
            return
        self._flash_status(text, 6000)
        self.notify_event(text, level)

    def _clear_dedup_cache(self):
        if self.scheduler is None:
            return
        self.scheduler.clear_dedup_cache()
        self._flash_status("已清空命中去重缓存（立即可以重新计数）")

    def toggle_hotkey(self):
        """① 上的 [启用/停用 X 扫描]：切换热键开关。

        必须自己取反。旧实现是读 chat_hotkey_var（那是菜单时代留给
        Checkbutton 用的），而主界面这个按钮从来不去写它 —— 于是点一次提交
        一次"当前值"，按钮永远启动不了热键（和暂停按钮同一个坑）。
        """
        self.set_hotkey_enabled(not bool(self.chat_hotkey_var.get()))

    def set_hotkey_enabled(self, enabled: bool):
        """写盘并通知 app 重启热键监听。"""
        if self.hotkey_config is None:
            self._flash_status("热键配置不可用")
            self._sync_hotkey_ui()          # 把按钮状态还原，别骗用户
            return
        self.chat_hotkey_var.set(bool(enabled))
        cfg = self.hotkey_config.set_enabled(bool(enabled))
        self._notify_hotkey_changed(cfg)

    def open_hotkey_settings(self):
        """打开设置区的「扫描与触发」分页（改键在那里）。"""
        if self.hotkey_config is None:
            self._flash_status("热键配置不可用")
            return
        self.show_settings_page("scan")

    def _on_hotkey_saved(self, cfg: dict):
        self.chat_hotkey_var.set(bool(cfg.get("enabled")))
        self._sync_hotkey_ui()
        self._notify_hotkey_changed(cfg)

    def _notify_hotkey_changed(self, cfg: dict):
        self._sync_hotkey_ui()
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

    def _sync_hotkey_ui(self):
        """同步 ① 上的热键开关按钮。

        按钮文字写的是**动作**（点下去会发生什么），配上当前按键名：
            未启用 → 「启用 F8 扫描」（普通按钮样式）
            已启用 → 「停用 F8 扫描」（金色 = 正在生效）
        这样"到底开没开、点了会怎样"一眼就知道，不用翻菜单。
        """
        enabled = bool(self.chat_hotkey_var.get())
        name = "热键"
        if self.hotkey_config is not None:
            try:
                name = self.hotkey_config.display or "热键"
            except Exception:                            # noqa: BLE001
                name = "热键"
        btn = getattr(self, "hotkey_btn", None)
        if btn is not None:
            try:
                btn.configure(
                    text=f"停用 {name} 扫描" if enabled else f"启用 {name} 扫描",
                    style="Accent.TButton" if enabled else "Bar.TButton")
            except tk.TclError:
                pass
        # 设置区里那个按键框也必须跟着变。否则会出现这种自相矛盾：
        # 顶部写着「停用 F8 扫描」（= 正在生效），设置页里却是「F8（未启用）」
        # —— 因为那个面板开机时按当时的 enabled=False 渲染，之后再没刷新过。
        panel = getattr(self, "hotkey_panel", None)
        if panel is not None:
            try:
                panel.refresh_enabled(enabled)
            except Exception as e:                        # noqa: BLE001
                self.log.warning("刷新自定义快捷键面板失败: %s", e)

    #: 兼容旧名字（菜单时代的叫法）
    _sync_hotkey_menu_label = _sync_hotkey_ui

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
            # 只转发同一条命令（不要在这里先翻 pause_var，否则会翻两次）
            self.post(self.toggle_pause)

        def _scan(icon=None, item=None):
            self.post(self.scan_chat_now)

        # 托盘只是"同一个命令的另一个入口"，不重复实现任何逻辑
        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", _show, default=True),
            pystray.MenuItem("扫描聊天框", _scan),
            pystray.MenuItem("暂停/恢复监控", _pause),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开设置：扫描与触发",
                             lambda i, it: self.post(self.open_hotkey_settings)),
            pystray.MenuItem("打开设置：监视区域",
                             lambda i, it: self.post(self.open_calibrator)),
            pystray.MenuItem("打开设置：通知",
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
        jobs = ([self._drain_job, self._flash_job,
                 getattr(self, "_resync_job", None)]
                + list(self._flash_jobs.values()))
        for job in jobs:
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:                        # noqa: BLE001
                    pass
        self._flash_jobs.clear()
        self._drain_job = None
        self._flash_job = None
        self._resync_job = None

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

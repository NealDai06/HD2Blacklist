# -*- coding: utf-8 -*-
"""gui.py —— 主界面。

布局（单页五段，每个功能只有一个入口）：
    ① 运行状态带   应用标识 + 监控灯 + 追踪状态 + [最近遇到]/[暂停]/[退出]
    ② 名单工具带   增删改查 + 搜索 + 排序 + 导入导出
    ③ 名单主区     表格（占满剩余空间）
    ④ 设置分页     最近遇到 / 通知 / 数据 / 帮助（默认收起，可拖拽分隔）
    ⑤ 状态栏       「最近动态」事件流（3 行，带时间戳）+ 当前状态 + 统计

没有菜单栏：菜单里曾经和工具栏重复的入口全部收敛到 ①②④，避免"同一个设置
在两处出现、两处不一致"。

红线：**所有 tkinter 操作都在主线程**。
后台线程（尾随插件日志的线程 / 托盘）只能往 `_command_queue` 里塞可调用对象
（`post()`），主线程用 root.after(GUI_QUEUE_POLL_MS) 周期性排空它。
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
from app.core.database import format_prev_names
from app.ui import theme
from app.config import GUI_QUEUE_POLL_MS, get_logger

#: 名单的列。最后那个 `_fill` 是**占位的空白列**，不显示任何数据 ——
#: 它存在的唯一理由是"给最后一列让出可以拖宽的余地"，见 _ColumnFitter 的说明。
COLUMNS = ("player_name", "peer_id", "prev_names", "note", "created_at",
           "_fill")
HEADERS = {"player_name": "名称", "peer_id": "PeerID", "prev_names": "曾用名",
           "note": "备注", "created_at": "添加时间", "_fill": ""}
#: 各列默认宽度（像素）。时间列（190）和 PeerID（170）是**实测**出来的：
#: 这台机器上 `2026-12-31 23:59:59` 渲染要 169px、16 位十六进制 ID 要 142px，
#: 而 Tk 自己的 `font.measure()` / `Label.winfo_reqwidth()` 只报 ~120/~100
#: （DPI 缩放没算进去）—— 照 Tk 的数字定宽度会让它们被切掉末尾（实测踩过）。
#: 反正每一列都能自己拖，这里只管"默认好看、不截断"。
WIDTHS = {"player_name": 210, "peer_id": 190, "prev_names": 165,
          "note": 250, "created_at": 200, "_fill": 40}
#: 每列的宽度下限（防止被拖成一条缝，再也抓不住）。
#: `_fill` 的下限就是"最后一列还能往右拖多少" —— 别设成 0。
MIN_WIDTHS = {"player_name": 60, "peer_id": 120, "prev_names": 50,
              "note": 50, "created_at": 120, "_fill": 16}

SORT_OPTIONS = (("created_at", "按添加时间"), ("player_name", "按名称"))

#: 「最近遇到」列表的列（来源：数据库 seen_players，由插件日志喂进来）
RECENT_COLUMNS = ("name", "peer_id", "last_seen", "games", "state", "_fill")
RECENT_HEADERS = {"name": "玩家名", "peer_id": "PeerID",
                  "last_seen": "最近遇到", "games": "遇到局数",
                  "state": "状态", "_fill": ""}
#: 同样按**实测渲染**给：16 位 PeerID 要 142px、时间串要 181px，
#: 「遇到局数」表头自己就要 63px（原来给 80 会把它切掉）
RECENT_WIDTHS = {"name": 200, "peer_id": 195, "last_seen": 210, "games": 112,
                 "state": 106, "_fill": 40}
RECENT_MIN_WIDTHS = {"name": 60, "peer_id": 130, "last_seen": 120, "games": 60,
                     "state": 60, "_fill": 16}

#: 占位空白列的键名（不显示数据、不参与排序）
FILLER_COLUMN = "_fill"


class _ColumnFitter:
    """列宽助手：所有真实列都是 **stretch=False**（宽度完全由用户拖出来），
    外加一列**空白占位列**在最后，用来"吃掉剩下的宽度"。

    为什么需要它（真实反馈：**最后一列只能缩小、不能放大**）：
      · Tk 里拖分隔条**只改左边那一列的宽度**，所以想放大最后一列，只能拖
        它**自己的右边缘**；
      · 而如果所有列加起来正好等于控件宽度，那个右边缘就贴在控件右边界上 ——
        指针得拖到控件外面才有效果；窗口最大化时外面就是屏幕边缘，**拖不动**。
        于是结论就是"只能缩，不能放"。
      · Tk 自带的 `stretch=True` 也救不了：它在拖分隔条时同样会重算，
        刚拖出来的宽度会被 stretch 列立刻抢回去（实测：抓"备注/添加时间"
        之间的分隔条往左拖，备注缩了、最后一列一动不动）。

    有了一列会被拉伸的空白列就不一样了：最后一列的右边缘**永远在控件内部**，
    往右拖时空白列同步让位，表格还始终是填满的。
    """

    def __init__(self, tree, columns, min_widths, filler=FILLER_COLUMN):
        self.tree = tree
        self.columns = tuple(columns)
        self.reals = tuple(c for c in self.columns if c != filler)
        self.min_widths = dict(min_widths)
        self.filler = filler
        self._w = 0
        tree.bind("<Configure>", self._on_configure, add="+")
        # 拖完分隔条松手时也重算一次：把差额记到空白列头上
        tree.bind("<ButtonRelease-1>", self._on_release, add="+")

    # ------------------------------------------------------------------ 内部
    def _on_configure(self, event):
        self._w = int(event.width)
        self.apply(self._w)

    def _on_release(self, _event=None):
        self.apply()

    # ------------------------------------------------------------------ 对外
    def apply(self, total=None) -> int:
        """把空白列设成"剩下那点宽度"，让表格始终填满控件。

        用户拖宽某一列 → 空白列让位（这就是"最后一列能放大"的机制）；
        拖窄某一列 / 缩放窗口 → 空白列补回来。空白列自己有下限，不会消失。
        """
        try:
            total = int(total if total else self.tree.winfo_width())
        except tk.TclError:
            return 0
        if total <= 1:
            return 0
        used = 0
        for col in self.reals:
            try:
                used += int(self.tree.column(col, "width"))
            except tk.TclError:
                return 0
        floor = int(self.min_widths.get(self.filler, 16))
        want = max(floor, total - used - 2)      # 2px 给边框，免得撑出多余滚动条
        try:
            if int(self.tree.column(self.filler, "width")) != want:
                self.tree.column(self.filler, width=want)
        except tk.TclError:
            return 0
        return want

# --------------------------------------------------------------------------
# 布局常量
# --------------------------------------------------------------------------
#: 默认窗口尺寸与最小尺寸（原来 1120x660 装不下「名单 + 展开的设置区」）
DEFAULT_WIDTH, DEFAULT_HEIGHT = 1280, 820
#: 1060 = 名单工具带自然宽度（约 1030）+ 余量；再窄按钮就会被 pack 挤扁
MIN_WIDTH, MIN_HEIGHT = 1060, 640

#: 下方设置区分页（key, 标签）。顺序 = 标签栏从左到右的顺序
SETTINGS_TABS = (
    ("recent", "最近遇到"),
    ("notify", "通知"),
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
    "数据来源：游戏内插件 HD2Tracker 写出的 playerLog.txt\n"
    "（%LOCALAPPDATA%\\CowboyBingus\\Helldivers2\\Logs\\）。\n"
    "本应用**只读这一个文本文件**：不注入、不读内存、不改包、不抢焦点。\n\n"
    "提醒规则：有人**刚进队**（join）且 **PeerID 命中黑名单**，才弹提示。\n"
    f"  · 同一个人在同一局里 {config.HIT_DEDUP_WINDOW} 秒内只提醒一次；\n"
    "  · 队伍快照只刷新「现在队里有谁」，绝不触发提醒；\n"
    "  · 你自己永远不会被当成目标；\n"
    "  · **只认 PeerID**：没有 PeerID 的老条目不会提醒（名字会重名、会改，\n"
    "    拿它当身份会认错人）—— 右键 [补全 PeerID] 或从「最近遇到」重新加入。\n\n"
    "最近遇到：插件每读到一次名册就把队里的人记下来，\n"
    "  于是在「最近遇到」里能直接把他们加进名单 —— 名字和 PeerID\n"
    "  都是程序填的，你不用手打，也就不会填错。\n"
    "  · [从列表移除] 只是把这一条划掉（下次遇到照旧记录、照旧提醒）；\n"
    "  · [忽略此人] 是记进忽略名单，以后既不再提醒、也不再进列表 ——\n"
    "    反悔入口在「数据」页 → [忽略名单…]。\n\n"
    "PeerID 是 PlayFab 的账户标识，跨局稳定、改名字也不变 ——\n"
    "  所以它是**身份**：同名不同 ID 是两个人（各留一条），同一个人改名\n"
    "  只会更新名字。提醒也只认 PeerID，没有 ID 的条目不会提醒。\n\n"
    "重复打开不会多开进程：会把已经运行的那个窗口叫到前台。\n"
    "关闭窗口 = 最小化到托盘，追踪继续；要退出请点顶部 [退出]。"
)


# ==========================================================================
class EntryDialog:
    """新增 / 编辑黑名单条目。

    PeerID 一栏是**只读**的：它由「最近遇到」带进来，或者由程序写入。
    用户在界面上没有地方手打 ID，所以再也不会出现 v1.1.2 那种
    「把名字填进 ID 栏、名称栏留空，于是怎么都不命中」的错。
    """

    def __init__(self, master, title="添加黑名单", entry=None):
        self.master = master
        self.result = None
        entry = entry or {}

        self.top = tk.Toplevel(master)
        self.top.title(title)
        self.top.transient(master)
        self.top.resizable(False, False)
        self.top.configure(bg=theme.PALETTE["bg"])
        theme.apply_window_icon(self.top)
        self.top.grab_set()

        self.vars = {
            "player_name": tk.StringVar(value=entry.get("player_name") or ""),
            "peer_id": tk.StringVar(value=entry.get("peer_id") or ""),
            "prev_names": tk.StringVar(
                value=format_prev_names(entry.get("prev_names"))),
        }

        frm = ttk.Frame(self.top, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="玩家名称：").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.vars["player_name"], width=38).grid(
            row=0, column=1, columnspan=2, sticky="we", pady=4)

        ttk.Label(frm, text="PeerID：").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.vars["peer_id"], width=38,
                  state="readonly").grid(row=1, column=1, sticky="we", pady=4)
        ttk.Button(frm, text="复制", width=6,
                   command=self._copy_peer).grid(row=1, column=2, padx=(6, 0))

        ttk.Label(frm, text="曾用名：").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.vars["prev_names"], width=38).grid(
            row=2, column=1, columnspan=2, sticky="we", pady=4)

        ttk.Label(frm, text="备注描述：").grid(row=3, column=0, sticky="nw", pady=4)
        self.note_text = tk.Text(frm, width=40, height=5, wrap="word")
        theme.style_text(self.note_text)
        self.note_text.grid(row=3, column=1, columnspan=2, sticky="we", pady=4)
        if entry.get("note"):
            self.note_text.insert("1.0", entry["note"])

        ttk.Label(
            frm,
            text=("有 PeerID = 精确命中（改名字也认得出）；没有则只能按名字匹配。\n"
                  "PeerID 由程序从插件日志里读出来，不用手填、也改不了。\n"
                  "曾用名可以自己填，多个用 / 分开；他改名时程序会自动往这里记。"),
            foreground="#888888", justify="left").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        bar = ttk.Frame(frm)
        bar.grid(row=5, column=0, columnspan=3, sticky="e", pady=(10, 0))
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

    def _copy_peer(self):
        value = self.vars["peer_id"].get()
        if not value:
            return
        try:
            self.top.clipboard_clear()
            self.top.clipboard_append(value)
        except tk.TclError:
            pass

    def _ok(self):
        data = {
            "player_name": self.vars["player_name"].get().strip(),
            "peer_id": self.vars["peer_id"].get().strip(),
            "prev_names": self.vars["prev_names"].get().strip(),
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


class RecentPickerDialog:
    """从「最近遇到」里挑一个人加入名单。

    存在的理由只有一个：**让程序把名字和 PeerID 填好**，用户只写备注。
    抓到的人可能已经改过名字，所以名字是"最近一次看到"的，可以手改；
    PeerID 才是判据。
    """

    COLUMNS = ("name", "peer_id", "last_seen", "games", "state")
    HEADERS = {"name": "玩家名", "peer_id": "PeerID", "last_seen": "最近遇到",
               "games": "遇到局数", "state": "状态"}
    WIDTHS = {"name": 200, "peer_id": 150, "last_seen": 140, "games": 80,
              "state": 120}

    def __init__(self, master, rows, existing_peers=()):
        self.master = master
        #: 选中的人（dict）；"manual" = 用户选了手动输入
        self.result = None
        self._rows = list(rows or [])
        self._existing = set(existing_peers or ())

        top = self.top = tk.Toplevel(master)
        top.title("从「最近遇到」里加入名单")
        top.transient(master)
        top.configure(bg=theme.PALETTE["bg"])
        theme.apply_window_icon(top)
        top.grab_set()
        top.geometry("820x460")
        top.minsize(680, 360)

        frm = ttk.Frame(top, padding=10)
        frm.pack(fill="both", expand=True)

        head = ttk.Frame(frm)
        head.pack(fill="x")
        ttk.Label(head, text="搜索：", style="PanelMuted.TLabel").pack(side="left")
        self.filter_var = tk.StringVar()
        ent = ttk.Entry(head, textvariable=self.filter_var, width=24)
        ent.pack(side="left", padx=4)
        ent.bind("<KeyRelease>", lambda e: self._fill())
        ttk.Label(head, text="（按名字或 PeerID 过滤；双击 = 加入名单）",
                  style="PanelMuted.TLabel").pack(side="left", padx=(8, 0))

        mid = ttk.Frame(frm)
        mid.pack(fill="both", expand=True, pady=(8, 0))
        self.tree = ttk.Treeview(mid, columns=self.COLUMNS, show="headings",
                                 selectmode="browse")
        for col in self.COLUMNS:
            self.tree.heading(col, text=self.HEADERS[col])
            self.tree.column(col, width=self.WIDTHS[col], minwidth=50,
                             anchor="w" if col == "name" else "center",
                             stretch=False)
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self._ok())

        foot = ttk.Frame(frm)
        foot.pack(fill="x", pady=(8, 0))
        self.hint_var = tk.StringVar(value="")
        ttk.Label(foot, textvariable=self.hint_var,
                  style="PanelMuted.TLabel").pack(side="left")

        ttk.Button(foot, text="取消", command=self._cancel).pack(side="right",
                                                               padx=4)
        ttk.Button(foot, text="加入名单", style="Accent.TButton",
                   command=self._ok).pack(side="right")
        ttk.Button(foot, text="手动输入…",
                   command=self._manual).pack(side="right", padx=4)

        self._fill()
        top.bind("<Escape>", lambda e: self._cancel())
        top.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - top.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - top.winfo_height()) // 3
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        top.wait_window()

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def state_of(peer_id: str, existing) -> str:
        """状态只看 PeerID：同名但不同 ID 是**另一个人**，不算"已在名单"。"""
        return "已在名单" if peer_id in existing else "未加入"

    def _display_name(self, row) -> str:
        return (row.get("name_last") or row.get("name_first") or "").strip()

    def _fill(self):
        self.tree.delete(*self.tree.get_children())
        needle = self.filter_var.get().strip().lower()
        shown = 0
        for row in self._rows:
            pid = row.get("peer_id") or ""
            name = self._display_name(row)
            if needle and needle not in name.lower() and needle not in pid.lower():
                continue
            self.tree.insert("", "end", iid=pid,
                             values=(name or "（无名）", pid,
                                     row.get("last_seen") or "",
                                     row.get("seen_count") or 1,
                                     self.state_of(pid, self._existing)))
            shown += 1
        self.hint_var.set(f"共 {len(self._rows)} 人，显示 {shown} 人")

    def _cancel(self):
        self.result = None
        self.top.destroy()

    def _manual(self):
        self.result = "manual"
        self.top.destroy()

    def _ok(self):
        sel = [iid for iid in self.tree.selection()]
        if not sel:
            messagebox.showinfo("提示", "请先选中一个玩家", parent=self.top)
            return
        by_peer = {r.get("peer_id"): r for r in self._rows}
        row = by_peer.get(sel[0])
        if row is None:
            return
        if sel[0] in self._existing:
            messagebox.showinfo("提示",
                                "这个人已经在名单里了（列表里标着「已在名单」）",
                                parent=self.top)
            return
        self.result = row
        self.top.destroy()


# ==========================================================================
# 名单导入导出（模块级函数，方便单独测试）
# ==========================================================================
#: 导出/导入字段顺序（与 database.export_all 一致）
#: `peer_id` 与 `prev_names` 一起走：换台机器导回来，PeerID 精确匹配照样有效，
#: 曾用名也不会丢（它在文件里是一串 JSON 数组，导入时原样带回来）。
IO_FIELDS = ("player_name", "note", "peer_id", "prev_names", "created_at")


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


class IgnoreListDialog:
    """忽略名单：看有哪些人、逐条取消忽略、或整体清空。

    「忽略」和「从列表移除」不是一回事：后者只是把这一条从「最近遇到」里
    划掉，下次遇到照旧记录、照旧提醒；忽略是记进一张表，之后既不提醒、
    也不再进列表 —— 所以**反悔的入口必须有**，就是这个对话框。
    """

    def __init__(self, master, db):
        self.db = db
        self.changed = False

        top = self.top = tk.Toplevel(master)
        top.title("忽略名单")
        top.transient(master)
        top.configure(bg=theme.PALETTE["bg"])
        theme.apply_window_icon(top)
        top.grab_set()
        top.geometry("640x400")
        top.minsize(520, 300)

        frm = ttk.Frame(top, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm,
                  text="下面这些人不会被提醒，也不会出现在「最近遇到」里。",
                  style="Muted.TLabel").pack(anchor="w")

        mid = ttk.Frame(frm)
        mid.pack(fill="both", expand=True, pady=(8, 0))
        self.tree = ttk.Treeview(mid, columns=("name", "peer_id", "added_at"),
                                 show="headings", selectmode="browse")
        for col, label, width in (("name", "玩家名", 180),
                                  ("peer_id", "PeerID", 160),
                                  ("added_at", "加入时间", 150)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width,
                             anchor="w" if col == "name" else "center",
                             stretch=(col == "name"))
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self._unignore())

        self.hint_var = tk.StringVar(value="")
        foot = ttk.Frame(frm)
        foot.pack(fill="x", pady=(8, 0))
        ttk.Label(foot, textvariable=self.hint_var,
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(foot, text="关闭", command=self._close).pack(side="right",
                                                              padx=4)
        ttk.Button(foot, text="取消忽略",
                   command=self._unignore).pack(side="right")
        ttk.Button(foot, text="清空忽略名单", style="Danger.TButton",
                   command=self._clear).pack(side="right", padx=4)

        self._fill()
        top.bind("<Escape>", lambda e: self._close())
        top.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - top.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - top.winfo_height()) // 3
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        top.wait_window()

    def _fill(self):
        self.tree.delete(*self.tree.get_children())
        rows = self.db.get_ignored()
        for row in rows:
            pid = row.get("peer_id") or ""
            self.tree.insert("", "end", iid=pid, values=(
                row.get("name") or "（无名）", pid, row.get("added_at") or ""))
        self.hint_var.set(f"共 {len(rows)} 人")

    def _unignore(self):
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先选中一个人（双击也行）",
                                parent=self.top)
            return
        for pid in sel:
            self.db.unignore_peer(pid)
        self.changed = True
        self._fill()

    def _clear(self):
        if not messagebox.askyesno(
                "确认清空",
                "清空整个忽略名单？\n（这些人以后会重新出现，也会重新提醒）",
                parent=self.top):
            return
        self.db.clear_ignored()
        self.changed = True
        self._fill()

    def _close(self):
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

    def __init__(self, db, notification_config, matcher=None, notifier=None,
                 watcher=None, on_quit=None, on_pause=None, app=None):
        self.db = db
        self.notification_config = notification_config
        self.matcher = matcher
        self.notifier = notifier
        self.watcher = watcher
        self.on_quit = on_quit
        self.on_pause = on_pause
        self.app = app
        self.log = get_logger("gui")

        # 跨线程通信（后台线程只能用 post() 排可调用对象到主线程）
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
        self._watch_job = None            # 「最近遇到」页的定时刷新
        self._recent_sig = None           # 上次渲染过的最近遇到列表指纹

        self.root = tk.Tk()
        self.root.title(config.WINDOW_TITLE)
        self.root.geometry(f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT}")
        self.root.minsize(MIN_WIDTH, MIN_HEIGHT)

        theme.apply_theme(self.root)
        theme.apply_window_icon(self.root)

        self.pause_var = tk.BooleanVar(value=False)
        self.data_info_var = tk.StringVar(value="")
        self.track_var = tk.StringVar(value="● 正在连接插件日志…")
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

        self._load_data()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._drain_job = self.root.after(GUI_QUEUE_POLL_MS, self._drain_queues)
        self._watch_job = self.root.after(400, self._tick_watch)

    # ------------------------------------------------------- ① 运行状态带
    def _build_header(self):
        """顶部一条：应用标识 + 追踪状态 + 高频动作。

        数据来源只有一个文本文件，所以这里不再有"扫描""热键"这类按钮；
        留下的是随时要看的东西：追踪指示灯、队伍人数、暂停、退出。
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
        ttk.Label(bar, textvariable=self.track_var,
                  style="PanelMuted.TLabel").pack(side="left", padx=(12, 0))

        # ---- 右：高频动作（pack 顺序 = 从右往左）----
        ttk.Button(bar, text="退出", style="Bar.TButton",
                   command=self.quit_app).pack(side="right")
        self.pause_btn = ttk.Button(bar, text="暂停追踪", style="Bar.TButton",
                                    command=self.toggle_pause)
        self.pause_btn.pack(side="right", padx=(0, 6))
        self.recent_btn = ttk.Button(bar, text="最近遇到", style="Accent.TButton",
                                     command=self.open_recent)
        self.recent_btn.pack(side="right", padx=(0, 6))
        ttk.Separator(bar, orient="vertical").pack(side="right", fill="y",
                                                   padx=10)

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
            "recent": self._page_recent,
            "notify": self._page_notify,
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
        # 每次进「最近遇到」都立刻对齐一次（列表 + 追踪状态）
        if key == "recent":
            try:
                self._refresh_recent(force=True)
            except Exception as e:                        # noqa: BLE001
                self.log.warning("刷新「最近遇到」失败: %s", e)
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

    # ---- 设置页：最近遇到 ----
    def _page_recent(self, parent):
        """追踪状态（只读）+ 「最近遇到」列表（可一键加入名单）。"""
        box = ttk.LabelFrame(parent, text="追踪状态", padding=10)
        box.pack(fill="x")
        self.track_detail_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.track_detail_var, justify="left",
                  style="Muted.TLabel", wraplength=980).pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="打开日志目录",
                   command=self.open_log_dir).pack(side="left")
        ttk.Button(row, text="打开 playerLog.txt",
                   command=self.open_player_log).pack(side="left", padx=6)
        ttk.Button(row, text="刷新",
                   command=lambda: self._refresh_recent(force=True)).pack(
            side="left")

        # 动作行放在**列表上面**：这一页内容比设置区高，按钮放下面会被滚出可视区
        # （实测截图确认过：加入名单那排按钮整个看不见了）。正文越长的页越要这样排。
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(10, 4))
        ttk.Button(bar, text="加入名单", style="Accent.TButton",
                   command=self.add_from_recent).pack(side="left")
        ttk.Button(bar, text="从列表移除",
                   command=self.forget_recent_selected).pack(side="left",
                                                             padx=6)
        ttk.Button(bar, text="忽略此人",
                   command=self.ignore_recent_selected).pack(side="left")
        ttk.Button(bar, text="手动添加…",
                   command=self.add_entry).pack(side="left", padx=6)
        ttk.Button(bar, text="列宽自适应",
                   command=self.reset_recent_columns).pack(side="left")
        self.recent_hint_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.recent_hint_var,
                  style="Muted.TLabel").pack(side="right")

        ttk.Label(parent,
                  text="最近遇到（插件每读到一次名册就记一次；双击那一行 = 加入名单）",
                  style="Muted.TLabel").pack(anchor="w", pady=(6, 2))
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        self.recent_tree = ttk.Treeview(wrap, columns=RECENT_COLUMNS,
                                        show="headings", selectmode="browse",
                                        height=7)
        for col in RECENT_COLUMNS:
            self.recent_tree.heading(col, text=RECENT_HEADERS[col])
            self.recent_tree.column(col, width=RECENT_WIDTHS[col],
                                    minwidth=RECENT_MIN_WIDTHS[col],
                                    anchor="w" if col == "name" else "center",
                                    stretch=False)
        self._recent_fitter = _ColumnFitter(self.recent_tree, RECENT_COLUMNS,
                                            RECENT_MIN_WIDTHS)
        vsb = ttk.Scrollbar(wrap, orient="vertical",
                            command=self.recent_tree.yview)
        self.recent_tree.configure(yscrollcommand=vsb.set)
        self.recent_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.recent_tree.bind("<Double-1>", lambda e: self.add_from_recent())

        self._refresh_recent(force=True)

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
        row2 = ttk.Frame(parent)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Button(row2, text="清空「最近遇到」", style="Danger.TButton",
                   command=self.clear_recent).pack(side="left")
        ttk.Button(row2, text="忽略名单…",
                   command=self.open_ignore_list).pack(side="left", padx=6)
        ttk.Label(parent, text="导入 / 导出在名单工具条上。",
                  style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
        self._refresh_data_info()

    def _refresh_data_info(self):
        """数据页的只读统计：名单条数、能告警几条、追踪记录、忽略名单、体积。"""
        parts = []
        try:
            entries = self.db.get_all()
            with_id = sum(1 for e in entries if e.get("peer_id"))
            parts.append(f"黑名单 {len(entries)} 条（可告警 {with_id} 条）")
            if len(entries) > with_id:
                parts.append(f"⚠ {len(entries) - with_id} 条没有 PeerID，"
                             "不会告警（右键[补全 PeerID]）")
        except Exception:                                # noqa: BLE001
            pass
        try:
            parts.append(f"最近遇到 {self.db.count_seen()} 人"
                         f"（{config.RECENT_DAYS} 天内 "
                         f"{self.db.count_seen(days=config.RECENT_DAYS)} 人）")
        except Exception:                                # noqa: BLE001
            pass
        try:
            parts.append(f"已忽略 {len(self.db.ignored_peers())} 人")
        except Exception:                                # noqa: BLE001
            pass
        try:
            size = os.path.getsize(config.DB_PATH) / 1024.0
            parts.append(f"数据库 {size:.0f} KB")
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
            # stretch=False 是有意的：宽度完全由用户拖出来，见 _ColumnFitter
            self.tree.column(col, width=WIDTHS[col],
                             minwidth=MIN_WIDTHS[col],
                             anchor="center" if col != "note" else "w",
                             stretch=False)
        self._column_fitter = _ColumnFitter(self.tree, COLUMNS, MIN_WIDTHS)

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
        self.menu.add_command(label="复制名称", command=self.copy_name)
        self.menu.add_command(label="复制 PeerID", command=self.copy_peer_id)
        self.menu.add_command(label="补全 PeerID", command=self.fill_peer_id)
        self.menu.add_separator()
        self.menu.add_command(label="列宽自适应", command=self.reset_column_widths)
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
        # 最后一列是占位空白列，永远给空串；曾用名存的是 JSON，显示成一行
        return (row.get("player_name") or "", row.get("peer_id") or "",
                format_prev_names(row.get("prev_names")),
                row.get("note") or "", row.get("created_at") or "", "")

    def refresh(self):
        self._load_data()

    # ------------------------------------------------------------ 排序搜索
    def sort_by_column(self, col):
        if col not in ("player_name", "peer_id", "note", "created_at"):
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
        """添加黑名单。

        默认先让用户在「最近遇到」里挑人 —— 名字和 PeerID 由程序填好，
        只需要写备注；也可以选「手动输入」走老路（只填名字，只能按名字匹配）。
        """
        rows = self._recent_rows()
        if rows:
            existing = set()
            try:
                existing = set(self.db.blacklist_peers())
            except Exception:                            # noqa: BLE001
                pass
            dlg = RecentPickerDialog(self.root, rows, existing_peers=existing)
            if dlg.result is None:
                return                                   # 取消 = 什么都不做
            if dlg.result != "manual":
                self._add_from_seen(dlg.result)
                return
        self._add_manual()

    def _add_manual(self):
        dlg = EntryDialog(self.root, "添加黑名单条目")
        if not dlg.result:
            return
        self._commit_add(dlg.result)

    def _add_from_seen(self, row):
        """从「最近遇到」加入名单：名字与 PeerID 预填好，只让用户写备注。"""
        peer_id = row.get("peer_id") or ""
        name = (row.get("name_last") or row.get("name_first") or "").strip()
        dlg = EntryDialog(self.root, "加入名单",
                          {"player_name": name or peer_id, "peer_id": peer_id})
        if not dlg.result:
            return
        if self._commit_add(dlg.result) is not None:
            self._refresh_recent(force=True)

    def _commit_add(self, data):
        """写进名单。同一个 PeerID 已经存在时不会新增，而是更新那一条的名字。"""
        before = self.db.get_count()
        try:
            entry_id = self.db.add(data.get("player_name") or "",
                                   data.get("note") or "",
                                   peer_id=data.get("peer_id") or "",
                                   prev_names=data.get("prev_names") or "")
        except ValueError as e:
            messagebox.showerror("添加失败", str(e), parent=self.root)
            return None
        self._after_db_change()
        if self.db.get_count() > before:
            self.set_status(f"已添加条目 #{entry_id}")
        else:
            self.set_status(f"这个 PeerID 已在名单里 → 更新了 #{entry_id} 的名字")
        self._select_and_reveal(str(entry_id))
        return entry_id

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
            # PeerID 不在这里改：它在界面上是只读的，属于系统字段。
            # 曾用名以编辑框里的内容为准（清空就是清空）。
            self.db.update(int(iid), player_name=dlg.result["player_name"],
                           note=dlg.result["note"],
                           prev_names=dlg.result.get("prev_names", ""))
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
        if not messagebox.askyesno(
                "确认删除",
                f"确定删除选中的 {len(iids)} 条记录？\n"
                "「最近遇到」里仍然留着这些人（那是遇到过的事实，不是名单）。",
                parent=self.root):
            return
        for iid in iids:
            self.db.delete(int(iid))
        self._after_db_change()
        self.set_status(f"已删除 {len(iids)} 条记录")

    def _after_db_change(self):
        self._load_data()
        if self.matcher is not None:
            try:
                self.matcher.reload()
            except Exception as e:                       # noqa: BLE001
                self.log.warning("刷新匹配索引失败: %s", e)
        try:
            self._refresh_data_info()
        except Exception:                                # noqa: BLE001
            pass

    def _selected_iid(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _select_and_reveal(self, iid):
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)

    def reset_column_widths(self):
        """[列宽自适应]：列宽恢复默认，空白列重新吃掉剩余宽度（拖乱了就用这个）。"""
        for col in COLUMNS:
            try:
                self.tree.column(col, width=WIDTHS[col])
            except tk.TclError:
                return
        self._column_fitter.apply()
        self._flash_status("列宽已恢复默认（每一列都可以自己拖）")

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

    def copy_name(self):
        """把选中条目的玩家名复制到剪贴板（去游戏里粘贴搜索用）。"""
        iid = getattr(self, "_ctx_iid", None) or self._selected_iid()
        if not iid:
            return
        entry = self.db.get(int(iid))
        if entry:
            self._copy_to_clipboard(entry.get("player_name") or "")

    def copy_peer_id(self):
        """把选中条目的 PeerID 复制到剪贴板。"""
        iid = getattr(self, "_ctx_iid", None) or self._selected_iid()
        if not iid:
            return
        entry = self.db.get(int(iid))
        if not entry:
            return
        value = entry.get("peer_id") or ""
        if not value:
            self._flash_status("这条没有 PeerID → 不会告警；"
                               "右键[补全 PeerID] 或从「最近遇到」重新加入")
            return
        self._copy_to_clipboard(value)

    def _copy_to_clipboard(self, value: str):
        if not value:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self._flash_status(f"已复制：{value}")
        except tk.TclError:
            pass

    def fill_peer_id(self):
        """给老条目补上 PeerID：从「最近遇到」里挑一个。

        为什么需要：老条目只有名字，只能按名字匹配 —— 别人取同名就会误报。
        补上 ID 之后这条就只走精确层（改名字也认得出，也不会被冒充）。
        """
        iid = getattr(self, "_ctx_iid", None) or self._selected_iid()
        if not iid:
            messagebox.showinfo("提示", "请先选中一条记录", parent=self.root)
            return
        entry = self.db.get(int(iid))
        if not entry:
            self.refresh()
            return
        if entry.get("peer_id"):
            if not messagebox.askyesno(
                    "这条已经有 PeerID",
                    f"「{entry['player_name']}」现在的 PeerID 是\n"
                    f"{entry['peer_id']}\n\n要换成另一个吗？",
                    parent=self.root):
                return
        rows = self._recent_rows()
        if not rows:
            messagebox.showinfo(
                "提示",
                "「最近遇到」还是空的，没有可挑的人。\n"
                "（和游戏一起跑一会儿，等到他出现在列表里再回来补）",
                parent=self.root)
            return
        dlg = RecentPickerDialog(self.root, rows, existing_peers=())
        if dlg.result in (None, "manual"):
            return
        try:
            self.db.set_peer_id(int(iid), dlg.result.get("peer_id") or "")
        except ValueError as e:
            messagebox.showerror("补全失败", str(e), parent=self.root)
            return
        self._after_db_change()
        self._select_and_reveal(iid)
        self._flash_status(f"已给「{entry['player_name']}」补上 PeerID")

    def on_rename(self, data: dict) -> None:
        """日志里发现名单里那个人改名了 —— 名单已经跟着改完，这里只说一声。

        这条不是"锦上添花"：名字同步是**自动改你数据库**的操作，不让用户看见
        就等于偷偷改数据。所以状态栏 +「最近动态」+ 那一行闪一下，三处都留痕。
        """
        old = data.get("old") or ""
        new = data.get("new") or ""
        peer_id = data.get("peer_id") or ""
        self._after_db_change()
        if peer_id:
            entry = self.db.find_by_peer_id(peer_id)
            if entry is not None:
                self._flash_row(str(entry["id"]), times=4)
        text = f"名单名字自动更新：{old} → {new}"
        self._flash_status(text, 6000)
        self.notify_event(text, "info")

    # -------------------------------------------------- 最近遇到（追踪）
    def _recent_rows(self) -> list:
        """读「最近遇到」列表（读不到就返回空，界面不炸）。"""
        try:
            return self.db.get_recent_seen()
        except Exception as e:                           # noqa: BLE001
            self.log.warning("读取「最近遇到」失败: %s", e)
            return []

    def open_recent(self):
        """[最近遇到]：跳到那一页。"""
        self.show_settings_page("recent")

    def reset_recent_columns(self):
        """[列宽自适应]：「最近遇到」列表的列宽恢复默认，空白列重新吃掉剩余宽度。"""
        for col in RECENT_COLUMNS:
            try:
                self.recent_tree.column(col, width=RECENT_WIDTHS[col])
            except tk.TclError:
                return
        self._recent_fitter.apply()
        self._flash_status("列宽已恢复默认（每一列都可以自己拖）")

    def _refresh_recent(self, force: bool = False):
        """刷新「最近遇到」页：追踪状态文字 + 列表（只在真的变了时重建）。"""
        tree = getattr(self, "recent_tree", None)
        rows = self._recent_rows()
        existing = set()
        try:
            existing = set(self.db.blacklist_peers())
        except Exception:                                # noqa: BLE001
            pass

        if tree is not None:
            signature = tuple((r.get("peer_id"),
                               r.get("name_last") or r.get("name_first"),
                               r.get("seen_count"),
                               r.get("peer_id") in existing)
                              for r in rows)
            if force or signature != self._recent_sig:
                keep = None
                sel = tree.selection()
                if sel:
                    keep = sel[0]
                tree.delete(*tree.get_children())
                for r in rows:
                    pid = r.get("peer_id") or ""
                    name = (r.get("name_last") or r.get("name_first") or "").strip()
                    tree.insert("", "end", iid=pid, values=(
                        name or "（无名）", pid, r.get("last_seen") or "",
                        r.get("seen_count") or 1,
                        "已在名单" if pid in existing else "未加入", ""))
                self._recent_sig = signature
                if keep and tree.exists(keep):
                    tree.selection_set(keep)

        hint = getattr(self, "recent_hint_var", None)
        if hint is not None:
            hint.set(f"共 {len(rows)} 人（{config.RECENT_DAYS} 天内）")

        self._refresh_track_detail()

    def _refresh_track_detail(self):
        """把追踪状态写进「最近遇到」页的只读区块。"""
        var = getattr(self, "track_detail_var", None)
        if var is None:
            return
        if self.watcher is None:
            var.set("追踪器未接入（数据来源不可用）")
            return
        s = self.watcher.snapshot()
        lines = [f"日志文件：{s['path'] or '（未取到路径）'}",
                 f"文件状态：{'存在' if s['exists'] else '不存在'}"
                 f"　大小 {s['size']} 字节　已读到偏移 {s['offset']}",
                 f"插件版本：{s['plugin'] or '未知'}"
                 + (f"（⚠ 低于要求的 {s['plugin_required']}，字段可能对不上）"
                    if s["plugin_outdated"] else ""),
                 f"解析：成功 {s['lines']} 行　失败 {s['bad_lines']} 行　"
                 f"重复丢弃 {s['dup_lines']} 行",
                 f"游戏 PID：{s['game_pid'] or '—'}　"
                 f"我自己：{s['self_name'] or '未知'}"
                 f"　最后事件：{s['last_event'] or '—'}"
                 f" @ {s['last_event_at'] or '—'}"]
        squad = s["squad"]
        if squad:
            members = "、".join(f"{m['name'] or '（无名）'}"
                                for m in squad)
            lines.append(f"当前队伍（{len(squad)} 人）：{members}")
        else:
            lines.append("当前队伍：（空 —— 没在队里，或者插件还没读到名册）")
        if s["error"]:
            lines.append(f"⚠ {s['error']}")
        if not s["dir_exists"]:
            lines.append(f"⚠ 找不到日志目录 {s['dir']} —— 游戏里装 HD2Tracker 了吗？")
        var.set("\n".join(lines))

    def _tick_watch(self):
        """每秒刷一次追踪状态（只在真的变化时重建最近遇到列表）。"""
        if self._closing:
            return
        try:
            snap = self.watcher.snapshot() if self.watcher is not None else None
            if snap is None:
                self.track_var.set("● 无数据来源")
            elif not snap["dir_exists"] or not snap["exists"]:
                self.track_var.set("● 未找到插件日志")
            elif snap["plugin_outdated"]:
                self.track_var.set("● 插件版本过旧")
            else:
                self.track_var.set(
                    f"● 追踪中 · 队伍 {len(snap['squad'])} 人 · "
                    f"最近遇到 {self.db.count_seen()} 人")
            if self.settings_page == "recent" and self.recent_tree is not None:
                self._refresh_recent(force=False)
        except Exception as e:                           # noqa: BLE001
            self.log.warning("刷新追踪状态失败: %s", e)
        finally:
            if not self._closing:
                try:
                    self._watch_job = self.root.after(1000, self._tick_watch)
                except tk.TclError:
                    self._watch_job = None

    def add_from_recent(self):
        """[加入名单]：从列表里选中的人 → 预填名字与 PeerID → 只写备注。"""
        tree = getattr(self, "recent_tree", None)
        if tree is None:
            return
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表里选中一个人", parent=self.root)
            return
        row = self.db.get_seen(sel[0])
        if not row:
            self._refresh_recent(force=True)
            return
        self._add_from_seen(row)

    def forget_recent_selected(self):
        """[从列表移除]：把这个人从「最近遇到」里划掉（不影响名单）。"""
        tree = getattr(self, "recent_tree", None)
        if tree is None:
            return
        sel = list(tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先在列表里选中一个人", parent=self.root)
            return
        if not messagebox.askyesno(
                "确认移除",
                f"把这 {len(sel)} 个人从「最近遇到」里划掉？\n"
                "（不会动黑名单；下次再遇到他们还是会重新出现）",
                parent=self.root):
            return
        for pid in sel:
            self.db.forget_seen(pid)
        self._refresh_recent(force=True)
        self._refresh_data_info()
        self._flash_status(f"已从列表移除 {len(sel)} 人")

    def clear_recent(self):
        """[清空最近遇到]：整张列表清掉（名单不动）。"""
        if not messagebox.askyesno("确认清空",
                                   "清空「最近遇到」全部记录？\n"
                                   "（不动黑名单；下次再遇到他们还是会重新记录）",
                                   parent=self.root):
            return
        self.db.clear_seen()
        self._refresh_recent(force=True)
        self._refresh_data_info()
        self._flash_status("「最近遇到」已清空")

    def ignore_recent_selected(self):
        """[忽略此人]：以后不再提醒他，也不再进「最近遇到」。

        和[从列表移除]的区别：那个只是把这一条划掉，下次遇到照旧提醒；
        忽略是记进忽略名单。反悔的入口在「数据」页 → [忽略名单…]。
        """
        tree = getattr(self, "recent_tree", None)
        if tree is None:
            return
        sel = list(tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先在列表里选中一个人", parent=self.root)
            return
        row = self.db.get_seen(sel[0]) or {}
        name = (row.get("name_last") or row.get("name_first") or "").strip()
        if not messagebox.askyesno(
                "确认忽略",
                f"以后不再提醒「{name or sel[0]}」，也不再把他列进「最近遇到」？\n"
                "（可以在「数据」页 → [忽略名单…] 里取消）",
                parent=self.root):
            return
        for pid in sel:
            self.db.ignore_peer(pid, name)
        self._refresh_recent(force=True)
        self._refresh_data_info()
        self._flash_status(f"已忽略 {len(sel)} 人")

    def open_ignore_list(self):
        """[忽略名单…]：查看 / 逐条取消忽略 / 整体清空。"""
        dlg = IgnoreListDialog(self.root, self.db)
        if dlg.changed:
            self._refresh_recent(force=True)
            self._refresh_data_info()
            self._flash_status("忽略名单已更新")

    def open_log_dir(self):
        try:
            os.startfile(config.PLUGIN_LOG_DIR)          # noqa: S606
        except Exception as e:                           # noqa: BLE001
            messagebox.showerror("打开失败", str(e), parent=self.root)

    def open_player_log(self):
        path = config.player_log_path()
        if not os.path.exists(path):
            messagebox.showinfo("提示",
                                f"文件还不存在：\n{path}\n"
                                "（插件没加载，或者还没进过任何一局）",
                                parent=self.root)
            return
        try:
            os.startfile(path)                           # noqa: S606
        except Exception as e:                           # noqa: BLE001
            messagebox.showerror("打开失败", str(e), parent=self.root)

    # -------------------------------------------------- 后台线程 → 主线程
    def on_alert(self, data: dict) -> None:
        """供 app 层推送一次命中（主线程里执行）。

        与 v1 的 ``enqueue_encounter_update`` 是同一个位置，区别是它现在
        直接由 app 层经 `_gui_call` 派发，不再需要一条专用队列。
        """
        entry = data.get("entry") or {}
        iid = str(entry.get("id") or "")
        if iid and self.tree.exists(iid):
            self._flash_row(iid, times=config.FLASH_TIMES)
        self.session_hit_count += 1
        self._refresh_stats()
        name = data.get("name") or entry.get("player_name") or "未知"
        label = config.alert_source_label(data.get("source") or "")
        note = "" if data.get("notified") else "（提示未能弹出，见日志）"
        self.set_status(f"命中黑名单：{name}　匹配度 "
                        f"{float(data.get('score') or 0):.0f}　{label}{note}")
        self.notify_event(f"命中黑名单：{name}　{label}", "hit")

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
        """排空命令队列（不排定时器，可安全地被测试或其它代码直接调用）。"""
        try:
            while True:
                func = self._command_queue.get_nowait()
                try:
                    func()
                except Exception as e:                   # noqa: BLE001
                    self.log.warning("主线程任务异常: %s", e)
        except queue.Empty:
            pass

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
            seen = self.db.count_seen()
        except Exception:                                # noqa: BLE001
            seen = 0
        squad = 0
        if self.watcher is not None:
            try:
                squad = len(self.watcher.snapshot()["squad"])
            except Exception:                            # noqa: BLE001
                squad = 0
        self.stats_var.set(
            f"黑名单总数：{total}　|　本局告警：{self.session_hit_count}"
            f"　|　最近遇到：{seen} 人　|　当前队伍：{squad} 人"
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
        """一局开始时清零「本局告警」。"""
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
            self.monitor_var.set("● 追踪中")
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
        self.set_status("已暂停追踪" if self.paused else "已恢复追踪")
        self._sync_pause_ui()

    def _sync_pause_ui(self):
        """顶部 [暂停追踪] 按钮的文字/样式跟着状态走。"""
        btn = getattr(self, "pause_btn", None)
        if btn is None:
            return
        try:
            btn.configure(text="恢复追踪" if self.paused else "暂停追踪",
                          style="Accent.TButton" if self.paused else "Bar.TButton")
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- 设置
    def open_notification_settings(self):
        """打开设置区的「通知」分页（原来是个独立对话框）。"""
        if self.notifier is None:
            messagebox.showinfo("提示", "通知模块不可用", parent=self.root)
            return
        self.show_settings_page("notify")

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
            "数据来源：游戏内插件 HD2Tracker 写出的 playerLog.txt\n"
            "  %LOCALAPPDATA%\\CowboyBingus\\Helldivers2\\Logs\\\n"
            "本应用只读这一个文本文件，不注入、不读内存、不改包。\n\n"
            "什么时候提醒：有人刚进队（join）且 **PeerID 命中黑名单**。\n"
            f"  · 同一局里同一个人 {config.HIT_DEDUP_WINDOW} 秒内只提醒一次；\n"
            "  · 队伍快照只刷新「现在队里有谁」，不会触发提醒；\n"
            "  · 你自己永远不会被当成目标；\n"
            "  · **只认 PeerID**：没 ID 的老条目不会提醒（名字会重名、会改）。\n\n"
            "怎么加人：打开「最近遇到」→ 选中 → [加入名单]（双击也行）。\n"
            "  名字与 PeerID 由程序填好，你只写备注 —— 不会填错。\n"
            "  不想再被某个人打扰就点 [忽略此人]，反悔在「数据」页 → [忽略名单…]。\n"
            "  老条目可以在名单里右键 → [补全 PeerID]（从「最近遇到」挑一个）。\n\n"
            "PeerID 是 PlayFab 账户标识，跨局稳定、改名也不变 —— 所以它是**身份**：\n"
            "  同名不同 ID 是两个人（各留一条），同一个人改名只更新名字。\n\n"
            "名单备份：工具栏 [导出] / [导入]，支持 CSV（Excel 兼容）与 JSON。\n"
            "自定义提示：设置 → 通知。\n\n"
            "命令行：--check 自检 / --watch-debug 实时看日志 / --replay 回放日志。\n"
            "重复打开不会多开进程：会把已经运行的那个窗口叫到前台。",
            parent=self.root)

    def show_about(self):
        messagebox.showinfo(
            "关于",
            f"{config.APP_NAME} v{config.VERSION}\n\n"
            "读取游戏内插件写出的日志文件，本地比对本机黑名单。\n"
            "全程不注入游戏进程、不读游戏内存、不改游戏包。\n"
            "提示窗口使用 WS_EX_NOACTIVATE | WS_EX_TRANSPARENT，不抢焦点。\n\n"
            f"数据目录：{config.DATA_DIR}\n"
            f"插件日志：{config.player_log_path()}",
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

        # 托盘只是"同一个命令的另一个入口"，不重复实现任何逻辑
        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", _show, default=True),
            pystray.MenuItem("最近遇到", lambda i, it: self.post(self.open_recent)),
            pystray.MenuItem("暂停/恢复追踪", _pause),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开设置：最近遇到",
                             lambda i, it: self.post(self.open_recent)),
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
        jobs = ([self._drain_job, self._flash_job, self._watch_job,
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
        self._watch_job = None
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

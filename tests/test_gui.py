# -*- coding: utf-8 -*-
"""阶段 6-7 验收测试：GUI 主界面 / 通知设置 / 校准器（构建级冒烟 + 线程安全）。

这些用例会真的创建 Tk 窗口（创建后立即 withdraw / destroy）。
若当前会话没有可用桌面，则整类跳过。

运行： python -m unittest tests.test_gui -v
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sys
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import config  # noqa: E402
from app.ui import gui as gui_mod  # noqa: E402
from app.core.database import BlacklistDB                 # noqa: E402
from app.core.matcher import Matcher                      # noqa: E402
from app.settings.notification_config import NotificationConfig   # noqa: E402
from app.settings.region_config import RegionConfig           # noqa: E402

TMP_ROOT = os.path.join(_ROOT, ".test_tmp")


def _tk_available() -> bool:
    try:
        import tkinter as tk
        r = tk.Tk()
        r.destroy()
        return True
    except Exception:                                # noqa: BLE001
        return False


TK_OK = _tk_available()


class TempCase(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.tmp = os.path.join(TMP_ROOT, self._testMethodName)
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *p):
        return os.path.join(self.tmp, *p)


# ==========================================================================
@unittest.skipUnless(TK_OK, "当前会话没有可用桌面")
class TestEntryDialogValidation(TempCase):
    """EntryDialog 的校验逻辑（不弹窗，直接测收集结果）。"""

    def setUp(self):
        super().setUp()
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:                            # noqa: BLE001
            pass
        super().tearDown()

    def _dialog(self, entry=None):
        import tkinter as tk
        from app.ui.gui import EntryDialog
        # 不进入 wait_window：直接构造内部状态
        dlg = EntryDialog.__new__(EntryDialog)
        dlg.master = self.root
        dlg.result = None
        dlg.top = tk.Toplevel(self.root)
        dlg.top.withdraw()
        dlg.vars = {k: tk.StringVar(value=str((entry or {}).get(k) or ""))
                    for k in ("player_name",)}
        dlg.note_text = tk.Text(dlg.top)
        if entry and entry.get("note"):
            dlg.note_text.insert("1.0", entry["note"])
        return dlg

    def test_valid_input(self):
        dlg = self._dialog()
        dlg.vars["player_name"].set("PlayerX")
        dlg.note_text.insert("1.0", "恶意TK")
        dlg._ok()
        self.assertEqual(dlg.result["player_name"], "PlayerX")
        self.assertEqual(dlg.result["note"], "恶意TK")

    def test_requires_name(self):
        import tkinter.messagebox as mb
        dlg = self._dialog()
        orig = mb.showwarning
        mb.showwarning = lambda *a, **k: None
        try:
            dlg._ok()
            self.assertIsNone(dlg.result)            # 被拦下
        finally:
            mb.showwarning = orig

    def test_blank_name_is_rejected(self):
        import tkinter.messagebox as mb
        dlg = self._dialog()
        dlg.vars["player_name"].set("   ")
        orig = mb.showwarning
        mb.showwarning = lambda *a, **k: None
        try:
            dlg._ok()
            self.assertIsNone(dlg.result)
        finally:
            mb.showwarning = orig

    def test_only_name_and_note_are_collected(self):
        """对话框只收集 名称 / 备注 —— 玩家ID / TK次数 / 证据图路径都已删掉。"""
        dlg = self._dialog()
        dlg.vars["player_name"].set("PlayerX")
        dlg._ok()
        self.assertEqual(set(dlg.result), {"player_name", "note"})

    def test_prefilled_from_entry(self):
        dlg = self._dialog({"player_name": "N", "note": "备注"})
        dlg._ok()
        self.assertEqual(dlg.result["player_name"], "N")
        self.assertEqual(dlg.result["note"], "备注")


# ==========================================================================
@unittest.skipUnless(TK_OK, "当前会话没有可用桌面")
class TestBlacklistGUI(TempCase):
    def setUp(self):
        super().setUp()
        self.db = BlacklistDB(self.path("bl.db"))
        self.db.add("PlayerX", "恶意TK")
        self.db.add("John Doe", "空格名")
        self.region_cfg = RegionConfig(self.path("user_config.json"))
        self.notif_cfg = NotificationConfig(self.path("notification.json"))
        self.matcher = Matcher(self.db)
        self.scheduler = None

        from app.ui.gui import BlacklistGUI
        self.gui = BlacklistGUI(self.db, self.notif_cfg, self.region_cfg,
                                matcher=self.matcher)
        self.gui.root.withdraw()

    def tearDown(self):
        try:
            self.gui.destroy()
        except Exception:                            # noqa: BLE001
            pass
        # 在主线程里立刻回收 Tk 对象：Tk 变量/图像若被后台线程或循环引用留到
        # 解释器退出时才析构，Tcl 会在错误的线程删掉自己的 async handler，
        # 进程直接 abort（Tcl_AsyncDelete: async handler deleted by the wrong
        # thread）。测试里反复建/销毁 Tk 解释器时这个偶发崩溃很明显。
        self.gui = None
        gc.collect()
        self.db.close()
        super().tearDown()

    def test_tree_columns(self):
        from app.ui.gui import COLUMNS, HEADERS
        self.assertEqual(tuple(self.gui.tree["columns"]), COLUMNS)
        for col in COLUMNS:
            self.assertEqual(self.gui.tree.heading(col)["text"], HEADERS[col])

    def test_rows_loaded(self):
        self.assertEqual(len(self.gui.tree.get_children()), 2)

    def test_row_values(self):
        """名单只有三列：名称 / 备注 / 添加时间。"""
        values = self.gui.tree.item("1", "values")
        self.assertEqual(len(values), 3)
        self.assertEqual(values[0], "PlayerX")
        self.assertEqual(values[1], "恶意TK")
        self.assertTrue(values[2])                 # created_at

    def test_tree_has_no_stats_columns(self):
        from app.ui.gui import COLUMNS
        self.assertEqual(COLUMNS, ("player_name", "note", "created_at"))
        for gone in ("player_id", "tk_count", "encounter_count", "last_seen"):
            self.assertNotIn(gone, COLUMNS)

    def test_sort_by_column_toggles(self):
        self.gui.sort_by_column("player_name")
        self.assertEqual(self.gui.sort_key, "player_name")
        self.assertTrue(self.gui.sort_desc)
        self.gui.sort_by_column("player_name")
        self.assertFalse(self.gui.sort_desc)
        self.assertIn("升序", self.gui.sort_dir_btn["text"])

    def test_sort_by_removed_column_is_ignored(self):
        self.gui.sort_by_column("encounter_count")     # 不该崩，也不该改排序键
        self.assertEqual(self.gui.sort_key, "created_at")

    def test_sort_button_label_sync(self):
        self.gui._on_sort_selected()
        self.assertEqual(self.gui.sort_key, "created_at")

    def test_search_and_clear(self):
        self.gui.search_var.set("John")
        self.gui.do_search()
        self.assertEqual(len(self.gui.tree.get_children()), 1)
        self.gui.clear_search()
        self.assertEqual(len(self.gui.tree.get_children()), 2)

    def test_search_by_note(self):
        self.gui.search_var.set("空格名")
        self.gui.do_search()
        self.assertEqual(len(self.gui.tree.get_children()), 1)

    # ---- 跨线程更新 ----
    def test_encounter_update_via_queue(self):
        """模拟后台线程命中 → 主线程 drain → UI 立即刷新。

        名单里已经没有「遇到次数 / 最后遇见」列了，所以命中只闪行 + 更新
        状态栏与动态流，不再写单元格。
        """
        self.gui.enqueue_encounter_update({
            "entry_id": 1, "player_name": "PlayerX",
            "today_count": 5, "seen_at": "2026-01-01 00:00:00",
            "score": 100.0, "source": "chat",
        })
        self.gui._drain_once()
        self.assertEqual(self.gui.session_hit_count, 1)
        self.assertIn("命中黑名单", self.gui.status_var.get())
        self.assertEqual(self.gui.tree.item("1", "values")[0], "PlayerX")
        self.assertIn("hit", self.gui.tree.item("1", "tags"))   # 命中行闪烁

    def test_encounter_update_for_missing_row_is_safe(self):
        self.gui.enqueue_encounter_update({
            "entry_id": 999, "player_name": "Ghost",
            "today_count": 1, "seen_at": "x", "score": 90, "source": "s"})
        self.gui._drain_once()                     # 不抛异常
        self.assertEqual(self.gui.session_hit_count, 1)

    def test_flash_row_does_not_crash(self):
        self.gui._flash_row("1", times=4, interval=10)
        for _ in range(6):
            self.gui.root.update()
            time.sleep(0.02)
        self.gui.root.update()

    def test_flash_survives_row_deleted(self):
        self.gui._flash_row("1", times=6, interval=10)
        self.gui.tree.delete("1")
        for _ in range(6):
            self.gui.root.update()
            time.sleep(0.02)

    def test_post_runs_on_main_thread(self):
        box = []
        self.gui.post(lambda: box.append(1))
        self.gui._drain_once()
        self.assertEqual(box, [1])

    def test_post_exception_is_swallowed(self):
        def boom():
            raise RuntimeError("x")
        self.gui.post(boom)
        self.gui._drain_once()                     # 不抛异常

    def test_drain_once_does_not_schedule_timer(self):
        self.gui._cancel_pending()
        self.assertIsNone(self.gui._drain_job)
        self.gui._drain_once()
        self.assertIsNone(self.gui._drain_job)     # 只排空，不排定时器

    def test_drain_queues_never_orphans_timers(self):
        """重复直接调用 _drain_queues 不能留下无法取消的孤儿定时器。"""
        self.gui._cancel_pending()
        self.gui._drain_queues()
        first = self.gui._drain_job
        self.assertIsNotNone(first)
        self.gui._drain_queues()                   # 旧的一定先被取消
        second = self.gui._drain_job
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.gui._cancel_pending()
        self.assertIsNone(self.gui._drain_job)

    def test_destroy_cancels_pending_timers(self):
        self.gui._drain_queues()
        self.assertIsNotNone(self.gui._drain_job)
        self.gui.destroy()
        self.assertTrue(self.gui._closing)

    # ---- 命中记录（列表里不再有计数列） ----
    def test_hit_records_encounter_without_touching_list(self):
        self.db.record_encounter(1, 100.0, "chat", "PlayerX", "")
        self.gui.refresh()
        self.assertEqual(self.db.get_total_encounter_count(), 1)
        self.assertEqual(len(self.gui.tree.get_children()), 2)
        self.assertEqual(self.gui.tree.item("1", "values")[0], "PlayerX")

    def test_context_menu_has_no_counter_reset(self):
        labels = [self.gui.menu.entrycget(i, "label")
                  for i in range(self.gui.menu.index("end") + 1)
                  if self.gui.menu.type(i) == "command"]
        self.assertEqual(labels, ["编辑", "删除"])

    # ---- 统计 ----
    def test_stats_bar(self):
        self.gui._refresh_stats()
        text = self.gui.stats_var.get()
        self.assertIn("黑名单总数：2", text)
        self.assertIn("本局命中：0", text)
        self.assertIn("今日命中：0", text)

    def test_today_count_after_hit(self):
        self.db.record_encounter(1, 100.0, "chat", "PlayerX", "")
        self.gui._refresh_stats()
        self.assertIn("今日命中：1", self.gui.stats_var.get())

    def test_reset_session_stats(self):
        self.gui.session_hit_count = 9
        self.gui.reset_session_stats()
        self.assertEqual(self.gui.session_hit_count, 0)
        self.assertIn("本局命中：0", self.gui.stats_var.get())

    def test_set_monitoring_labels(self):
        self.gui.set_monitoring(False, "游戏未运行")
        self.assertIn("已暂停", self.gui.monitor_var.get())
        self.gui.set_monitoring(True)
        self.assertIn("监控中", self.gui.monitor_var.get())

    def test_pause_button_actually_toggles(self):
        """[暂停监控] 必须能真的切换。

        旧实现读的是 pause_var（而主界面这个按钮从来不写它）→ 每次点都只发一次
        "恢复"，永远暂停不上，日志还一直刷「监控已恢复」（用户实测反馈）。
        """
        seen = []
        self.gui.on_pause = seen.append
        self.gui.set_monitoring(True)                  # 假装游戏在跑

        self.gui.toggle_pause()
        self.assertTrue(self.gui.paused)
        self.assertTrue(self.gui.pause_var.get())
        self.assertIn("恢复监控", self.gui.pause_btn.cget("text"))
        self.assertEqual(seen, [True])

        self.gui.toggle_pause()
        self.assertFalse(self.gui.paused)
        self.assertFalse(self.gui.pause_var.get())
        self.assertIn("暂停监控", self.gui.pause_btn.cget("text"))
        self.assertEqual(seen, [True, False])

    def test_pause_does_not_fake_monitoring_light(self):
        """游戏没在跑时点暂停，指示灯不能变成「监控中」。"""
        self.gui.set_monitoring(False, "游戏未运行")
        self.assertIn("已暂停", self.gui.monitor_var.get())

        self.gui.toggle_pause()
        self.assertIn("已暂停", self.gui.monitor_var.get())
        self.assertNotIn("监控中", self.gui.monitor_var.get())
        self.assertIn("手动暂停", self.gui.monitor_var.get())

        self.gui.toggle_pause()                        # 取消暂停，游戏仍未运行
        self.assertIn("已暂停", self.gui.monitor_var.get())
        self.assertNotIn("监控中", self.gui.monitor_var.get())

    def test_monitor_light_green_only_when_game_runs_and_not_paused(self):
        self.gui.toggle_pause()                        # 用户暂停
        self.gui.set_monitoring(True)                  # 游戏起来了，但用户暂停着
        self.assertIn("已暂停", self.gui.monitor_var.get())
        self.gui.toggle_pause()                        # 取消暂停
        self.assertIn("监控中", self.gui.monitor_var.get())
        self.gui.set_monitoring(False, "游戏已退出")     # 游戏退出 → 灯变回暂停
        self.assertIn("已暂停", self.gui.monitor_var.get())

    # ---- 增删改 ----
    def test_delete_entry(self):
        self.db.delete(1)
        self.gui.refresh()
        self.assertEqual(len(self.gui.tree.get_children()), 1)

    def test_after_db_change_reloads_matcher(self):
        self.db.add("NewGuy")
        self.gui._after_db_change()
        self.assertEqual(len(self.gui.tree.get_children()), 3)
        self.assertIsNotNone(self.matcher.match_one("NewGuy"))

    def test_export_list(self):
        import csv
        import importlib
        # 注意：__import__("a.b.c") 返回的是顶层包 a，拿子模块要用 import_module
        gui_mod = importlib.import_module("app.ui.gui")
        out = self.path("out.csv")
        rows = self.db.get_all()
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([gui_mod.HEADERS[c] for c in gui_mod.COLUMNS])
            for r in rows:
                w.writerow(gui_mod.BlacklistGUI._row_values(r))
        with open(out, encoding="utf-8-sig") as f:
            self.assertEqual(len(list(csv.reader(f))), 3)

    def test_right_click_menu_exists(self):
        """右键菜单只留 编辑 / 删除 —— 计数类操作随字段一起删掉了。"""
        labels = [self.gui.menu.entrycget(i, "label")
                  for i in range(self.gui.menu.index("end") + 1)
                  if self.gui.menu.type(i) == "command"]
        self.assertEqual(labels, ["编辑", "删除"])
        self.assertFalse(hasattr(self.gui, "_reset_encounter"))
        self.assertFalse(hasattr(self.gui, "_reset_last_seen"))

    # ---- 布局 ----
    def _overflowing_children(self, parent, limit_x, limit_y):
        """返回超出父窗口可视范围且被映射的子控件（即被挤掉的控件）。"""
        bad = []

        def walk(w):
            for child in w.winfo_children():
                try:
                    if child.winfo_ismapped():
                        x = child.winfo_rootx() - self.gui.root.winfo_rootx()
                        y = child.winfo_rooty() - self.gui.root.winfo_rooty()
                        if (x + child.winfo_width() > limit_x + 2
                                or y + child.winfo_height() > limit_y + 2):
                            bad.append((child.winfo_class(),
                                        child.winfo_name(),
                                        x, y,
                                        child.winfo_width(),
                                        child.winfo_height()))
                except Exception:                     # noqa: BLE001
                    pass
                walk(child)

        walk(parent)
        return bad

    def test_toolbar_fits_at_min_width(self):
        """最小尺寸下两行工具带、设置标签栏、状态栏都不能被挤掉。"""
        from app.ui import gui as gui_mod
        min_w, min_h = gui_mod.MIN_WIDTH, gui_mod.MIN_HEIGHT
        self.gui.root.minsize(min_w, min_h)
        self.gui.root.geometry(f"{min_w}x{min_h}+0+0")
        self.gui.root.deiconify()
        self.gui.root.update()
        self.gui.root.update_idletasks()

        bad = self._overflowing_children(self.gui.root, min_w, min_h)
        self.assertEqual(bad, [], f"以下控件在 {min_w}x{min_h} 下被挤出窗口: {bad}")
        self.gui.root.withdraw()

    def _button_texts(self):
        texts = []

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c.winfo_class() == "TButton":
                        texts.append(c.cget("text"))
                except Exception:                     # noqa: BLE001
                    pass
                walk(c)

        walk(self.gui.root)
        return texts

    def test_toolbar_buttons_are_not_squeezed(self):
        """最小尺寸下按钮不能被 pack 压扁。

        这条能抓住一个真实历史坑：Tk 8.6 的 ttk 按钮默认等宽（-11 字符），
        一行塞不下时 pack 会把每个按钮一起压缩 —— 曾经挤到 1px 宽。
        """
        from app.ui import gui as gui_mod
        self.gui.root.minsize(gui_mod.MIN_WIDTH, gui_mod.MIN_HEIGHT)
        self.gui.root.geometry(f"{gui_mod.MIN_WIDTH}x{gui_mod.MIN_HEIGHT}+0+0")
        self.gui.root.deiconify()
        self.gui.root.update()
        self.gui.root.update_idletasks()

        squeezed = []

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c.winfo_class() == "TButton" and c.winfo_ismapped():
                        if c.winfo_width() < c.winfo_reqwidth() - 2:
                            squeezed.append((c.cget("text"),
                                             c.winfo_width(),
                                             c.winfo_reqwidth()))
                except Exception:                     # noqa: BLE001
                    pass
                walk(c)

        walk(self.gui.root)
        self.assertEqual(squeezed, [], f"这些按钮被挤扁了: {squeezed}")
        self.gui.root.withdraw()

    def test_toolbar_buttons_all_present(self):
        """① 运行状态带 + ② 名单工具带 + ④ 设置标签栏的按钮一个都不能少。"""
        texts = self._button_texts()
        for expected in ("添加", "编辑", "删除", "搜索", "清空", "↓ 降序",
                         "导入", "导出",              # ② 名单工具带
                         "扫描聊天框", "暂停监控", "退出"):   # ① 运行状态带
            self.assertIn(expected, texts, f"缺少按钮：{expected}")
        for _, label in gui_mod.SETTINGS_TABS:        # ④ 设置分页标签
            self.assertIn(label, texts, f"设置缺少分页：{label}")
        self.assertIn("▴ 展开设置", texts)

    def test_no_menu_bar(self):
        """菜单栏已删除：重复入口的根源。"""
        self.assertEqual(str(self.gui.root.cget("menu")), "")

    def test_duplicated_entries_are_gone(self):
        """原来在菜单 + 工具栏各来一遍的入口不再重复。"""
        texts = self._button_texts()
        for gone in ("恢复全部默认", "通知设置", "校准区域", "最小化到托盘"):
            self.assertNotIn(gone, texts,
                             f"{gone} 应该只存在于设置分页里，不该再占工具带")

    def test_settings_collapsed_by_default(self):
        self.gui.root.update()
        self.assertFalse(self.gui.settings_expanded)
        self.assertEqual(self.gui.settings_toggle_btn.cget("text"), "▴ 展开设置")
        # 收起时内容区根本没被 pack：标签栏还在，内容不参与布局
        self.assertFalse(self.gui.settings_content.winfo_manager())
        self.assertGreater(self.gui.settings_bar.winfo_reqheight(), 0)

    def test_toggle_settings_expands_and_collapses(self):
        self.gui.root.deiconify()
        self.gui.root.update()
        self.gui.set_settings_expanded(True)
        self.gui.root.update()
        self.gui.root.update_idletasks()
        self.assertTrue(self.gui.settings_expanded)
        self.assertEqual(self.gui.settings_toggle_btn.cget("text"), "▾ 收起设置")
        self.assertEqual(self.gui.settings_content.winfo_manager(), "pack")
        self.assertGreater(self.gui.settings_content.winfo_height(), 100)

        self.gui.set_settings_expanded(False)
        self.gui.root.update()
        self.gui.root.update_idletasks()
        self.assertFalse(self.gui.settings_content.winfo_manager())
        self.gui.root.withdraw()

    def test_settings_pages_switch(self):
        self.gui.root.update()
        self.gui.show_settings_page("data")
        self.assertEqual(self.gui.settings_page, "data")
        self.assertTrue(self.gui.settings_expanded)      # 点分页会自动展开
        packed = [k for k, f in self.gui.settings_pages.items()
                  if f.winfo_manager() == "pack"]
        self.assertEqual(packed, ["data"])
        self.assertEqual(self.gui.settings_tab_btns["data"].cget("style"),
                         "Accent.TButton")
        self.assertEqual(self.gui.settings_tab_btns["scan"].cget("style"),
                         "Bar.TButton")

    def test_startup_layout_settles_without_jitter(self):
        """启动后设置区高度必须一次到位，之后纹丝不动。

        历史 bug：挪分隔条会让 PanedWindow 自身高度变 1px → 触发 <Configure>
        → 又去挪 → 再变 1px…… 无限来回抖（肉眼就是启动时那块区域抽搐，而且
        事件循环空转烧 CPU）。所以这里直接数"设置了几次高度"和"位置变过几种"。
        """
        self.gui.root.deiconify()
        self.gui.root.update()
        time.sleep(0.2)                     # 先让首帧布局落定

        calls = []
        orig = self.gui.paned.paneconfigure

        def traced(*args, **kwargs):
            if "height" in kwargs:
                calls.append(kwargs["height"])
                if len(calls) > 12:         # 保险丝：别把测试本身拖死
                    return None
            return orig(*args, **kwargs)

        self.gui.paned.paneconfigure = traced
        positions, heights = set(), set()
        t0 = time.time()
        while time.time() - t0 < 0.5:
            self.gui.root.update()
            try:
                positions.add(self.gui.paned.sash_coord(0)[1])
                heights.add(self.gui.settings_frame.winfo_height())
            except Exception:                    # noqa: BLE001
                pass
            time.sleep(0.01)

        self.assertLessEqual(len(calls), 8, f"反复重设设置区高度：{calls}")
        self.assertEqual(len(positions), 1, f"分隔条在抖动：{sorted(positions)}")
        self.assertEqual(len(heights), 1, f"设置区高度在抖动：{sorted(heights)}")
        # 收起态就该是"标签栏那么高"（差 1~2px 是 Tk 的边框取整，不算问题）
        self.assertAlmostEqual(self.gui.settings_frame.winfo_height(),
                               self.gui._collapsed_h, delta=4)
        self.gui.root.withdraw()

    def test_settings_page_falls_back_to_first(self):
        self.gui.show_settings_page("no_such_page")
        self.assertEqual(self.gui.settings_page, gui_mod.SETTINGS_TABS[0][0])

    def test_every_settings_page_scrolls_to_the_end(self):
        """每一页的内容都必须落在滚动区域内 —— 否则底部的按钮根本点不到。"""
        self.gui.root.deiconify()
        self.gui.root.update()
        for key, label in gui_mod.SETTINGS_TABS:
            self.gui.show_settings_page(key)
            for _ in range(4):
                self.gui.root.update()
            area = self.gui.settings_content
            area.update_idletasks()
            bbox = area.canvas.bbox("all")
            self.assertIsNotNone(bbox, f"{label} 页没有滚动区域")
            self.assertGreaterEqual(
                bbox[3], area.inner.winfo_reqheight() - 2,
                f"{label} 页的内容超出了滚动区域，滚到底也点不到")
        self.gui.root.withdraw()


# ==========================================================================
@unittest.skipUnless(TK_OK, "当前会话没有可用桌面")
class TestNotificationDialog(TempCase):
    def setUp(self):
        super().setUp()
        import tkinter as tk
        from app.notify.notifier import Notifier
        self.root = tk.Tk()
        self.root.withdraw()
        self.cfg = NotificationConfig(self.path("notification.json"))
        self.notifier = Notifier(self.cfg)
        self.notifier.toast_enabled = False

    def tearDown(self):
        try:
            self.notifier.close()
        except Exception:                            # noqa: BLE001
            pass
        try:
            self.root.destroy()
        except Exception:                            # noqa: BLE001
            pass
        super().tearDown()

    def _dialog(self):
        from app.ui.gui_notification import NotificationSettingsDialog
        return NotificationSettingsDialog(self.root, self.cfg, self.notifier)

    def test_build_and_load_defaults(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            self.assertEqual(d.title_var.get(), config.DEFAULT_NOTIFICATION["title_template"])
            self.assertEqual(d.image_mode_var.get(), "default")
            self.assertAlmostEqual(d.opacity_var.get(), 0.85, places=2)
            self.assertEqual(d.sound_mode_var.get(), "beep")
            self.assertTrue(d.show_vars["note"].get())
            self.assertFalse(d.show_vars["time"].get())
        finally:
            d.close()

    def test_collect_roundtrip(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            d.title_var.set("我的标题")
            d.body_text.delete("1.0", "end")
            d.body_text.insert("1.0", "{player_name} 出现了")
            d.image_mode_var.set("none")
            d.opacity_var.set(0.5)
            d.width_var.set(420)
            d.height_var.set(130)
            d.monitor_var.set(2)
            d.pos_combo.set("左下")
            d.sound_mode_var.set("none")
            data = d._collect()
            self.assertEqual(data["title_template"], "我的标题")
            self.assertEqual(data["body_template"], "{player_name} 出现了")
            self.assertEqual(data["image_mode"], "none")
            self.assertAlmostEqual(data["appearance"]["opacity"], 0.5, places=3)
            self.assertEqual(data["appearance"]["width"], 420)
            self.assertEqual(data["appearance"]["monitor"], 2)
            self.assertEqual(data["appearance"]["position"], "bottom_left")
            self.assertEqual(data["sound"]["mode"], "none")
        finally:
            d.close()

    def test_save_writes_file_and_takes_effect(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            d.title_var.set("持久化标题")
            d.save()
            self.assertTrue(os.path.exists(self.path("notification.json")))
            self.assertEqual(self.cfg.get()["title_template"], "持久化标题")
        finally:
            d.close()

    def test_reset_restores_defaults(self):
        import tkinter.messagebox as mb
        d = self._dialog()
        try:
            d.top.withdraw()
            d.title_var.set("X")
            d.save()
            orig = mb.askyesno
            mb.askyesno = lambda *a, **k: True
            try:
                d.reset()
            finally:
                mb.askyesno = orig
            self.assertEqual(d.title_var.get(),
                             config.DEFAULT_NOTIFICATION["title_template"])
            self.assertFalse(os.path.exists(self.path("notification.json")))
        finally:
            d.close()

    def test_preview_render_does_not_raise(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            d.title_var.set("黑名单玩家")
            d._refresh_preview()
            self.assertIsNotNone(d.preview_photo)
        finally:
            d.close()

    def test_clamping_of_bad_numbers(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            d.width_var.set(-100)
            d.height_var.set(-5)
            d.opacity_var.set(9.0)
            d.beep_freq_var.set(999999)
            data = d._collect()
            self.assertGreaterEqual(data["appearance"]["width"], 160)
            self.assertGreaterEqual(data["appearance"]["height"], 60)
            self.assertLessEqual(data["appearance"]["opacity"], 1.0)
            self.assertLessEqual(data["sound"]["beep_freq"], 32767)
        finally:
            d.close()

    # ---- 自定义资源：默认目录 = data/assets，选完自动复制一份 ----
    def _pick_with(self, dialog, method, src):
        """把文件对话框替换成「返回 src」，并把 data/assets 指向临时目录。"""
        import tkinter.filedialog as fd
        from app.core import assets as astore
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return src

        with mock.patch.object(astore, "ASSETS_DIR", self.path("assets")), \
                mock.patch.object(fd, "askopenfilename", fake):
            getattr(dialog, method)()
        return seen

    def test_pick_image_defaults_to_assets_and_copies(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            src = self.path("外链图.png")
            with open(src, "wb") as f:
                f.write(b"IMG")
            seen = self._pick_with(d, "_pick_image", src)
            self.assertEqual(seen.get("initialdir"), self.path("assets"))
            self.assertEqual(d.image_mode_var.get(), "custom")
            got = d.image_path_var.get()
            self.assertEqual(got, self.path("assets", "外链图.png"))
            self.assertTrue(os.path.isfile(got))
            self.assertIn("assets", d.status_var.get())
        finally:
            d.close()

    def test_pick_sound_accepts_mp3_and_copies(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            src = self.path("预警.mp3")
            with open(src, "wb") as f:
                f.write(b"ID3")
            seen = self._pick_with(d, "_pick_sound", src)
            self.assertEqual(seen.get("initialdir"), self.path("assets"))
            pattern = dict(seen.get("filetypes"))["音频"]
            self.assertIn("*.mp3", pattern)
            self.assertEqual(d.sound_mode_var.get(), "wav")
            self.assertEqual(d.sound_path_var.get(),
                             self.path("assets", "预警.mp3"))
        finally:
            d.close()

    def test_pick_cancelled_changes_nothing(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            before = (d.image_path_var.get(), d.image_mode_var.get())
            self._pick_with(d, "_pick_image", "")          # 用户点了取消
            self.assertEqual((d.image_path_var.get(), d.image_mode_var.get()),
                             before)
        finally:
            d.close()

    def test_import_failure_falls_back_to_original_path(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            src = self.path("big.png")
            with open(src, "wb") as f:
                f.write(b"x" * 64)
            import tkinter.filedialog as fd
            from tkinter import messagebox
            from app.core import assets as astore
            shown = []
            with mock.patch.object(astore, "ASSETS_DIR", self.path("assets")), \
                    mock.patch.object(astore, "MAX_ASSET_BYTES", 8), \
                    mock.patch.object(fd, "askopenfilename",
                                      lambda **kw: src), \
                    mock.patch.object(messagebox, "showwarning",
                                      lambda *a, **k: shown.append(a)):
                d._pick_image()
            self.assertTrue(shown)                          # 弹了警告
            self.assertEqual(d.image_path_var.get(), os.path.abspath(src))
        finally:
            d.close()

    # ---- 字号（消息文字大小可编辑）----
    def test_font_size_vars_loaded_from_config(self):
        self.cfg.save({"appearance": {"title_font_size": 28,
                                      "body_font_size": 22}})
        d = self._dialog()
        try:
            d.top.withdraw()
            self.assertEqual(d.title_size_var.get(), 28)
            self.assertEqual(d.body_size_var.get(), 22)
        finally:
            d.close()

    def test_font_size_defaults_when_absent(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            self.assertEqual(d.title_size_var.get(),
                             config.DEFAULT_TITLE_FONT_SIZE)
            self.assertEqual(d.body_size_var.get(),
                             config.DEFAULT_BODY_FONT_SIZE)
        finally:
            d.close()

    def test_collect_includes_font_sizes(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            d.title_size_var.set(26)
            d.body_size_var.set(20)
            data = d._collect()
            self.assertEqual(data["appearance"]["title_font_size"], 26)
            self.assertEqual(data["appearance"]["body_font_size"], 20)
        finally:
            d.close()

    def test_font_size_clamped_on_collect(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            d.title_size_var.set(9999)
            d.body_size_var.set(1)
            data = d._collect()
            self.assertEqual(data["appearance"]["title_font_size"],
                             config.FONT_SIZE_MAX)
            self.assertEqual(data["appearance"]["body_font_size"],
                             config.FONT_SIZE_MIN)
        finally:
            d.close()

    def test_font_size_bad_input_falls_back(self):
        """字号控件里出现非数字时，_collect 必须兜住，不能抛 TclError。"""
        import tkinter as tk
        d = self._dialog()
        try:
            d.top.withdraw()

            class BadVar:
                def get(self):
                    raise tk.TclError("expected integer but got \"abc\"")

            d.title_size_var = BadVar()
            data = d._collect()
            self.assertEqual(data["appearance"]["title_font_size"],
                             config.DEFAULT_TITLE_FONT_SIZE)
        finally:
            d.close()

    def test_font_size_saved_and_reloaded(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            d.title_size_var.set(30)
            d.body_size_var.set(24)
            d.save()
        finally:
            d.close()
        self.assertEqual(self.cfg.get()["appearance"]["title_font_size"], 30)
        self.assertEqual(self.cfg.get()["appearance"]["body_font_size"], 24)

    def test_font_presets(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            d._apply_font_preset(30, 24)
            self.assertEqual(d.title_size_var.get(), 30)
            self.assertEqual(d.body_size_var.get(), 24)
            d._apply_font_preset(13, 11)
            self.assertEqual(d.title_size_var.get(), 13)
            self.assertEqual(d.body_size_var.get(), 11)
        finally:
            d.close()

    def test_font_size_change_updates_preview(self):
        """改字号后实时预览渲染出的图必须随之变化。"""
        d = self._dialog()
        try:
            d.top.withdraw()
            d.title_size_var.set(12)
            d.body_size_var.set(10)
            d._refresh_preview()
            small = d.preview_photo.width(), d.preview_photo.height()
            d.title_size_var.set(34)
            d.body_size_var.set(28)
            d._refresh_preview()
            large = d.preview_photo.width(), d.preview_photo.height()
            self.assertNotEqual(small, large)
        finally:
            d.close()

    def test_font_size_renders_without_error_at_extremes(self):
        d = self._dialog()
        try:
            d.top.withdraw()
            for size in (config.FONT_SIZE_MIN, config.FONT_SIZE_MAX):
                d.title_size_var.set(size)
                d.body_size_var.set(size)
                d._refresh_preview()
                self.assertIsNotNone(d.preview_photo)
        finally:
            d.close()

    def test_reset_restores_default_font_size(self):
        import tkinter.messagebox as mb
        d = self._dialog()
        try:
            d.top.withdraw()
            d.title_size_var.set(40)
            d.save()
            orig = mb.askyesno
            mb.askyesno = lambda *a, **k: True
            try:
                d.reset()
            finally:
                mb.askyesno = orig
            self.assertEqual(d.title_size_var.get(),
                             config.DEFAULT_TITLE_FONT_SIZE)
        finally:
            d.close()


# ==========================================================================
@unittest.skipUnless(TK_OK, "当前会话没有可用桌面")
class TestCalibratorDialog(TempCase):
    def setUp(self):
        super().setUp()
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.region_cfg = RegionConfig(self.path("user_config.json"))

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:                            # noqa: BLE001
            pass
        super().tearDown()

    class FakeCapture:
        def grab(self, key):
            from PIL import Image
            return Image.new("RGB", (200, 100), (30, 30, 30))

    def test_build_and_show_current(self):
        from app.ui.calibrator import CalibratorDialog
        d = CalibratorDialog(self.root, self.region_cfg, self.FakeCapture())
        try:
            d.top.withdraw()
            self.assertIn("left=", d.coords_var.get())
            self.assertEqual(d.source_var.get(), "内置默认值")
        finally:
            d.close()

    def test_reset_region(self):
        from app.ui.calibrator import CalibratorDialog
        self.region_cfg.set("chat_event", {"left": 1, "top": 2,
                                           "width": 30, "height": 40})
        d = CalibratorDialog(self.root, self.region_cfg, self.FakeCapture())
        try:
            d.top.withdraw()
            self.assertEqual(d.source_var.get(), "用户自定义")
            d.reset_region()
            self.assertEqual(d.source_var.get(), "内置默认值")
            self.assertFalse(self.region_cfg.is_customized("chat_event"))
        finally:
            d.close()

    def test_reset_all(self):
        import tkinter.messagebox as mb
        from app.ui.calibrator import CalibratorDialog
        self.region_cfg.set("chat_event", {"left": 1, "top": 2,
                                           "width": 30, "height": 40})
        d = CalibratorDialog(self.root, self.region_cfg, self.FakeCapture())
        orig = mb.askyesno
        mb.askyesno = lambda *a, **k: True
        try:
            d.top.withdraw()
            d.reset_all()
            self.assertEqual(len(self.region_cfg.user_regions), 0)
        finally:
            mb.askyesno = orig
            d.close()

    def test_preview_creates_window(self):
        from app.ui.calibrator import CalibratorDialog
        d = CalibratorDialog(self.root, self.region_cfg, self.FakeCapture())
        try:
            d.top.withdraw()
            d.preview()
            self.assertIsNotNone(d._preview_win)
            self.assertIsNotNone(d._preview_img)
        finally:
            d.close()

    def test_preview_capture_failure_shows_message(self):
        import tkinter.messagebox as mb
        from app.ui.calibrator import CalibratorDialog

        class Boom:
            def grab(self, key):
                return None

        d = CalibratorDialog(self.root, self.region_cfg, Boom())
        orig = mb.showwarning
        calls = []
        mb.showwarning = lambda *a, **k: calls.append(a)
        try:
            d.top.withdraw()
            d.preview()
            self.assertEqual(len(calls), 1)
        finally:
            mb.showwarning = orig
            d.close()

    def test_switch_region_updates_coords(self):
        from app.ui.calibrator import CalibratorDialog
        d = CalibratorDialog(self.root, self.region_cfg, self.FakeCapture())
        try:
            d.top.withdraw()
            first = d.coords_var.get()
            d.combo.current(1)
            d._refresh()
            self.assertNotEqual(first, d.coords_var.get() or first)
        finally:
            d.close()


# ==========================================================================
class TestImportExportHelpers(TempCase):
    """改进点二：CSV / JSON 读写辅助函数（不需要 Tk）。"""

    ROWS = [
        {"player_name": "PlayerX", "note": "恶意TK",
         "created_at": "2026-01-01 10:00:00"},
        {"player_name": "John Doe", "note": "空格名, 含逗号",
         "created_at": "2026-01-03 12:00:00"},
    ]

    def test_csv_has_bom_for_excel(self):
        path = self.path("out.csv")
        gui_mod.write_csv(path, self.ROWS)
        with open(path, "rb") as f:
            self.assertTrue(f.read(3) == b"\xef\xbb\xbf", "CSV 必须带 UTF-8 BOM")

    def test_csv_roundtrip(self):
        path = self.path("out.csv")
        gui_mod.write_csv(path, self.ROWS)
        back = gui_mod.parse_csv(path)
        self.assertEqual(len(back), 2)
        self.assertEqual(back[0]["player_name"], "PlayerX")
        self.assertEqual(back[0]["note"], "恶意TK")
        self.assertEqual(back[0]["created_at"], "2026-01-01 10:00:00")
        self.assertEqual(back[1]["note"], "空格名, 含逗号")   # 逗号被正确转义

    def test_csv_header_has_only_three_fields(self):
        path = self.path("out.csv")
        gui_mod.write_csv(path, self.ROWS)
        with open(path, encoding="utf-8-sig", newline="") as f:
            header = f.readline().strip()
        self.assertEqual(header, "player_name,note,created_at")
        for gone in ("player_id", "tk_count", "encounter_count", "last_seen"):
            self.assertNotIn(gone, header)

    def test_json_structure(self):
        path = self.path("out.json")
        gui_mod.write_json(path, self.ROWS)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["exported_count"], 2)
        self.assertEqual(len(data["entries"]), 2)
        self.assertEqual(data["entries"][0]["player_name"], "PlayerX")
        self.assertIn("fields", data)
        self.assertEqual(data["fields"], list(gui_mod.IO_FIELDS))

    def test_json_roundtrip(self):
        path = self.path("out.json")
        gui_mod.write_json(path, self.ROWS)
        back = gui_mod.parse_json(path)
        self.assertEqual(len(back), 2)
        self.assertEqual(back[0]["created_at"], "2026-01-01 10:00:00")
        self.assertEqual(back[1]["player_name"], "John Doe")

    def test_json_accepts_bare_list(self):
        path = self.path("list.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"player_name": "X"}], f)
        self.assertEqual(len(gui_mod.parse_json(path)), 1)

    def test_json_accepts_bom_file(self):
        """用记事本另存过的名单（UTF-8 BOM）也要能导入。"""
        path = self.path("bom.json")
        with open(path, "w", encoding="utf-8-sig") as f:
            json.dump([{"player_name": "X"}], f)
        self.assertEqual(len(gui_mod.parse_json(path)), 1)

    def test_json_accepts_alt_container_keys(self):
        for key in ("entries", "blacklist", "items", "data"):
            path = self.path(f"{key}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({key: [{"player_name": "x"}]}, f)
            self.assertEqual(len(gui_mod.parse_json(path)), 1, key)

    def test_json_rejects_bad_shape(self):
        path = self.path("bad.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"nope": 1}, f)
        with self.assertRaises(ValueError):
            gui_mod.parse_json(path)

    def test_read_entries_dispatches_by_extension(self):
        csv_path = self.path("a.csv")
        json_path = self.path("a.json")
        gui_mod.write_csv(csv_path, self.ROWS)
        gui_mod.write_json(json_path, self.ROWS)
        self.assertEqual(len(gui_mod.read_entries(csv_path)), 2)
        self.assertEqual(len(gui_mod.read_entries(json_path)), 2)

    def test_non_json_extension_treated_as_csv(self):
        path = self.path("a.txt")
        gui_mod.write_csv(path, self.ROWS)
        self.assertEqual(len(gui_mod.read_entries(path)), 2)

    def test_export_targets_are_readable_by_db(self):
        """导出的文件能被 db.import_entries 直接吃下去。"""
        db = BlacklistDB(self.path("bl.db"))
        try:
            path = self.path("out.csv")
            gui_mod.write_csv(path, self.ROWS)
            res = db.import_entries(gui_mod.read_entries(path), strategy="skip")
            self.assertEqual(res["inserted"], 2)
            row = [r for r in db.get_all() if r["player_name"] == "PlayerX"][0]
            self.assertEqual(row["note"], "恶意TK")
            self.assertEqual(row["created_at"], "2026-01-01 10:00:00")
        finally:
            db.close()


# ==========================================================================
@unittest.skipUnless(TK_OK, "当前会话没有可用桌面")
class TestChatScanAndIOGui(TempCase):
    """改进点一/二在 GUI 层的接线。"""

    class FakeScanner:
        def __init__(self, status="ok", hits=1):
            self.calls = []
            self.busy = False
            self.status = status
            self.hits = hits
            self.on_result = None

        def is_busy(self):
            return self.busy

        def scan_now(self, source=None):
            self.calls.append(source)
            # 和真的 ChatScanner 一样：先报"开始"，再报结果。
            # 界面（按钮 / F8 热键）都靠这个回调播报。
            if self.on_result is not None:
                self.on_result({"status": "started",
                                "source": source or "chat_manual"})
            result = {"status": self.status, "hits": self.hits,
                      "elapsed_ms": 42.0, "processed": self.hits,
                      "source": source or "chat_manual",
                      "names": ["PlayerX"] if self.hits else [],
                      "recorded": self.hits, "deduped": 0}
            if self.on_result is not None:
                self.on_result(result)
            return result

    def setUp(self):
        super().setUp()
        self.db = BlacklistDB(self.path("bl.db"))
        self.db.add("PlayerX", "恶意TK")
        self.region_cfg = RegionConfig(self.path("user_config.json"))
        self.notif_cfg = NotificationConfig(self.path("notification.json"))
        self.scanner = self.FakeScanner()
        from app.settings.hotkey_config import HotkeyConfig
        from app.ui.gui import BlacklistGUI
        self.hotkey_cfg = HotkeyConfig(self.path("hotkey.json"))
        self.gui = BlacklistGUI(self.db, self.notif_cfg, self.region_cfg,
                                matcher=Matcher(self.db),
                                chat_scanner=self.scanner,
                                hotkey_config=self.hotkey_cfg)
        self.gui.root.withdraw()

    def tearDown(self):
        try:
            self.gui.destroy()
        except Exception:                            # noqa: BLE001
            pass
        # 在主线程里立刻回收 Tk 对象：Tk 变量/图像若被后台线程或循环引用留到
        # 解释器退出时才析构，Tcl 会在错误的线程删掉自己的 async handler，
        # 进程直接 abort（Tcl_AsyncDelete: async handler deleted by the wrong
        # thread）。测试里反复建/销毁 Tk 解释器时这个偶发崩溃很明显。
        self.gui = None
        gc.collect()
        self.db.close()
        super().tearDown()

    def _pump(self, seconds=1.0):
        end = time.time() + seconds
        while time.time() < end:
            self.gui.root.update()
            self.gui._drain_once()
            time.sleep(0.02)
        self.gui.root.update()

    # ---- 扫描聊天框按钮 ----
    def test_scan_button_exists(self):
        texts = []

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c.winfo_class() == "TButton":
                        texts.append(c.cget("text"))
                except Exception:                    # noqa: BLE001
                    pass
                walk(c)

        walk(self.gui.root)
        for expected in ("扫描聊天框", "导入", "导出"):
            self.assertIn(expected, texts, f"工具栏缺少按钮：{expected}")

    def test_scan_chat_now_calls_scanner(self):
        self.gui.scan_chat_now()
        self._pump(0.6)
        self.assertEqual(len(self.scanner.calls), 1)
        self.assertIn("聊天框扫描完成", self.gui.status_var.get())

    def test_scan_chat_reports_hits(self):
        self.scanner.hits = 3
        self.gui.scan_chat_now()
        self._pump(0.6)
        self.assertIn("命中 3 条", self.gui.status_var.get())

    def test_scan_chat_busy_is_ignored(self):
        self.scanner.busy = True
        self.gui.scan_chat_now()
        self._pump(0.3)
        self.assertEqual(self.scanner.calls, [])
        self.assertIn("还没结束", self.gui.status_var.get())

    def test_scan_chat_empty_and_error_messages(self):
        self.scanner.status = "empty"
        self.gui.scan_chat_now()
        self._pump(0.5)
        self.assertIn("为空", self.gui.status_var.get())

        self.scanner.status = "error"
        self.gui.scan_chat_now()
        self._pump(0.5)
        self.assertIn("失败", self.gui.status_var.get())

    def test_scan_chat_without_scanner(self):
        self.gui.chat_scanner = None
        self.gui.scan_chat_now()                     # 不应抛异常
        self.assertIn("不可用", self.gui.status_var.get())

    def test_scan_chat_scanner_exception_is_reported(self):
        class Boom(self.FakeScanner):
            def scan_now(self, source=None):
                raise RuntimeError("scanner boom")

        self.gui.chat_scanner = Boom()
        self.gui.scan_chat_now()
        self._pump(0.5)
        self.assertIn("失败", self.gui.status_var.get())

    # ---- 导出 ----
    def test_export_worker_writes_csv(self):
        path = self.path("out.csv")
        self.gui._export_worker(path)
        self._pump(0.3)
        self.assertTrue(os.path.exists(path))
        rows = gui_mod.read_entries(path)
        self.assertEqual(len(rows), 1)
        self.assertIn("已导出 1 条", self.gui.status_var.get())

    def test_export_worker_writes_json(self):
        path = self.path("out.json")
        self.gui._export_worker(path)
        self._pump(0.3)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(len(gui_mod.parse_json(path)), 1)

    def test_export_failure_reports(self):
        import tkinter.messagebox as mb
        calls = []
        orig = mb.showerror
        mb.showerror = lambda *a, **k: calls.append(a)
        try:
            self.gui._export_worker(os.path.join(self.tmp, "no", "dir.csv"))
            self._pump(0.4)
            self.assertEqual(len(calls), 1)
        finally:
            mb.showerror = orig

    # ---- 导入 ----
    def test_import_worker_inserts_and_refreshes(self):
        import tkinter.messagebox as mb
        path = self.path("in.csv")
        gui_mod.write_csv(path, [
            {"player_name": "NewGuy", "note": "n"},
            {"player_name": "PlayerX", "note": "dup"},
        ])
        calls = []
        orig = mb.showinfo
        mb.showinfo = lambda *a, **k: calls.append(a)
        try:
            self.gui._import_worker(path, "skip")
            self._pump(0.5)
            self.assertEqual(len(calls), 1)
            self.assertIn("新增 1", calls[0][1])
            self.assertIn("跳过 1", calls[0][1])
            # Treeview 已刷新
            self.assertEqual(len(self.gui.tree.get_children()), 2)
        finally:
            mb.showinfo = orig

    def test_import_update_note_strategy(self):
        import tkinter.messagebox as mb
        path = self.path("in.csv")
        created = self.db.get(1)["created_at"]
        gui_mod.write_csv(path, [
            {"player_name": "PlayerX", "note": "改过的备注",
             "created_at": "2000-01-01 00:00:00"},
        ])
        orig = mb.showinfo
        mb.showinfo = lambda *a, **k: None
        try:
            self.gui._import_worker(path, "update_note")
            self._pump(0.5)
            row = self.db.get(1)
            self.assertEqual(row["note"], "改过的备注")
            self.assertEqual(row["created_at"], created)   # 添加时间保持本机的
        finally:
            mb.showinfo = orig

    def test_import_overwrite_strategy(self):
        import tkinter.messagebox as mb
        path = self.path("in.csv")
        gui_mod.write_csv(path, [
            {"player_name": "PlayerX", "note": "覆盖",
             "created_at": "2000-01-01 00:00:00"},
        ])
        orig = mb.showinfo
        mb.showinfo = lambda *a, **k: None
        try:
            self.gui._import_worker(path, "overwrite")
            self._pump(0.5)
            row = self.db.get(1)
            self.assertEqual(row["note"], "覆盖")
            self.assertEqual(row["created_at"], "2000-01-01 00:00:00")
        finally:
            mb.showinfo = orig

    def test_import_bad_file_reports_error(self):
        import tkinter.messagebox as mb
        path = self.path("bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        calls = []
        orig = mb.showerror
        mb.showerror = lambda *a, **k: calls.append(a)
        try:
            self.gui._import_worker(path, "skip")
            self._pump(0.5)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.db.get_count(), 1)     # 原数据没动
        finally:
            mb.showerror = orig

    def test_import_empty_file_reports_error(self):
        import tkinter.messagebox as mb
        path = self.path("empty.csv")
        gui_mod.write_csv(path, [])
        calls = []
        orig = mb.showerror
        mb.showerror = lambda *a, **k: calls.append(a)
        try:
            self.gui._import_worker(path, "skip")
            self._pump(0.5)
            self.assertEqual(len(calls), 1)
        finally:
            mb.showerror = orig

    # ---- 热键开关 & 去重缓存 ----
    def test_clear_dedup_cache(self):
        self.gui.scheduler = FakeSchedulerForGui()
        self.gui.scheduler._recent_hits[1] = time.time()
        self.gui._clear_dedup_cache()
        self.assertEqual(self.gui.scheduler.dedup_cache_size(), 0)
        self.assertIn("去重缓存", self.gui.status_var.get())

    def test_hotkey_toggle_button_reflects_state(self):
        """F8 开关就是 ① 上那个按钮：文字写"动作"，金色 = 已启用。"""
        self.gui.chat_hotkey_var.set(True)
        self.gui._sync_hotkey_ui()
        self.assertIn("停用", self.gui.hotkey_btn.cget("text"))
        self.assertEqual(self.gui.hotkey_btn.cget("style"), "Accent.TButton")

        self.gui.chat_hotkey_var.set(False)
        self.gui._sync_hotkey_ui()
        self.assertIn("启用", self.gui.hotkey_btn.cget("text"))
        self.assertEqual(self.gui.hotkey_btn.cget("style"), "Bar.TButton")

    def test_hotkey_button_actually_toggles(self):
        """点一下 [启用 F8 扫描] 必须真的把热键打开（自己取反）。

        旧实现读 chat_hotkey_var（菜单时代留给 Checkbutton 的），而按钮从不写它
        → 每次点都提交"当前值"，热键永远启动不了 —— 表现得就像"根本没有启动
        按钮"（用户实测反馈）。
        """
        seen = []
        self.gui.on_chat_hotkey = seen.append
        self.assertFalse(self.gui.chat_hotkey_var.get())

        self.gui.hotkey_btn.invoke()                     # 真点按钮
        self.assertTrue(self.gui.chat_hotkey_var.get())
        self.assertTrue(self.hotkey_cfg.enabled)         # 已落盘
        self.assertEqual([c["enabled"] for c in seen], [True])   # 回调收到配置 dict
        self.assertEqual(self.gui.hotkey_btn.cget("style"), "Accent.TButton")

        self.gui.hotkey_btn.invoke()
        self.assertFalse(self.gui.chat_hotkey_var.get())
        self.assertFalse(self.hotkey_cfg.enabled)
        self.assertEqual([c["enabled"] for c in seen], [True, False])
        self.assertEqual(self.gui.hotkey_btn.cget("style"), "Bar.TButton")

    def test_set_hotkey_enabled_persists_and_calls_back(self):
        seen = []
        self.gui.on_chat_hotkey = seen.append
        self.gui.set_hotkey_enabled(True)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["enabled"])              # 回调收到配置 dict
        self.assertTrue(self.hotkey_cfg.enabled)         # 已落盘

        self.gui.set_hotkey_enabled(False)
        self.assertEqual(len(seen), 2)
        self.assertFalse(seen[1]["enabled"])
        self.assertFalse(self.hotkey_cfg.enabled)

    def test_hotkey_button_shows_custom_key(self):
        self.hotkey_cfg.set_binding(0x77, ctrl=True)     # Ctrl+F8
        self.gui.chat_hotkey_var.set(True)
        self.gui._sync_hotkey_ui()
        self.assertIn("Ctrl+F8", self.gui.hotkey_btn.cget("text"))

    def test_hotkey_toggle_button_is_a_visible_button(self):
        """按钮必须在深色面板上看得出来是按钮（用有边框的 Bar.TButton）。"""
        self.gui.chat_hotkey_var.set(False)
        self.gui._sync_hotkey_ui()
        style = self.gui.hotkey_btn.cget("style")
        self.assertIn(style, ("Bar.TButton", "Accent.TButton"))
        # 未启用时也要是"能点"的样子，而不是 flat 的 TButton
        self.assertNotEqual(style, "TButton")

    def test_hotkey_panel_embedded_in_scan_page(self):
        """快捷键改键 UI 现在嵌在设置分页里（不再是独立对话框）。"""
        self.gui.show_settings_page("scan")
        self.gui.root.update()
        panel = self.gui.hotkey_panel
        self.assertTrue(panel.embedded)
        # 嵌入模式不重复提供「启用」勾选（开关在顶部状态带上）
        self.assertFalse(panel.show_enabled)
        panel.vk_var.set(0x77)
        panel.ctrl_var.set(True)
        panel.save()
        self.assertIn("Ctrl+F8", self.hotkey_cfg.display)
        self.assertIn("已保存", panel.status_var.get())

    def test_toggle_without_hotkey_config(self):
        self.gui.hotkey_config = None
        self.gui.toggle_hotkey()                         # 不应抛异常
        self.assertIn("不可用", self.gui.status_var.get())
        self.assertIn("不可用", self.gui.status_var.get())

    def test_open_hotkey_settings_without_config(self):
        self.gui.hotkey_config = None
        self.gui.open_hotkey_settings()                  # 不应抛异常
        self.assertIn("不可用", self.gui.status_var.get())

    # ---- 自定义按键面板不再显示过期的「未启用」 ----
    def test_hotkey_panel_follows_top_toggle(self):
        """开机时先建了设置页（那时热键是关的），之后打开热键必须同步过去。

        旧实现：面板按当时的 enabled=False 渲染成「F8（未启用）」，之后再没人
        刷新它 —— 顶部写着「停用 F8 扫描」，设置页里却写着「未启用」。
        """
        self.gui.on_chat_hotkey = lambda cfg: ""
        self.gui.show_settings_page("scan")              # 此刻热键是关闭的
        self.gui.root.update()
        panel = self.gui.hotkey_panel
        self.assertIn("未启用", panel.key_text.get())

        self.gui.set_hotkey_enabled(True)                # 顶部按钮打开热键
        self.gui.root.update()
        self.assertNotIn("未启用", panel.key_text.get())
        self.assertIn("F8", panel.key_text.get())

        self.gui.set_hotkey_enabled(False)
        self.gui.root.update()
        self.assertIn("未启用", panel.key_text.get())

    def test_hotkey_panel_disabled_hint_points_at_top_button(self):
        self.gui.on_chat_hotkey = lambda cfg: ""
        self.gui.show_settings_page("scan")
        self.gui.root.update()
        hint = self.gui.hotkey_panel.status_var.get()
        self.assertIn("顶部", hint)

    # ---- 状态栏「最近动态」 ----
    def test_event_feed_is_empty_at_startup(self):
        self.assertEqual(self.gui.event_vars[0].get(), "（暂无动态）")
        self.assertEqual(self.gui.event_vars[1].get(), "")

    def test_notify_event_shows_timestamped_lines_newest_first(self):
        self.gui.notify_event("第一条", "info")
        self.gui.notify_event("第二条", "hit")
        self.assertIn("第二条", self.gui.event_vars[0].get())
        self.assertIn("第一条", self.gui.event_vars[1].get())
        # 带时间戳，才知道"什么时候发生的"
        self.assertRegex(self.gui.event_vars[0].get(), r"^\d{2}:\d{2}:\d{2}  ")
        self.assertEqual(self.gui.event_labels[0].cget("style"), "Hit.TLabel")

    def test_event_feed_keeps_only_the_last_rows(self):
        from app.ui.gui import EVENT_ROWS
        for i in range(EVENT_ROWS + 3):
            self.gui.notify_event(f"事件{i}")
        self.assertIn(f"事件{EVENT_ROWS + 2}", self.gui.event_vars[0].get())
        self.assertNotIn("事件0", " ".join(v.get() for v in self.gui.event_vars))

    def test_chat_scan_result_lands_in_event_feed(self):
        self.gui.scan_chat_now()
        self._pump(0.6)
        joined = " ".join(v.get() for v in self.gui.event_vars)
        self.assertIn("聊天框扫描完成", joined)
        # "开始扫描"只是过渡态，不该顶掉结果行
        self.assertNotIn("开始扫描聊天框", joined)

    def test_hotkey_scan_is_reported_with_key_name(self):
        """按 F8 触发的那次扫描必须在界面上留痕（旧版只剩一行日志）。"""
        self.hotkey_cfg.set_binding(0x77)                 # F8
        self.gui.chat_scanner.on_result(
            {"status": "ok", "hits": 1, "names": ["PlayerX"], "recorded": 1,
             "deduped": 0, "elapsed_ms": 786.0, "source": "chat_hotkey",
             "processed": 1})
        self.gui._drain_once()
        text = self.gui.status_var.get()
        self.assertIn("按 F8", text)
        self.assertIn("聊天框扫描完成", text)
        self.assertIn("PlayerX", text)
        self.assertIn("按 F8", self.gui.event_vars[0].get())

    def test_hotkey_scan_dedup_is_reported(self):
        self.gui.chat_scanner.on_result(
            {"status": "ok", "hits": 1, "names": ["PlayerX"], "recorded": 0,
             "deduped": 1, "elapsed_ms": 795.0, "source": "chat_hotkey",
             "processed": 0})
        self.gui._drain_once()
        self.assertIn("跳过", self.gui.status_var.get())
        self.assertEqual(self.gui.event_labels[0].cget("style"),
                         "Paused.TLabel")

    def test_hotkey_scan_failure_is_reported(self):
        self.gui.chat_scanner.on_result(
            {"status": "error", "error": "聊天框截图失败", "source": "chat_hotkey"})
        self.gui._drain_once()
        self.assertIn("失败", self.gui.status_var.get())
        self.assertEqual(self.gui.event_labels[0].cget("style"),
                         "Danger.TLabel")


class FakeSchedulerForGui:
    """只用于 _clear_dedup_cache 测试。"""

    def __init__(self):
        self._recent_hits = {}

    def clear_dedup_cache(self):
        self._recent_hits.clear()

    def dedup_cache_size(self):
        return len(self._recent_hits)


if __name__ == "__main__":
    unittest.main(verbosity=2)

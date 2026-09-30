# -*- coding: utf-8 -*-
"""阶段 6-7 验收测试：GUI 主界面 / 通知设置 / 校准器（构建级冒烟 + 线程安全）。

这些用例会真的创建 Tk 窗口（创建后立即 withdraw / destroy）。
若当前会话没有可用桌面，则整类跳过。

运行： python -m unittest tests.test_gui -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config                                    # noqa: E402
import gui as gui_mod                            # noqa: E402
from database import BlacklistDB                 # noqa: E402
from matcher import Matcher                      # noqa: E402
from notification_config import NotificationConfig   # noqa: E402
from region_config import RegionConfig           # noqa: E402

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
        from gui import EntryDialog
        # 不进入 wait_window：直接构造内部状态
        dlg = EntryDialog.__new__(EntryDialog)
        dlg.master = self.root
        dlg.result = None
        dlg.top = tk.Toplevel(self.root)
        dlg.top.withdraw()
        dlg.vars = {k: tk.StringVar(value=str((entry or {}).get(k) or ""))
                    for k in ("player_id", "player_name", "tk_count",
                              "evidence_path")}
        dlg.note_text = tk.Text(dlg.top)
        if entry and entry.get("note"):
            dlg.note_text.insert("1.0", entry["note"])
        return dlg

    def test_valid_input(self):
        dlg = self._dialog()
        dlg.vars["player_id"].set("7656119")
        dlg.vars["player_name"].set("PlayerX")
        dlg.vars["tk_count"].set("4")
        dlg.note_text.insert("1.0", "恶意TK")
        dlg._ok()
        self.assertEqual(dlg.result["player_id"], "7656119")
        self.assertEqual(dlg.result["player_name"], "PlayerX")
        self.assertEqual(dlg.result["tk_count"], 4)
        self.assertEqual(dlg.result["note"], "恶意TK")

    def test_requires_id_or_name(self):
        import tkinter.messagebox as mb
        dlg = self._dialog()
        orig = mb.showwarning
        mb.showwarning = lambda *a, **k: None
        try:
            dlg._ok()
            self.assertIsNone(dlg.result)            # 被拦下
        finally:
            mb.showwarning = orig

    def test_bad_tk_count_rejected(self):
        import tkinter.messagebox as mb
        dlg = self._dialog()
        dlg.vars["player_id"].set("x")
        dlg.vars["tk_count"].set("abc")
        orig = mb.showwarning
        mb.showwarning = lambda *a, **k: None
        try:
            dlg._ok()
            self.assertIsNone(dlg.result)
        finally:
            mb.showwarning = orig

    def test_prefilled_from_entry(self):
        dlg = self._dialog({"player_id": "id1", "player_name": "N",
                            "tk_count": 2, "note": "备注"})
        dlg._ok()
        self.assertEqual(dlg.result["tk_count"], 2)
        self.assertEqual(dlg.result["note"], "备注")


# ==========================================================================
@unittest.skipUnless(TK_OK, "当前会话没有可用桌面")
class TestBlacklistGUI(TempCase):
    def setUp(self):
        super().setUp()
        self.db = BlacklistDB(self.path("bl.db"))
        self.db.add("id1", "PlayerX", "恶意TK", 2)
        self.db.add("id2", "John Doe", "空格名", 1)
        self.region_cfg = RegionConfig(self.path("user_config.json"))
        self.notif_cfg = NotificationConfig(self.path("notification.json"))
        self.matcher = Matcher(self.db)
        self.scheduler = None

        from gui import BlacklistGUI
        self.gui = BlacklistGUI(self.db, self.notif_cfg, self.region_cfg,
                                matcher=self.matcher)
        self.gui.root.withdraw()

    def tearDown(self):
        try:
            self.gui.destroy()
        except Exception:                            # noqa: BLE001
            pass
        self.db.close()
        super().tearDown()

    def test_tree_columns(self):
        from gui import COLUMNS, HEADERS
        self.assertEqual(tuple(self.gui.tree["columns"]), COLUMNS)
        for col in COLUMNS:
            self.assertEqual(self.gui.tree.heading(col)["text"], HEADERS[col])

    def test_rows_loaded(self):
        self.assertEqual(len(self.gui.tree.get_children()), 2)

    def test_row_values(self):
        values = self.gui.tree.item("1", "values")
        self.assertEqual(values[0], "id1")
        self.assertEqual(values[1], "PlayerX")
        self.assertEqual(values[3], "2")           # tk_count
        self.assertEqual(values[4], "0")           # encounter_count

    def test_sort_by_column_toggles(self):
        self.gui.sort_by_column("encounter_count")
        self.assertEqual(self.gui.sort_key, "encounter_count")
        self.assertTrue(self.gui.sort_desc)
        self.gui.sort_by_column("encounter_count")
        self.assertFalse(self.gui.sort_desc)
        self.assertIn("升序", self.gui.sort_dir_btn["text"])

    def test_sort_button_label_sync(self):
        self.gui._on_sort_selected()
        self.assertEqual(self.gui.sort_key, "last_seen")

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
        """模拟后台线程命中 → 主线程 drain → UI 立即刷新。"""
        self.gui.enqueue_encounter_update({
            "entry_id": 1, "player_name": "PlayerX",
            "encounter_count": 5, "last_seen": "2026-01-01 00:00:00",
            "score": 100.0, "source": "chat",
        })
        self.gui._drain_once()
        self.assertEqual(self.gui.tree.set("1", "encounter_count"), "5")
        self.assertEqual(self.gui.tree.set("1", "last_seen"),
                         "2026-01-01 00:00:00")
        self.assertEqual(self.gui.session_hit_count, 1)
        self.assertIn("命中黑名单", self.gui.status_var.get())

    def test_encounter_update_for_missing_row_is_safe(self):
        self.gui.enqueue_encounter_update({
            "entry_id": 999, "player_name": "Ghost",
            "encounter_count": 1, "last_seen": "x", "score": 90, "source": "s"})
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

    # ---- 右键重置 ----
    def test_reset_encounter(self):
        self.db.record_encounter(1, 100.0, "chat", "PlayerX", "")
        self.gui.refresh()
        self.gui._reset_encounter("1")
        self.assertEqual(self.gui.tree.set("1", "encounter_count"), "0")
        self.assertEqual(self.db.get(1)["encounter_count"], 0)

    def test_reset_last_seen(self):
        self.db.record_encounter(1, 100.0, "chat", "PlayerX", "")
        self.gui.refresh()
        self.gui._reset_last_seen("1")
        self.assertEqual(self.gui.tree.set("1", "last_seen"), "")
        self.assertIsNone(self.db.get(1)["last_seen"])

    def test_reset_only_affects_target(self):
        self.db.record_encounter(1, 100.0, "chat", "PlayerX", "")
        self.db.record_encounter(2, 100.0, "chat", "John Doe", "")
        self.gui.refresh()
        self.gui._reset_encounter("1")
        self.assertEqual(self.db.get(2)["encounter_count"], 1)

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

    # ---- 增删改 ----
    def test_delete_entry(self):
        self.db.delete(1)
        self.gui.refresh()
        self.assertEqual(len(self.gui.tree.get_children()), 1)

    def test_after_db_change_reloads_matcher(self):
        self.db.add("id3", "NewGuy")
        self.gui._after_db_change()
        self.assertEqual(len(self.gui.tree.get_children()), 3)
        self.assertIsNotNone(self.matcher.match_one("NewGuy"))

    def test_export_list(self):
        import csv
        out = self.path("out.csv")
        rows = self.db.get_all()
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([__import__("gui").HEADERS[c]
                        for c in __import__("gui").COLUMNS])
            for r in rows:
                w.writerow(__import__("gui").BlacklistGUI._row_values(r))
        with open(out, encoding="utf-8-sig") as f:
            self.assertEqual(len(list(csv.reader(f))), 3)

    def test_right_click_menu_exists(self):
        labels = [self.gui.menu.entrycget(i, "label")
                  for i in range(self.gui.menu.index("end") + 1)
                  if self.gui.menu.type(i) == "command"]
        for expected in ("编辑", "删除", "清零遇到次数", "重置最后遇见时间"):
            self.assertIn(expected, labels)

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
        """最小宽度下两行工具栏与底部栏都不能被挤掉。"""
        import gui as gui_mod
        min_w, min_h = 960, 480
        self.gui.root.minsize(min_w, min_h)
        self.gui.root.geometry(f"{min_w}x{min_h}+0+0")
        self.gui.root.deiconify()
        self.gui.root.update()
        self.gui.root.update_idletasks()

        bad = self._overflowing_children(self.gui.root, min_w, min_h)
        self.assertEqual(bad, [], f"以下控件在 {min_w}x{min_h} 下被挤出窗口: {bad}")
        self.gui.root.withdraw()

    def test_toolbar_buttons_all_present(self):
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
        for expected in ("添加", "编辑", "删除", "搜索", "清空",
                         "校准区域", "恢复全部默认", "通知设置", "最小化到托盘"):
            self.assertIn(expected, texts, f"工具栏缺少按钮：{expected}")
        self.assertIn("↓ 降序", texts)


# ==========================================================================
@unittest.skipUnless(TK_OK, "当前会话没有可用桌面")
class TestNotificationDialog(TempCase):
    def setUp(self):
        super().setUp()
        import tkinter as tk
        from notifier import Notifier
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
        from gui_notification import NotificationSettingsDialog
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
        from calibrator import CalibratorDialog
        d = CalibratorDialog(self.root, self.region_cfg, self.FakeCapture())
        try:
            d.top.withdraw()
            self.assertIn("left=", d.coords_var.get())
            self.assertEqual(d.source_var.get(), "内置默认值")
        finally:
            d.close()

    def test_reset_region(self):
        from calibrator import CalibratorDialog
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
        from calibrator import CalibratorDialog
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
        from calibrator import CalibratorDialog
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
        from calibrator import CalibratorDialog

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
        from calibrator import CalibratorDialog
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
        {"player_id": "76561198000000001", "player_name": "PlayerX",
         "note": "恶意TK", "tk_count": 3, "encounter_count": 5,
         "created_at": "2026-01-01 10:00:00",
         "last_seen": "2026-02-02 11:00:00"},
        {"player_id": "76561198000000002", "player_name": "John Doe",
         "note": "空格名, 含逗号", "tk_count": 1, "encounter_count": 0,
         "created_at": "2026-01-03 12:00:00", "last_seen": None},
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
        self.assertEqual(back[0]["player_id"], "76561198000000001")
        self.assertEqual(back[0]["player_name"], "PlayerX")
        self.assertEqual(back[0]["note"], "恶意TK")
        self.assertEqual(back[0]["tk_count"], "3")        # CSV 里都是字符串
        self.assertEqual(back[0]["encounter_count"], "5")
        self.assertEqual(back[1]["note"], "空格名, 含逗号")   # 逗号被正确转义

    def test_csv_header_fields(self):
        path = self.path("out.csv")
        gui_mod.write_csv(path, self.ROWS)
        with open(path, encoding="utf-8-sig", newline="") as f:
            header = f.readline().strip()
        for field in gui_mod.IO_FIELDS:
            self.assertIn(field, header)

    def test_json_structure(self):
        path = self.path("out.json")
        gui_mod.write_json(path, self.ROWS)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["exported_count"], 2)
        self.assertEqual(len(data["entries"]), 2)
        self.assertEqual(data["entries"][0]["player_name"], "PlayerX")
        self.assertIn("fields", data)

    def test_json_roundtrip(self):
        path = self.path("out.json")
        gui_mod.write_json(path, self.ROWS)
        back = gui_mod.parse_json(path)
        self.assertEqual(len(back), 2)
        self.assertEqual(back[0]["created_at"], "2026-01-01 10:00:00")
        self.assertIsNone(back[1]["last_seen"])

    def test_json_accepts_bare_list(self):
        path = self.path("list.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"player_id": "x", "player_name": "X"}], f)
        self.assertEqual(len(gui_mod.parse_json(path)), 1)

    def test_json_accepts_bom_file(self):
        """用记事本另存过的名单（UTF-8 BOM）也要能导入。"""
        path = self.path("bom.json")
        with open(path, "w", encoding="utf-8-sig") as f:
            json.dump([{"player_id": "x", "player_name": "X"}], f)
        self.assertEqual(len(gui_mod.parse_json(path)), 1)

    def test_json_accepts_alt_container_keys(self):
        for key in ("entries", "blacklist", "items", "data"):
            path = self.path(f"{key}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({key: [{"player_id": "x"}]}, f)
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
            self.assertEqual(row["tk_count"], 3)
            self.assertEqual(row["encounter_count"], 5)
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

        def is_busy(self):
            return self.busy

        def scan_now(self, source=None):
            self.calls.append(source)
            return {"status": self.status, "hits": self.hits,
                    "elapsed_ms": 42.0, "processed": self.hits}

    def setUp(self):
        super().setUp()
        self.db = BlacklistDB(self.path("bl.db"))
        self.db.add("id1", "PlayerX", "恶意TK", 2)
        self.region_cfg = RegionConfig(self.path("user_config.json"))
        self.notif_cfg = NotificationConfig(self.path("notification.json"))
        self.scanner = self.FakeScanner()
        from hotkey_config import HotkeyConfig
        from gui import BlacklistGUI
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
            {"player_id": "new1", "player_name": "NewGuy", "note": "n",
             "tk_count": 4, "encounter_count": 0},
            {"player_id": "id1", "player_name": "PlayerX", "note": "dup"},
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
        gui_mod.write_csv(path, [
            {"player_id": "id1", "player_name": "PlayerX", "note": "改过的备注",
             "tk_count": 9, "encounter_count": 77},
        ])
        orig = mb.showinfo
        mb.showinfo = lambda *a, **k: None
        try:
            self.gui._import_worker(path, "update_note")
            self._pump(0.5)
            row = self.db.get(1)
            self.assertEqual(row["note"], "改过的备注")
            self.assertEqual(row["tk_count"], 9)
            self.assertEqual(row["encounter_count"], 0)   # 保留本机计数
        finally:
            mb.showinfo = orig

    def test_import_overwrite_strategy(self):
        import tkinter.messagebox as mb
        path = self.path("in.csv")
        gui_mod.write_csv(path, [
            {"player_id": "id1", "player_name": "PlayerX", "note": "覆盖",
             "tk_count": 9, "encounter_count": 77},
        ])
        orig = mb.showinfo
        mb.showinfo = lambda *a, **k: None
        try:
            self.gui._import_worker(path, "overwrite")
            self._pump(0.5)
            self.assertEqual(self.db.get(1)["encounter_count"], 77)
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

    def test_menu_has_chat_hotkey_submenu(self):
        menu = self.gui.chat_menu
        labels = []
        for i in range(menu.index("end") + 1):
            try:
                labels.append(menu.entrycget(i, "label"))
            except Exception:                        # noqa: BLE001
                pass
        self.assertIn("立即扫描聊天框", labels)
        self.assertIn("自定义快捷键…", labels)
        self.assertIn("清空命中去重缓存", labels)
        # 启用项会把当前按键名带在括号里
        self.assertTrue(any(lab.startswith("启用热键扫描")
                            for lab in labels), labels)
        self.assertIn("F8", " ".join(labels))

    def test_toggle_chat_hotkey_persists_and_calls_back(self):
        seen = []
        self.gui.on_chat_hotkey = seen.append
        self.gui.chat_hotkey_var.set(True)
        self.gui._toggle_chat_hotkey()
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["enabled"])              # 回调收到配置 dict
        self.assertTrue(self.hotkey_cfg.enabled)         # 已落盘

        self.gui.chat_hotkey_var.set(False)
        self.gui._toggle_chat_hotkey()
        self.assertEqual(len(seen), 2)
        self.assertFalse(seen[1]["enabled"])
        self.assertFalse(self.hotkey_cfg.enabled)

    def test_menu_label_shows_custom_key(self):
        self.hotkey_cfg.set_binding(0x77, ctrl=True)     # Ctrl+F8
        self.gui._sync_hotkey_menu_label()
        label = self.gui.chat_menu.entrycget(self.gui._hotkey_menu_index,
                                             "label")
        self.assertIn("Ctrl+F8", label)

    def test_toggle_without_hotkey_config(self):
        self.gui.hotkey_config = None
        self.gui.chat_hotkey_var.set(True)
        self.gui._toggle_chat_hotkey()                   # 不应抛异常
        self.assertIn("不可用", self.gui.status_var.get())

    def test_open_hotkey_settings_without_config(self):
        self.gui.hotkey_config = None
        self.gui.open_hotkey_settings()                  # 不应抛异常
        self.assertIn("不可用", self.gui.status_var.get())


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

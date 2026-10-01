# -*- coding: utf-8 -*-
"""阶段 1-3 验收测试：config / region / keyword / notification / database / matcher.

运行： python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import config  # noqa: E402
from app.core.database import BlacklistDB                # noqa: E402
from app.core.matcher import Matcher                     # noqa: E402
from app.settings.notification_config import NotificationConfig, ensure_notification_file  # noqa: E402
from app.settings.region_config import RegionConfig          # noqa: E402

TMP_ROOT = os.path.join(_ROOT, ".test_tmp")


class TempCase(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.tmp = os.path.join(TMP_ROOT, self._testMethodName)
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


# ==========================================================================
class TestConfig(TempCase):
    def test_paths_are_absolute_and_under_base(self):
        for p in (config.DATA_DIR, config.DB_PATH,
                  config.NOTIFICATION_PATH, config.USER_CONFIG_PATH,
                  config.LOG_PATH, config.DEFAULT_ICON_PATH):
            self.assertTrue(os.path.isabs(p), p)
            self.assertTrue(p.startswith(config.BASE_DIR), p)

    def test_ensure_dirs(self):
        config.ensure_dirs()
        for d in config.DIRECTORIES:
            self.assertTrue(os.path.isdir(d), d)

    def test_no_deleted_constants(self):
        """已删除的旧常量不应存在（含本轮改造删掉的聊天框/关键词常量）。"""
        for name in ("FALLBACK_INTERVAL", "FALLBACK_SESSION", "HOTKEY_VK",
                     "HOTKEY_SESSION", "CHAT_SESSION",
                     # 本轮改造：聊天框改按需扫描后删掉的
                     "CHAT_POLL_INTERVAL", "CHAT_FULL_SCAN_INTERVAL",
                     "CHAT_HASH_SIZE", "CHAT_TEXT_HASH_CACHE_SIZE",
                     "NOTIFY_THROTTLE_SECONDS",
                     "DEFAULT_KEYWORDS", "KEYWORDS_PATH"):
            self.assertFalse(hasattr(config, name), name)

    def test_new_chat_and_dedup_constants(self):
        self.assertEqual(config.HIT_DEDUP_WINDOW, 30)
        self.assertIs(config.CHAT_SCAN_HOTKEY_ENABLED, False)
        self.assertEqual(config.CHAT_SCAN_HOTKEY_VK, 0x77)   # F8
        self.assertGreaterEqual(config.MAX_NOTIFY_STACK, 1)
        # 保留项
        for name in ("ESC_POLL_INTERVAL", "ESC_SESSION", "COLD_START_DELAY",
                     "COLD_START_SESSION", "MATCH_THRESHOLD",
                     "OCR_CONFIDENCE_MIN"):
            self.assertTrue(hasattr(config, name), name)


# ==========================================================================
# ==========================================================================
class TestBundledAssetSeeding(TempCase):
    """打包版把图标打进 _internal；用户删掉 data/ 后要能自动补回来。

    否则 README 教的「删掉整个 data/ 彻底重置」会让应用图标消失、自检报错。
    """

    def _fake_bundle(self, names=config.BUNDLED_ASSETS):
        bundle = self.path("_internal", "data", "assets")
        os.makedirs(bundle, exist_ok=True)
        for name in names:
            with open(os.path.join(bundle, name), "wb") as f:
                f.write(b"BUNDLED")
        return self.path("_internal")

    def _with_assets_dir(self, assets):
        old = config.ASSETS_DIR
        config.ASSETS_DIR = assets
        self.addCleanup(setattr, config, "ASSETS_DIR", old)

    def test_seeds_missing_assets_from_bundle(self):
        bundle = self._fake_bundle()
        assets = self.path("data", "assets")
        os.makedirs(assets, exist_ok=True)
        self._with_assets_dir(assets)
        with mock.patch.object(sys, "_MEIPASS", bundle, create=True):
            config._seed_bundled_assets()
        for name in config.BUNDLED_ASSETS:
            self.assertTrue(os.path.exists(os.path.join(assets, name)), name)

    def test_keeps_user_file_with_same_name(self):
        bundle = self._fake_bundle()
        assets = self.path("data", "assets")
        os.makedirs(assets, exist_ok=True)
        mine = os.path.join(assets, "app_icon.png")
        with open(mine, "wb") as f:
            f.write(b"MINE")
        self._with_assets_dir(assets)
        with mock.patch.object(sys, "_MEIPASS", bundle, create=True):
            config._seed_bundled_assets()
        with open(mine, "rb") as f:
            self.assertEqual(f.read(), b"MINE")      # 用户自己的不能被覆盖

    def test_dev_mode_is_noop(self):
        bundle = self._fake_bundle()
        assets = self.path("data", "assets")
        os.makedirs(assets, exist_ok=True)
        self._with_assets_dir(assets)
        # 开发态没有 sys._MEIPASS
        if hasattr(sys, "_MEIPASS"):
            self.skipTest("当前环境带了 _MEIPASS")
        config._seed_bundled_assets()
        self.assertEqual(os.listdir(assets), [])

    def test_ensure_dirs_creates_everything(self):
        base = self.path("fresh")
        data = os.path.join(base, "data")
        old = (config.DATA_DIR, config.ASSETS_DIR, config.EVIDENCE_DIR,
               config.LOG_DIR, config.DIRECTORIES)
        config.DATA_DIR = data
        config.ASSETS_DIR = os.path.join(data, "assets")
        config.EVIDENCE_DIR = os.path.join(data, "evidence")
        config.LOG_DIR = os.path.join(data, "logs")
        config.DIRECTORIES = (config.DATA_DIR, config.ASSETS_DIR,
                              config.EVIDENCE_DIR, config.LOG_DIR)
        try:
            config.ensure_dirs()
        finally:
            (config.DATA_DIR, config.ASSETS_DIR, config.EVIDENCE_DIR,
             config.LOG_DIR, config.DIRECTORIES) = old
        for sub in ("", "assets", "evidence", "logs"):
            self.assertTrue(os.path.isdir(os.path.join(data, sub)), sub)


# ==========================================================================
class TestRegionConfig(TempCase):
    def setUp(self):
        super().setUp()
        self.cfg = RegionConfig(self.path("user_config.json"))

    def test_defaults_when_no_file(self):
        self.assertEqual(self.cfg.get("chat_event"),
                         config.DEFAULT_REGIONS["chat_event"])
        self.assertFalse(self.cfg.is_customized("chat_event"))
        self.assertEqual(self.cfg.source_of("chat_event"), "default")
        self.assertEqual(len(self.cfg.get_all()), 3)

    def test_set_persist_and_reload(self):
        self.cfg.set("chat_event", {"left": 1, "top": 2, "width": 30, "height": 40})
        self.assertTrue(self.cfg.is_customized("chat_event"))
        self.assertEqual(self.cfg.get("chat_event")["width"], 30)
        again = RegionConfig(self.path("user_config.json"))
        self.assertEqual(again.get("chat_event"),
                         {"left": 1, "top": 2, "width": 30, "height": 40})

    def test_reset_single_and_all(self):
        self.cfg.set("chat_event", {"left": 1, "top": 2, "width": 30, "height": 40})
        self.cfg.set("player_list_hud", {"left": 5, "top": 6, "width": 70, "height": 80})
        self.cfg.reset("chat_event")
        self.assertEqual(self.cfg.get("chat_event"), config.DEFAULT_REGIONS["chat_event"])
        self.assertTrue(self.cfg.is_customized("player_list_hud"))
        self.cfg.reset_all()
        self.assertFalse(self.cfg.is_customized("player_list_hud"))

    def test_defaults_have_no_size_warning(self):
        self.assertEqual(self.cfg.warnings(), [])

    def test_region_size_warning(self):
        """区域被框成几个像素 → 永远识别不到东西，必须给出告警。

        真实案例：HUD 玩家列表被误框成 10x13 像素。
        """
        self.cfg.set("player_list_hud", {"left": 1603, "top": 571,
                                         "width": 10, "height": 13})
        warns = self.cfg.warnings()
        self.assertEqual(len(warns), 1)
        self.assertIn("10x13", warns[0])
        self.assertIn("HUD", warns[0])
        # 改回一个合理尺寸后告警消失
        self.cfg.set("player_list_hud", {"left": 100, "top": 200,
                                         "width": 400, "height": 120})
        self.assertEqual(self.cfg.warnings(), [])

    def test_region_warnings_helper_handles_garbage(self):
        from app.settings.region_config import region_warnings
        self.assertEqual(region_warnings({}), [])
        self.assertEqual(region_warnings({"chat_event": None}), [])
        self.assertEqual(region_warnings({"chat_event": {"width": "x"}}), [])

    def test_reject_bad_input(self):
        with self.assertRaises(KeyError):
            self.cfg.get("nope")
        with self.assertRaises(KeyError):
            self.cfg.set("nope", {"left": 0, "top": 0, "width": 1, "height": 1})
        with self.assertRaises(ValueError):
            self.cfg.set("chat_event", {"left": 0, "top": 0, "width": 1})
        with self.assertRaises(ValueError):
            self.cfg.set("chat_event", {"left": 0, "top": 0, "width": 0, "height": 10})
        with self.assertRaises(ValueError):
            self.cfg.set("chat_event", {"left": "x", "top": 0, "width": 10, "height": 10})

    def test_set_rect_normalizes_negative_drag(self):
        r = self.cfg.set_rect("chat_event", 100, 100, -40, -30)
        self.assertEqual(r, {"left": 60, "top": 70, "width": 40, "height": 30})

    def test_corrupt_file_falls_back(self):
        with open(self.path("user_config.json"), "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        cfg = RegionConfig(self.path("user_config.json"))
        self.assertEqual(cfg.get("chat_event"), config.DEFAULT_REGIONS["chat_event"])

    def test_reads_file_saved_with_bom(self):
        """记事本另存的 user_config.json（带 BOM）不能被当成损坏配置。"""
        with open(self.path("user_config.json"), "w", encoding="utf-8-sig") as f:
            json.dump({"regions": {"chat_event": {"left": 11, "top": 22,
                                                  "width": 33, "height": 44}}}, f)
        cfg = RegionConfig(self.path("user_config.json"))
        self.assertEqual(cfg.get("chat_event"),
                         {"left": 11, "top": 22, "width": 33, "height": 44})

    def test_unknown_keys_in_file_ignored(self):
        with open(self.path("user_config.json"), "w", encoding="utf-8") as f:
            json.dump({"regions": {"ghost": {"left": 0, "top": 0,
                                             "width": 9, "height": 9}}}, f)
        cfg = RegionConfig(self.path("user_config.json"))
        self.assertEqual(len(cfg.get_all()), 3)


# ==========================================================================
class TestNotificationConfig(TempCase):
    def setUp(self):
        super().setUp()
        self.npath = self.path("notification.json")

    def test_defaults_when_missing(self):
        cfg = NotificationConfig(self.npath)
        data = cfg.get()
        self.assertEqual(data["appearance"]["opacity"], 0.85)
        self.assertEqual(data["image_mode"], "default")
        self.assertEqual(data["sound"]["mode"], "beep")

    def test_deep_merge_partial_file(self):
        with open(self.npath, "w", encoding="utf-8") as f:
            json.dump({"appearance": {"opacity": 0.5, "width": 500}}, f)
        cfg = NotificationConfig(self.npath)
        data = cfg.get()
        self.assertEqual(data["appearance"]["opacity"], 0.5)
        self.assertEqual(data["appearance"]["width"], 500)
        self.assertEqual(data["appearance"]["background_color"], "#2b2b2b")
        self.assertEqual(data["title_template"], "⚠️ 黑名单玩家")

    def test_save_and_get(self):
        cfg = NotificationConfig(self.npath)
        cfg.save({"title_template": "自定义标题", "appearance": {"opacity": 0.4}})
        cfg2 = NotificationConfig(self.npath)
        self.assertEqual(cfg2.get()["title_template"], "自定义标题")
        self.assertEqual(cfg2.get()["appearance"]["opacity"], 0.4)
        self.assertEqual(cfg2.get()["appearance"]["height"], 100)

    def test_reset_removes_file_and_restores_default(self):
        cfg = NotificationConfig(self.npath)
        cfg.save({"title_template": "X"})
        self.assertTrue(os.path.exists(self.npath))
        cfg.reset()
        self.assertFalse(os.path.exists(self.npath))
        self.assertEqual(cfg.get()["title_template"],
                         config.DEFAULT_NOTIFICATION["title_template"])

    def test_corrupt_file_falls_back(self):
        with open(self.npath, "w", encoding="utf-8") as f:
            f.write("{{{")
        cfg = NotificationConfig(self.npath)
        self.assertEqual(cfg.get(), config.DEFAULT_NOTIFICATION)

    def test_reads_file_saved_with_bom(self):
        """记事本另存会写 UTF-8 BOM —— 不能因此静默丢掉用户配置。"""
        with open(self.npath, "w", encoding="utf-8-sig") as f:
            json.dump({"title_template": "带 BOM 的标题",
                       "appearance": {"opacity": 0.42}}, f, ensure_ascii=False)
        cfg = NotificationConfig(self.npath)
        self.assertEqual(cfg.get()["title_template"], "带 BOM 的标题")
        self.assertEqual(cfg.get()["appearance"]["opacity"], 0.42)

    def test_get_value_helper(self):
        cfg = NotificationConfig(self.npath)
        self.assertEqual(cfg.get_value("appearance", "height"), 100)
        self.assertEqual(cfg.get_value("nope", "x", default="d"), "d")

    def test_ensure_notification_file(self):
        real_path = config.NOTIFICATION_PATH
        backup = None
        if os.path.exists(real_path):
            backup = real_path + ".bak"
            shutil.move(real_path, backup)
        try:
            ensure_notification_file()
            self.assertTrue(os.path.exists(real_path))
            with open(real_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("title_template", data)
        finally:
            try:
                os.remove(real_path)
            except OSError:
                pass
            if backup:
                shutil.move(backup, real_path)


# ==========================================================================
class TestVendoredLibPath(TempCase):
    """回归：`python main.py` 直接跑（不设 PYTHONPATH）也要能找到自带依赖。

    真实故障：源码运行没带上 .pylibs → 没有 rapidocr → OCR 全程不可用 →
    每轮扫描"识别 0 个名字"，界面上就是"扫描功能失效"。
    """

    def test_adds_both_dirs_to_sys_path(self):
        from app._bootstrap import add_vendored_libs
        root = self.path("fake_root")
        os.makedirs(os.path.join(root, ".pylibs"))
        os.makedirs(os.path.join(root, ".devtools"))
        before = list(sys.path)
        try:
            added = add_vendored_libs(root)
            self.assertEqual(len(added), 2)
            self.assertIn(os.path.join(root, ".pylibs"), sys.path[:2])
            self.assertIn(os.path.join(root, ".devtools"), sys.path[:2])
            # 再调一次不会重复插入
            self.assertEqual(add_vendored_libs(root), [])
        finally:
            sys.path[:] = before

    def test_missing_dirs_are_skipped_quietly(self):
        from app._bootstrap import add_vendored_libs, vendored_libs_missing
        root = self.path("empty_root")
        os.makedirs(root)
        before = list(sys.path)
        try:
            self.assertEqual(add_vendored_libs(root), [])
            self.assertTrue(vendored_libs_missing(root))
        finally:
            sys.path[:] = before

    def test_main_py_calls_the_bootstrap(self):
        """入口脚本必须在导入 app.application **之前**补路径。"""
        with open(os.path.join(_ROOT, "main.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("add_vendored_libs", src)
        self.assertLess(src.index("add_vendored_libs(_ROOT)"),
                        src.index("from app.application import main"))


# ==========================================================================
class TestDatabase(TempCase):
    def setUp(self):
        super().setUp()
        self.db = BlacklistDB(self.path("blacklist.db"))

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def test_add_get(self):
        eid = self.db.add("PlayerX", "恶意TK")
        row = self.db.get(eid)
        self.assertEqual(row["player_name"], "PlayerX")
        self.assertEqual(row["note"], "恶意TK")
        self.assertTrue(row["created_at"])

    def test_no_legacy_fields_anymore(self):
        """玩家ID / TK次数 / 遇到次数 / 最后遇见 必须彻底消失。"""
        row = self.db.get(self.db.add("PlayerX"))
        for gone in ("player_id", "tk_count", "encounter_count", "last_seen"):
            self.assertNotIn(gone, row)
        cols = {r[1] for r in
                self.db.conn.execute("PRAGMA table_info(blacklist)")}
        self.assertEqual(cols, {"id", "player_name", "note", "created_at"})

    def test_unique_constraint(self):
        self.db.add("Name1")
        with self.assertRaises(ValueError):
            self.db.add("Name1")

    def test_requires_name(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                self.db.add(bad)

    def test_update(self):
        eid = self.db.add("Name1", "n")
        self.assertTrue(self.db.update(eid, note="改过了"))
        self.assertEqual(self.db.get(eid)["note"], "改过了")
        self.assertTrue(self.db.update(eid, player_name="Name2"))
        self.assertEqual(self.db.get(eid)["player_name"], "Name2")
        self.assertFalse(self.db.update(eid))

    def test_update_rejects_empty_name(self):
        eid = self.db.add("Name1")
        with self.assertRaises(ValueError):
            self.db.update(eid, player_name="  ")
        self.assertEqual(self.db.get(eid)["player_name"], "Name1")

    def test_delete_cascades_encounters(self):
        eid = self.db.add("Name1")
        self.db.record_encounter(eid, 100.0, "chat", "Name1", "")
        self.assertEqual(len(self.db.get_encounters(eid)), 1)
        self.assertTrue(self.db.delete(eid))
        self.assertIsNone(self.db.get(eid))
        self.assertEqual(self.db.get_total_encounter_count(), 0)

    def test_search(self):
        self.db.add("Alpha", "备注A")
        self.db.add("Beta", "备注B")
        self.db.add("gamma", "特殊")
        self.assertEqual(len(self.db.search("alph")), 1)
        self.assertEqual(len(self.db.search("备注")), 2)
        self.assertEqual(len(self.db.search("GAMMA")), 1)   # 大小写不敏感
        self.assertEqual(len(self.db.search("")), 3)

    def test_record_encounter_writes_facts(self):
        """命中只"写事实"：一条 encounters 记录，不再累加到名单上。"""
        eid = self.db.add("Name1")
        r1 = self.db.record_encounter(eid, 100.0, "chat", "Name1", "a.jpg")
        self.assertTrue(r1["seen_at"])
        self.assertEqual(r1["today_count"], 1)
        r2 = self.db.record_encounter(eid, 92.5, "esc_menu", "Name12", "b.jpg")
        self.assertEqual(r2["today_count"], 2)
        rows = self.db.get_encounters(eid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name_seen"], "Name12")
        self.assertEqual(rows[0]["match_score"], 92.5)
        self.assertEqual(rows[0]["source"], "esc_menu")
        self.assertEqual(rows[0]["screenshot_path"], "b.jpg")
        self.assertEqual(self.db.get_total_encounter_count(), 2)

    def test_record_encounter_concurrent_no_lost_rows(self):
        eid = self.db.add("Name1")
        n_threads, per_thread = 8, 25
        errors = []

        def worker(tag):
            try:
                for i in range(per_thread):
                    self.db.record_encounter(eid, 100.0, f"t{tag}", "Name1", "")
            except Exception as e:                       # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        expected = n_threads * per_thread
        self.assertEqual(len(self.db.get_encounters(eid, limit=1000)), expected)
        self.assertEqual(self.db.get_total_encounter_count(), expected)

    def test_record_encounter_missing_entry_rolls_back(self):
        with self.assertRaises(ValueError):
            self.db.record_encounter(99999, 100.0, "chat", "x", "")
        self.assertEqual(self.db.get_total_encounter_count(), 0)

    def test_today_count(self):
        eid = self.db.add("Name1")
        self.assertEqual(self.db.get_today_encounter_count(), 0)
        self.db.record_encounter(eid, 100.0, "chat", "Name1", "")
        self.db.record_encounter(eid, 100.0, "chat", "Name1", "")
        self.assertEqual(self.db.get_today_encounter_count(), 2)

    def test_ordering_is_newest_first(self):
        a = self.db.add("A")
        b = self.db.add("B")
        rows = self.db.get_all()
        self.assertEqual(rows[0]["id"], b)            # 默认按添加时间倒序
        self.assertEqual(rows[-1]["id"], a)

    def test_sort_matches_counters_are_gone(self):
        """按遇到次数 / 最后遇见排序的入口必须一起删掉（字段已经不存在）。"""
        self.db.add("A")
        rows = self.db.get_all(order_by="encounter_count")   # 白名单外 → 回退
        self.assertEqual(len(rows), 1)
        rows = self.db.get_all(order_by="last_seen")
        self.assertEqual(len(rows), 1)

    def test_sort_whitelist_blocks_injection(self):
        self.db.add("A")
        rows = self.db.get_all(order_by="id; DROP TABLE blacklist;--")
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.db.get_count(), 1)

    def test_persistence_across_reopen(self):
        eid = self.db.add("Persist")
        self.db.close()
        db2 = BlacklistDB(self.path("blacklist.db"))
        try:
            self.assertEqual(db2.get(eid)["player_name"], "Persist")
        finally:
            db2.close()
        self.db = BlacklistDB(self.path("blacklist.db"))   # tearDown 用

    def test_wal_mode_enabled(self):
        mode = self.db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    # ------------------------------------------------------ 导入导出
    def test_export_all_fields(self):
        self.db.add("Name1", "备注")
        rows = self.db.export_all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), {"player_name", "note", "created_at"})
        self.assertEqual(row["player_name"], "Name1")
        self.assertEqual(row["note"], "备注")

    def test_export_all_ordering_is_stable(self):
        self.db.add("A")
        self.db.add("B")
        self.db.add("C")
        self.assertEqual([r["player_name"] for r in self.db.export_all()],
                         ["A", "B", "C"])

    def test_import_skip_strategy(self):
        self.db.add("Name1", "原始备注")
        res = self.db.import_entries(
            [{"player_name": "Name1", "note": "新备注"}], strategy="skip")
        self.assertEqual(res, {"inserted": 0, "skipped": 1, "updated": 0})
        self.assertEqual(self.db.get_all()[0]["note"], "原始备注")

    def test_import_update_note_keeps_created_at(self):
        eid = self.db.add("Name1", "原始备注")
        created = self.db.get(eid)["created_at"]

        res = self.db.import_entries(
            [{"player_name": "Name1", "note": "新备注",
              "created_at": "2000-01-01 00:00:00"}],
            strategy="update_note")
        self.assertEqual(res, {"inserted": 0, "skipped": 0, "updated": 1})
        row = self.db.get(eid)
        self.assertEqual(row["note"], "新备注")
        self.assertEqual(row["created_at"], created)     # 添加时间保持本机的

    def test_import_overwrite_strategy(self):
        eid = self.db.add("Name1", "原始")
        res = self.db.import_entries(
            [{"player_name": "Name1", "note": "覆盖",
              "created_at": "2000-01-01 00:00:00"}],
            strategy="overwrite")
        self.assertEqual(res["updated"], 1)
        row = self.db.get(eid)
        self.assertEqual(row["note"], "覆盖")
        self.assertEqual(row["created_at"], "2000-01-01 00:00:00")

    def test_import_inserts_new_entries(self):
        res = self.db.import_entries(
            [{"player_name": "NewOne", "note": "n",
              "created_at": "2020-01-01 00:00:00"}],
            strategy="skip")
        self.assertEqual(res, {"inserted": 1, "skipped": 0, "updated": 0})
        row = self.db.get_all()[0]
        self.assertEqual(row["player_name"], "NewOne")
        self.assertEqual(row["created_at"], "2020-01-01 00:00:00")

    def test_import_skips_rows_without_name(self):
        res = self.db.import_entries(
            [{"player_name": ""}, {"note": "只有备注"}, {"player_name": "  "}],
            strategy="skip")
        self.assertEqual(res, {"inserted": 0, "skipped": 3, "updated": 0})
        self.assertEqual(self.db.get_count(), 0)

    def test_import_same_name_case_insensitive_is_duplicate(self):
        self.db.add("NameA")
        res = self.db.import_entries(
            [{"player_name": "namea"}], strategy="skip")
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(self.db.get_count(), 1)
        # 不同名字 → 新条目
        res2 = self.db.import_entries(
            [{"player_name": "NameB"}], strategy="skip")
        self.assertEqual(res2["inserted"], 1)
        self.assertEqual(self.db.get_count(), 2)

    def test_import_ignores_legacy_fields(self):
        """老文件的 player_id / tk_count / encounter_count / last_seen 一律无视。"""
        res = self.db.import_entries(
            [{"player_id": "x", "player_name": "N", "tk_count": "abc",
              "encounter_count": 99, "last_seen": "2020-01-01 00:00:00"}],
            strategy="skip")
        self.assertEqual(res["inserted"], 1)
        row = self.db.get_all()[0]
        self.assertEqual(row["player_name"], "N")
        self.assertNotIn("tk_count", row)
        self.assertNotIn("last_seen", row)

    def test_import_ignores_non_dict_rows(self):
        res = self.db.import_entries(["junk", 42, None], strategy="skip")
        self.assertEqual(res["skipped"], 3)

    def test_import_rejects_unknown_strategy(self):
        with self.assertRaises(ValueError):
            self.db.import_entries([{"player_name": "x"}], strategy="nope")

    def test_import_rolls_back_on_error(self):
        """整批导入中任一条出错 → 全部回滚，数据库保持原状。"""
        self.db.add("KeepMe")
        before = self.db.get_count()

        class Boom:
            """先给出一条合法数据，再抛异常 —— 用来验证整批回滚。"""

            def __iter__(self):
                yield {"player_name": "WillRollback"}
                raise RuntimeError("模拟导入中断")

        with self.assertRaises(RuntimeError):
            self.db.import_entries(Boom(), strategy="skip")

        self.assertEqual(self.db.get_count(), before)   # 没有半途写入
        self.assertEqual([r["player_name"] for r in self.db.get_all()],
                         ["KeepMe"])

    def test_export_import_roundtrip(self):
        self.db.add("Name1", "备注")
        exported = self.db.export_all()

        other = BlacklistDB(self.path("other.db"))
        try:
            res = other.import_entries(exported, strategy="skip")
            self.assertEqual(res["inserted"], 1)
            row = other.get_all()[0]
            self.assertEqual(row["player_name"], "Name1")
            self.assertEqual(row["note"], "备注")
            # 与源库完全一致（时间字段也一致）
            self.assertEqual(row["created_at"], exported[0]["created_at"])
        finally:
            other.close()

    def test_import_empty_list_is_noop(self):
        self.assertEqual(self.db.import_entries([]),
                         {"inserted": 0, "skipped": 0, "updated": 0})
        self.assertEqual(self.db.import_entries(None),
                         {"inserted": 0, "skipped": 0, "updated": 0})


# ==========================================================================
class TestLegacyMigration(TempCase):
    """老库（含玩家ID与统计字段）自动升级，且名字不丢。"""

    def _make_legacy_db(self):
        """手写一份老结构 + 老数据（模拟用户升级前的 blacklist.db）。"""
        import sqlite3
        path = self.path("legacy.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id TEXT NOT NULL,
                player_name TEXT,
                note TEXT,
                tk_count INTEGER DEFAULT 0,
                encounter_count INTEGER DEFAULT 0,
                evidence_path TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen DATETIME,
                UNIQUE(player_id, player_name)
            );
            CREATE TABLE encounters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blacklist_id INTEGER,
                name_seen TEXT,
                match_score REAL,
                source TEXT,
                screenshot_path TEXT,
                seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.executemany(
            "INSERT INTO blacklist (player_id, player_name, note, tk_count, "
            "encounter_count, created_at, last_seen) VALUES (?,?,?,?,?,?,?)",
            [
                ("PlayerZ", "", "名字填在ID栏", 0, 5, "2026-01-01 10:00:00",
                 "2026-01-02 10:00:00"),
                ("？", "", "", 0, 0, "2026-01-01 11:00:00", None),
                ("76561198000000001", "PlayerX", "正常条目", 2, 7,
                 "2026-01-01 12:00:00", "2026-01-03 10:00:00"),
                ("-", "", "", 0, 0, "2026-01-01 13:00:00", None),   # 无名占位
            ],
        )
        conn.execute(
            "INSERT INTO encounters (blacklist_id, name_seen, match_score, "
            "source, screenshot_path, seen_at) VALUES (1,'PlayerZ',100,'chat',"
            "'a.jpg','2026-01-02 10:00:00')")
        conn.commit()
        conn.close()
        return path

    def test_legacy_db_is_upgraded(self):
        path = self._make_legacy_db()
        db = BlacklistDB(path)
        try:
            cols = {r[1] for r in db.conn.execute("PRAGMA table_info(blacklist)")}
            self.assertEqual(cols, {"id", "player_name", "note", "created_at"})
            names = sorted(r["player_name"] for r in db.get_all())
            # 名称栏为空的条目用 player_id 补名字；没有名字的占位行丢弃
            self.assertEqual(names, ["PlayerX", "PlayerZ", "？"])
            # 备注与添加时间保留
            by_name = {r["player_name"]: r for r in db.get_all()}
            self.assertEqual(by_name["PlayerZ"]["note"], "名字填在ID栏")
            self.assertEqual(by_name["PlayerZ"]["created_at"],
                             "2026-01-01 10:00:00")
            # 历史统计一并清空
            self.assertEqual(db.get_total_encounter_count(), 0)
            self.assertEqual(db.get_today_encounter_count(), 0)
            # 名单里的 ? 依然可索引可命中
            m = Matcher(db)
            self.assertEqual(len(m.check(["？"])), 1)
        finally:
            db.close()

    def test_migration_is_idempotent(self):
        path = self._make_legacy_db()
        for _ in range(2):
            db = BlacklistDB(path)
            try:
                self.assertEqual(db.get_count(), 3)
            finally:
                db.close()

    def test_migration_keeps_evidence_untouched(self):
        """证据截图是磁盘上的文件，迁移不碰它（只是不再有 DB 索引行）。"""
        path = self._make_legacy_db()
        db = BlacklistDB(path)
        try:
            self.assertEqual(db.get_recent_encounters(), [])
        finally:
            db.close()


# ==========================================================================
class TestMatcher(TempCase):
    def setUp(self):
        super().setUp()
        self.db = BlacklistDB(self.path("blacklist.db"))
        self.db.add("PlayerX", "老TK")
        self.db.add("John Doe", "空格名")
        self.db.add("阴影猎手", "中文名")
        self.matcher = Matcher(self.db)

    def tearDown(self):
        self.db.close()
        super().tearDown()

    # ---- match_text 验收 ----
    def test_match_text_joined_message(self):
        hits = self.matcher.match_text("PlayerX has joined the game")
        self.assertEqual(len(hits), 1)
        entry, score, sub = hits[0]
        self.assertEqual(entry["player_name"], "PlayerX")
        self.assertEqual(score, 100.0)
        self.assertTrue(sub)

    def test_match_text_name_with_space(self):
        hits = self.matcher.match_text("John Doe has joined the game")
        self.assertEqual([h[0]["player_name"] for h in hits], ["John Doe"])

    def test_match_text_chinese(self):
        hits = self.matcher.match_text("阴影猎手 回归战场")
        self.assertEqual([h[0]["player_name"] for h in hits], ["阴影猎手"])

    def test_match_text_no_duplicate_return(self):
        text = "PlayerX has joined the game. PlayerX has left the game."
        hits = self.matcher.match_text(text)
        self.assertEqual(len(hits), 1)

    def test_match_text_no_hit(self):
        self.assertEqual(self.matcher.match_text("SomeRandomGuy has joined"), [])
        self.assertEqual(self.matcher.match_text(""), [])
        self.assertEqual(self.matcher.match_text("   "), [])

    def test_exact_score_is_100(self):
        hits = self.matcher.match_text("xyzPlayerXxyz has joined")   # 需组合?
        # 整段包含 → 100
        self.assertTrue(any(h[0]["player_name"] == "PlayerX" and h[1] == 100.0
                            for h in hits))

    def test_fuzzy_threshold(self):
        hits = self.matcher.match_text("PlayerY has joined the game")
        # PlayerY 与 PlayerX ratio ≈ 85.7 → 应当返回且分数 >= 85
        self.assertTrue(hits)
        self.assertGreaterEqual(hits[0][1], 85.0)

    def test_fuzzy_below_threshold_not_returned(self):
        hits = self.matcher.match_text("Zzzzzzzz has joined")
        self.assertEqual([h for h in hits
                          if h[0]["player_name"] == "PlayerX"], [])

    # ---- check 验收 ----
    def test_check_list(self):
        hits = self.matcher.check(["PlayerX", "Unknown", "John Doe"])
        names = [h[0]["player_name"] for h in hits]
        self.assertEqual(names, ["PlayerX", "John Doe"])
        self.assertEqual(hits[0][1], 100.0)

    def test_check_dedup(self):
        hits = self.matcher.check(["PlayerX", "PlayerX", "playerx"])
        self.assertEqual(len(hits), 1)

    def test_check_skips_short_and_empty(self):
        self.assertEqual(self.matcher.check(["a", "", None, "-"]), [])
        self.assertEqual(self.matcher.check([]), [])

    def test_normalize(self):
        self.assertEqual(Matcher.normalize("  Player_X!! "), "playerx")
        self.assertEqual(Matcher.normalize("阴影 猎手"), "阴影猎手")
        self.assertEqual(Matcher.normalize(None), "")

    def test_check_joins_ocr_split_name(self):
        """OCR 把一个名字拆成两行时，拼接后仍应精确命中。"""
        self.db.add("SamplePlayer_01")
        self.matcher.reload()
        hits = self.matcher.check(["SamplePlayer", "01", "RandomGuy"])
        self.assertEqual([h[0]["player_name"] for h in hits],
                         ["SamplePlayer_01"])
        self.assertEqual(hits[0][1], 100.0)

    def test_check_joins_two_part_name(self):
        hits = self.matcher.check(["John", "Doe", "has"])
        self.assertEqual([h[0]["player_name"] for h in hits], ["John Doe"])
        self.assertEqual(hits[0][1], 100.0)

    def test_check_join_does_not_create_false_positive(self):
        hits = self.matcher.check(["Random", "Guy", "Here"])
        self.assertEqual([h for h in hits
                          if h[0]["player_name"] == "John Doe"], [])

    def test_exact_match_outranks_fuzzy_prefix(self):
        """较短的模糊候选不能抢走较长的精确候选。"""
        self.db.add("SamplePlayer_01")
        self.db.add("SamplePlayer_02")
        self.matcher.reload()
        hits = self.matcher.check(["SamplePlayer", "01"])
        self.assertEqual(hits[0][0]["player_name"], "SamplePlayer_01")
        self.assertEqual(hits[0][1], 100.0)

    def test_expand_keeps_singles_first(self):
        self.assertEqual(Matcher._expand(["A", "B"]), ["A", "B", "AB"])

    def test_reload_picks_up_new_entries(self):
        self.assertEqual(self.matcher.check(["NewGuy"]), [])
        self.db.add("NewGuy")
        self.matcher.reload()
        self.assertEqual(len(self.matcher.check(["NewGuy"])), 1)

    def test_deleted_entry_no_longer_matches(self):
        eid = next(r["id"] for r in self.db.get_all()
                   if r["player_name"] == "PlayerX")
        self.db.delete(eid)
        self.matcher.reload()
        self.assertEqual(self.matcher.check(["PlayerX"]), [])


# ==========================================================================
class TestSymbolNames(TempCase):
    """回归：玩家名整条就是一个符号（例如 `?`）时也要能命中。

    曾经的 bug：归一化会去掉所有非字母数字字符，`?` 于是变成空串，
    条目在 reload() 里被直接丢掉 —— 这类名字**永远**匹配不上，
    而 OCR 那侧还有第二道关卡（is_valid_player_name 认为它不含字母数字）
    会先把文本扔掉。
    """

    def setUp(self):
        super().setUp()
        self.db = BlacklistDB(self.path("b.db"))
        self.db.add("?")
        self.db.add("PlayerX")
        self.db.add("PlayerY")
        self.matcher = Matcher(self.db)

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def test_symbol_only_entry_is_indexed(self):
        self.assertIn("?", self.matcher.symbol_names())
        self.assertEqual(self.matcher.reload(), 3)

    def test_check_matches_symbol_name(self):
        hits = self.matcher.check(["?"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0]["player_name"], "?")
        self.assertEqual(hits[0][1], 100.0)

    def test_fullwidth_question_mark_matches_halfwidth_entry(self):
        """NFKC 折叠：OCR 给出全角 `？` 也要对上半角 `?`。"""
        self.assertEqual(len(self.matcher.check(["？"])), 1)
        self.assertEqual(len(self.matcher.match_text("？ : hello")), 1)
        self.assertEqual(len(self.matcher.match_text("? : hello")), 1)

    def test_symbol_name_matches_inside_a_line(self):
        """ESC 菜单里名字单独一行 / 聊天里 "? : 你好" 都要命中。"""
        self.assertEqual(len(self.matcher.match_text("?加入了游戏")), 1)
        self.assertEqual(len(self.matcher.match_text("为什么?")), 1)

    def test_longer_symbol_runs_do_not_false_positive(self):
        """聊天里打 "???" 不该命中名字为 "?" 的条目。"""
        self.assertEqual(self.matcher.match_text("???"), [])
        self.assertEqual(self.matcher.match_text("?!"), [])
        self.assertEqual(self.matcher.check(["??"]), [])

    def test_normal_names_still_match(self):
        self.assertEqual(self.matcher.check(["PlayerX"])[0][0]["player_name"],
                         "PlayerX")
        self.assertEqual(self.matcher.check(["playerx"])[0][0]["player_name"],
                         "PlayerX")

    def test_symbol_entry_coexists_with_normal_ones(self):
        hits = self.matcher.check(["PlayerX", "?", "PlayerY"])
        self.assertEqual({h[0]["player_name"] for h in hits},
                         {"PlayerX", "?", "PlayerY"})

    def test_symbol_key_helper(self):
        from app.core.matcher import fold, symbol_key
        self.assertEqual(symbol_key("?"), "?")
        self.assertEqual(symbol_key("？"), "?")
        self.assertEqual(symbol_key("★☆"), "★☆")
        self.assertEqual(symbol_key("PlayerX"), "")
        self.assertEqual(symbol_key(""), "")
        self.assertEqual(fold("ＰｌａｙｅｒＸ"), "playerx")


# ==========================================================================
class TestSymbolAndNormalNamesTogether(TempCase):
    """一行里同时出现普通名字与纯符号名字时，**两条都要报**。

    回归：用户实测「聊天里明明出现了 `？`，却只报了另一个名字」。除了索引问题，
    还要确认匹配本身不会"报了一个就收工"。
    """

    def setUp(self):
        super().setUp()
        self.db = BlacklistDB(self.path("b.db"))
        self.db.add("？")
        self.db.add("PlayerX")
        self.matcher = Matcher(self.db)

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def test_index_count_matches_entry_count(self):
        self.assertEqual(len(self.db.get_all()), 2)
        self.assertEqual(self.matcher.reload(), 2)

    def test_symbol_name_is_indexed(self):
        self.assertIn("?", self.matcher.symbol_names())
        self.assertIn("?", self.matcher.name_allowlist())

    def test_check_matches_symbol_name(self):
        hits = self.matcher.check(["?"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0]["player_name"], "？")
        self.assertEqual(hits[0][1], 100.0)

    def test_match_text_finds_both_symbol_and_normal_name(self):
        hits = self.matcher.match_text("PlayerX: asd PlayerX: ? PlayerY")
        self.assertEqual({h[0]["player_name"] for h in hits}, {"PlayerX", "？"})

    def test_fullwidth_and_halfwidth_both_hit(self):
        self.assertEqual(len(self.matcher.match_text("？ : 你好")), 1)
        self.assertEqual(len(self.matcher.match_text("? : hi")), 1)

    def test_entry_without_name_is_reported_not_silently_dropped(self):
        """没有名字的条目既不静默丢，也不假装索引成功。

        正常路径下 database.add() 会拦住这种行，但历史/手改过的库里可能有，
        所以索引时必须自己兜住并说出来。
        """
        self.db.conn.execute(
            "INSERT INTO blacklist (player_name) VALUES ('')")
        self.db.conn.commit()
        with self.assertLogs("matcher", level="WARNING") as captured:
            count = self.matcher.reload()
        self.assertEqual(count, 2)
        self.assertTrue(any("无法参与匹配" in line
                            for line in captured.output), captured.output)


# ==========================================================================
class TestConfusableNames(TempCase):
    """回归：OCR 把 0 认成 O、1 认成 l 时也要能命中（仅当解唯一）。"""

    def setUp(self):
        super().setUp()
        self.db = BlacklistDB(self.path("b.db"))
        self.db.add("Player_01")
        self.matcher = Matcher(self.db)

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def test_digit_letter_confusion_matches(self):
        for wrong in ("Player_Ol", "Player_0l", "Player_O1", "PlayerOI"):
            hits = self.matcher.check([wrong])
            self.assertEqual(len(hits), 1, f"{wrong} 应当命中 Player_01")
            self.assertEqual(hits[0][0]["player_name"], "Player_01")
            self.assertEqual(hits[0][1], 100.0, "应当由易混层直接命中，而不是模糊")

    def test_confusion_in_chat_text(self):
        hits = self.matcher.match_text("Player_Ol has joined the game")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], 100.0)

    def test_ambiguous_confusion_is_refused(self):
        """两个不同条目折叠到同一个键时，宁可不认也不能认错。"""
        self.db.add("Player_0l")          # 与 Player_01 折叠后同键
        self.matcher.reload()
        hits = self.matcher.check(["PlayerO1"])
        self.assertFalse([h for h in hits if h[1] >= 100.0],
                         "折叠键有歧义时必须放弃易混匹配")

    def test_plain_typo_still_uses_fuzzy(self):
        """易混层管不到的错误仍然由模糊层兜底。"""
        hits = self.matcher.check(["Player_01x"])
        self.assertEqual(len(hits), 1)
        self.assertLess(hits[0][1], 100.0)
        self.assertFalse(self.matcher.check(["TotallyDifferentName"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

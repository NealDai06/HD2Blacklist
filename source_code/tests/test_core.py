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

import config                                   # noqa: E402
from database import BlacklistDB                # noqa: E402
from matcher import Matcher                     # noqa: E402
from notification_config import NotificationConfig, ensure_notification_file  # noqa: E402
from region_config import RegionConfig          # noqa: E402

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
class TestDatabase(TempCase):
    def setUp(self):
        super().setUp()
        self.db = BlacklistDB(self.path("blacklist.db"))

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def test_add_get(self):
        eid = self.db.add("76561198000000001", "PlayerX", "恶意TK", 3)
        row = self.db.get(eid)
        self.assertEqual(row["player_id"], "76561198000000001")
        self.assertEqual(row["player_name"], "PlayerX")
        self.assertEqual(row["note"], "恶意TK")
        self.assertEqual(row["tk_count"], 3)
        self.assertEqual(row["encounter_count"], 0)
        self.assertIsNone(row["last_seen"])
        self.assertTrue(row["created_at"])

    def test_unique_constraint(self):
        self.db.add("id1", "Name1")
        with self.assertRaises(ValueError):
            self.db.add("id1", "Name1")

    def test_requires_id_or_name(self):
        with self.assertRaises(ValueError):
            self.db.add("", "")

    def test_name_only_entry(self):
        eid = self.db.add("", "只有名字")
        self.assertEqual(self.db.get(eid)["player_id"], "-")

    def test_update(self):
        eid = self.db.add("id1", "Name1", "n", 1)
        self.assertTrue(self.db.update(eid, note="改过了", tk_count=9))
        row = self.db.get(eid)
        self.assertEqual(row["note"], "改过了")
        self.assertEqual(row["tk_count"], 9)
        self.assertFalse(self.db.update(eid,))

    def test_delete_cascades_encounters(self):
        eid = self.db.add("id1", "Name1")
        self.db.record_encounter(eid, 100.0, "chat", "Name1", "")
        self.assertEqual(len(self.db.get_encounters(eid)), 1)
        self.assertTrue(self.db.delete(eid))
        self.assertIsNone(self.db.get(eid))
        self.assertEqual(self.db.get_total_encounter_count(), 0)

    def test_search(self):
        self.db.add("id1", "Alpha", "备注A")
        self.db.add("id2", "Beta", "备注B")
        self.db.add("id3", "gamma", "特殊")
        self.assertEqual(len(self.db.search("alph")), 1)
        self.assertEqual(len(self.db.search("备注")), 2)
        self.assertEqual(len(self.db.search("GAMMA")), 1)   # 大小写不敏感
        self.assertEqual(len(self.db.search("")), 3)

    def test_record_encounter_updates_atomically(self):
        eid = self.db.add("id1", "Name1")
        r1 = self.db.record_encounter(eid, 100.0, "chat", "Name1", "a.jpg")
        self.assertEqual(r1["encounter_count"], 1)
        self.assertIsNotNone(r1["last_seen"])
        r2 = self.db.record_encounter(eid, 92.5, "esc_menu", "Name12", "b.jpg")
        self.assertEqual(r2["encounter_count"], 2)
        rows = self.db.get_encounters(eid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name_seen"], "Name12")
        self.assertEqual(rows[0]["match_score"], 92.5)
        self.assertEqual(rows[0]["source"], "esc_menu")
        self.assertEqual(rows[0]["screenshot_path"], "b.jpg")
        # blacklist.last_seen 与 encounters 一致
        self.assertEqual(self.db.get(eid)["last_seen"], r2["last_seen"])

    def test_record_encounter_concurrent_no_lost_update(self):
        eid = self.db.add("id1", "Name1")
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
        self.assertEqual(self.db.get(eid)["encounter_count"], expected)
        self.assertEqual(len(self.db.get_encounters(eid, limit=1000)), expected)

    def test_record_encounter_missing_entry_rolls_back(self):
        with self.assertRaises(ValueError):
            self.db.record_encounter(99999, 100.0, "chat", "x", "")
        self.assertEqual(self.db.get_total_encounter_count(), 0)

    def test_reset_helpers(self):
        eid = self.db.add("id1", "Name1")
        other = self.db.add("id2", "Name2")
        self.db.record_encounter(eid, 100.0, "chat", "Name1", "")
        self.db.record_encounter(other, 100.0, "chat", "Name2", "")
        self.db.reset_encounter_count(eid)
        self.db.reset_last_seen(eid)
        self.assertEqual(self.db.get(eid)["encounter_count"], 0)
        self.assertIsNone(self.db.get(eid)["last_seen"])
        # 只影响当前条目
        self.assertEqual(self.db.get(other)["encounter_count"], 1)
        self.assertIsNotNone(self.db.get(other)["last_seen"])

    def test_today_count(self):
        eid = self.db.add("id1", "Name1")
        self.assertEqual(self.db.get_today_encounter_count(), 0)
        self.db.record_encounter(eid, 100.0, "chat", "Name1", "")
        self.db.record_encounter(eid, 100.0, "chat", "Name1", "")
        self.assertEqual(self.db.get_today_encounter_count(), 2)

    def test_ordering_never_seen_last(self):
        a = self.db.add("id1", "A")
        b = self.db.add("id2", "B")
        self.db.record_encounter(b, 100.0, "chat", "B", "")
        rows = self.db.get_all()
        self.assertEqual(rows[0]["id"], b)
        self.assertEqual(rows[-1]["id"], a)

    def test_sort_whitelist_blocks_injection(self):
        self.db.add("id1", "A")
        rows = self.db.get_all(order_by="id; DROP TABLE blacklist;--")
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.db.get_count(), 1)

    def test_persistence_across_reopen(self):
        eid = self.db.add("id1", "Persist")
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
        eid = self.db.add("id1", "Name1", "备注", 2, "ev.jpg")
        self.db.record_encounter(eid, 100.0, "chat", "Name1", "a.jpg")
        rows = self.db.export_all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        for field in ("player_id", "player_name", "note", "tk_count",
                      "encounter_count", "created_at", "last_seen"):
            self.assertIn(field, row)
        self.assertEqual(row["player_id"], "id1")
        self.assertEqual(row["player_name"], "Name1")
        self.assertEqual(row["note"], "备注")
        self.assertEqual(row["tk_count"], 2)
        self.assertEqual(row["encounter_count"], 1)
        self.assertIsNotNone(row["last_seen"])

    def test_export_all_ordering_is_stable(self):
        self.db.add("id1", "A")
        self.db.add("id2", "B")
        self.db.add("id3", "C")
        self.assertEqual([r["player_name"] for r in self.db.export_all()],
                         ["A", "B", "C"])

    def test_import_skip_strategy(self):
        self.db.add("id1", "Name1", "原始备注", 1)
        res = self.db.import_entries(
            [{"player_id": "id1", "player_name": "Name1", "note": "新备注",
              "tk_count": 9}], strategy="skip")
        self.assertEqual(res, {"inserted": 0, "skipped": 1, "updated": 0})
        self.assertEqual(self.db.get_all()[0]["note"], "原始备注")

    def test_import_update_note_keeps_counters(self):
        eid = self.db.add("id1", "Name1", "原始备注", 1)
        self.db.record_encounter(eid, 100.0, "chat", "Name1", "")
        last_seen = self.db.get(eid)["last_seen"]

        res = self.db.import_entries(
            [{"player_id": "id1", "player_name": "Name1", "note": "新备注",
              "tk_count": 7, "encounter_count": 999,
              "last_seen": "2000-01-01 00:00:00"}],
            strategy="update_note")
        self.assertEqual(res, {"inserted": 0, "skipped": 0, "updated": 1})
        row = self.db.get(eid)
        self.assertEqual(row["note"], "新备注")
        self.assertEqual(row["tk_count"], 7)
        self.assertEqual(row["encounter_count"], 1)      # 保留
        self.assertEqual(row["last_seen"], last_seen)    # 保留

    def test_import_overwrite_strategy(self):
        eid = self.db.add("id1", "Name1", "原始", 1)
        self.db.record_encounter(eid, 100.0, "chat", "Name1", "")
        res = self.db.import_entries(
            [{"player_id": "id1", "player_name": "Name1", "note": "覆盖",
              "tk_count": 5, "encounter_count": 42,
              "last_seen": "2000-01-01 00:00:00"}],
            strategy="overwrite")
        self.assertEqual(res["updated"], 1)
        row = self.db.get(eid)
        self.assertEqual(row["note"], "覆盖")
        self.assertEqual(row["tk_count"], 5)
        self.assertEqual(row["encounter_count"], 42)
        self.assertEqual(row["last_seen"], "2000-01-01 00:00:00")

    def test_import_inserts_new_entries(self):
        res = self.db.import_entries(
            [{"player_id": "new1", "player_name": "NewOne", "note": "n",
              "tk_count": 3, "encounter_count": 2,
              "created_at": "2020-01-01 00:00:00",
              "last_seen": "2020-02-02 00:00:00"}],
            strategy="skip")
        self.assertEqual(res, {"inserted": 1, "skipped": 0, "updated": 0})
        row = self.db.get_all()[0]
        self.assertEqual(row["player_name"], "NewOne")
        self.assertEqual(row["tk_count"], 3)
        self.assertEqual(row["encounter_count"], 2)
        self.assertEqual(row["created_at"], "2020-01-01 00:00:00")
        self.assertEqual(row["last_seen"], "2020-02-02 00:00:00")

    def test_import_skips_empty_player_id(self):
        res = self.db.import_entries(
            [{"player_id": "", "player_name": "NoId"},
             {"player_name": "AlsoNoId"},
             {"player_id": "  ", "player_name": "Blank"}],
            strategy="skip")
        self.assertEqual(res, {"inserted": 0, "skipped": 3, "updated": 0})
        self.assertEqual(self.db.get_count(), 0)

    def test_import_distinguishes_same_id_different_name(self):
        self.db.add("id1", "NameA")
        res = self.db.import_entries(
            [{"player_id": "id1", "player_name": "NameB"}], strategy="skip")
        self.assertEqual(res["inserted"], 1)          # 名字不同 → 新条目
        self.assertEqual(self.db.get_count(), 2)

    def test_import_handles_bad_numbers(self):
        res = self.db.import_entries(
            [{"player_id": "x", "player_name": "N", "tk_count": "abc",
              "encounter_count": None}], strategy="skip")
        self.assertEqual(res["inserted"], 1)
        row = self.db.get_all()[0]
        self.assertEqual(row["tk_count"], 0)
        self.assertEqual(row["encounter_count"], 0)

    def test_import_ignores_non_dict_rows(self):
        res = self.db.import_entries(["junk", 42, None], strategy="skip")
        self.assertEqual(res["skipped"], 3)

    def test_import_rejects_unknown_strategy(self):
        with self.assertRaises(ValueError):
            self.db.import_entries([{"player_id": "x"}], strategy="nope")

    def test_import_rolls_back_on_error(self):
        """整批导入中任一条出错 → 全部回滚，数据库保持原状。"""
        self.db.add("keep", "KeepMe")
        before = self.db.get_count()

        class Boom:
            """先给出一条合法数据，再抛异常 —— 用来验证整批回滚。"""

            def __iter__(self):
                yield {"player_id": "ok1", "player_name": "WillRollback"}
                raise RuntimeError("模拟导入中断")

        with self.assertRaises(RuntimeError):
            self.db.import_entries(Boom(), strategy="skip")

        self.assertEqual(self.db.get_count(), before)   # 没有半途写入
        self.assertEqual([r["player_name"] for r in self.db.get_all()],
                         ["KeepMe"])

    def test_export_import_roundtrip(self):
        eid = self.db.add("id1", "Name1", "备注", 2)
        self.db.record_encounter(eid, 95.0, "chat", "Name1", "")
        exported = self.db.export_all()

        other = BlacklistDB(self.path("other.db"))
        try:
            res = other.import_entries(exported, strategy="skip")
            self.assertEqual(res["inserted"], 1)
            row = other.get_all()[0]
            self.assertEqual(row["player_id"], "id1")
            self.assertEqual(row["player_name"], "Name1")
            self.assertEqual(row["note"], "备注")
            self.assertEqual(row["tk_count"], 2)
            self.assertEqual(row["encounter_count"], 1)
            # 与源库完全一致（时间字段也一致）
            self.assertEqual(row["created_at"], exported[0]["created_at"])
            self.assertEqual(row["last_seen"], exported[0]["last_seen"])
        finally:
            other.close()

    def test_import_empty_list_is_noop(self):
        self.assertEqual(self.db.import_entries([]),
                         {"inserted": 0, "skipped": 0, "updated": 0})
        self.assertEqual(self.db.import_entries(None),
                         {"inserted": 0, "skipped": 0, "updated": 0})


# ==========================================================================
class TestMatcher(TempCase):
    def setUp(self):
        super().setUp()
        self.db = BlacklistDB(self.path("blacklist.db"))
        self.db.add("id1", "PlayerX", "老TK", 2)
        self.db.add("id2", "John Doe", "空格名", 1)
        self.db.add("id3", "阴影猎手", "中文名", 0)
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
        self.db.add("id7", "SamplePlayer_01")
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
        self.db.add("id7", "SamplePlayer_01")
        self.db.add("id8", "SamplePlayer_02")
        self.matcher.reload()
        hits = self.matcher.check(["SamplePlayer", "01"])
        self.assertEqual(hits[0][0]["player_name"], "SamplePlayer_01")
        self.assertEqual(hits[0][1], 100.0)

    def test_expand_keeps_singles_first(self):
        self.assertEqual(Matcher._expand(["A", "B"]), ["A", "B", "AB"])

    def test_reload_picks_up_new_entries(self):
        self.assertEqual(self.matcher.check(["NewGuy"]), [])
        self.db.add("id9", "NewGuy")
        self.matcher.reload()
        self.assertEqual(len(self.matcher.check(["NewGuy"])), 1)

    def test_deleted_entry_no_longer_matches(self):
        eid = next(r["id"] for r in self.db.get_all()
                   if r["player_name"] == "PlayerX")
        self.db.delete(eid)
        self.matcher.reload()
        self.assertEqual(self.matcher.check(["PlayerX"]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

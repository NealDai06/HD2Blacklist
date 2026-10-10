# -*- coding: utf-8 -*-
"""阶段 1-3 验收测试：config / 通知配置 / 数据库 / 匹配器 / 插件日志解析。

运行： python -m unittest discover -s tests -t tests
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
from app.core.database import (BlacklistDB, PREV_NAME_LIMIT,   # noqa: E402
                               dump_prev_names, format_prev_names,
                               normalize_peer_id, parse_prev_names,
                               push_prev_name)
from app.core.matcher import Matcher                     # noqa: E402
from app.settings.notification_config import NotificationConfig, ensure_notification_file  # noqa: E402

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
        """已删除的旧常量不应存在（含本轮 v2 改造删掉的抓屏/OCR/热键常量）。"""
        for name in ("FALLBACK_INTERVAL", "FALLBACK_SESSION", "HOTKEY_VK",
                     "HOTKEY_SESSION", "CHAT_SESSION",
                     "CHAT_POLL_INTERVAL", "CHAT_FULL_SCAN_INTERVAL",
                     "CHAT_HASH_SIZE", "CHAT_TEXT_HASH_CACHE_SIZE",
                     "NOTIFY_THROTTLE_SECONDS",
                     "DEFAULT_KEYWORDS", "KEYWORDS_PATH",
                     # v2：抓屏 + OCR + 区域 + 热键 + 进程监控 全部下线
                     "DEFAULT_REGIONS", "REGION_META", "REGION_MIN_SIZE",
                     "HOTKEY_PATH", "DEFAULT_HOTKEY",
                     "CHAT_SCAN_REGION_KEY", "CHAT_SCAN_SOURCE",
                     "CHAT_SCAN_HOTKEY_SOURCE", "CHAT_SCAN_HOTKEY_ENABLED",
                     "CHAT_SCAN_HOTKEY_VK", "SCAN_SOURCE_LABELS",
                     "ESC_POLL_INTERVAL", "ESC_SESSION", "ESC_MENU_MEAN_MAX",
                     "COLD_START_DELAY", "COLD_START_SESSION",
                     "SESSION_SKIP_UNCHANGED", "SESSION_MAX_STATIC_FRAMES",
                     "OCR_UPSCALE", "OCR_CONFIDENCE_MIN",
                     "EVIDENCE_DIR", "EVIDENCE_JPEG_FILES",
                     "EVIDENCE_MAX_FILES", "SCAN_THREAD_PRIORITY",
                     "TARGET_PROCESS_NAMES"):
            self.assertFalse(hasattr(config, name), name)

    def test_watch_constants(self):
        """v2 的数据来源常量必须齐全。"""
        self.assertEqual(config.HIT_DEDUP_WINDOW, 30)
        self.assertGreaterEqual(config.WATCH_POLL_INTERVAL, 0.1)
        self.assertGreaterEqual(config.WATCH_TAIL_MAX_LINES, 100)
        self.assertEqual(config.WATCH_THREAD_PRIORITY, "lowest")
        self.assertIn("join", config.ALERT_EVENTS)
        self.assertNotIn("squad", config.ALERT_EVENTS)   # 快照绝不触发告警
        self.assertEqual(config.PLAYER_LOG_NAME, "playerLog.txt")
        self.assertTrue(config.player_log_path().endswith("playerLog.txt"))
        self.assertTrue(os.path.isabs(config.player_log_path()))
        # 保留项
        for name in ("MATCH_THRESHOLD", "MATCH_SYMBOL_NAMES",
                     "MATCH_FUZZY_CONFUSABLE", "MAX_NOTIFY_STACK",
                     "GAME_FRIENDLY_PRIORITY", "PROCESS_PRIORITY"):
            self.assertTrue(hasattr(config, name), name)

    def test_alert_source_label(self):
        self.assertIn("PeerID", config.alert_source_label("peer_join"))
        self.assertEqual(config.alert_source_label(""), "")
        self.assertEqual(config.alert_source_label("unknown", "兜底"), "兜底")


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
        old = (config.DATA_DIR, config.ASSETS_DIR,
               config.LOG_DIR, config.DIRECTORIES)
        config.DATA_DIR = data
        config.ASSETS_DIR = os.path.join(data, "assets")
        config.LOG_DIR = os.path.join(data, "logs")
        config.DIRECTORIES = (config.DATA_DIR, config.ASSETS_DIR,
                              config.LOG_DIR)
        try:
            config.ensure_dirs()
        finally:
            (config.DATA_DIR, config.ASSETS_DIR,
             config.LOG_DIR, config.DIRECTORIES) = old
        for sub in ("", "assets", "logs"):
            self.assertTrue(os.path.isdir(os.path.join(data, sub)), sub)


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
        """TK次数 / 遇到次数 / 最后遇见 必须彻底消失（peer_id 是 v2 新增的系统列）。"""
        row = self.db.get(self.db.add("PlayerX"))
        for gone in ("player_id", "tk_count", "encounter_count", "last_seen"):
            self.assertNotIn(gone, row)
        cols = {r[1] for r in
                self.db.conn.execute("PRAGMA table_info(blacklist)")}
        self.assertEqual(cols, {"id", "player_name", "note", "peer_id",
                                "prev_names", "created_at"})

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

    def test_peer_id_is_stored_and_normalized(self):
        eid = self.db.add("Name1", peer_id="aabbccdd00112233")
        self.assertEqual(self.db.get(eid)["peer_id"], "AABBCCDD00112233")

    def test_peer_id_is_optional(self):
        """没有 PeerID 的条目：只能按名字识别，而且不会告警。"""
        eid = self.db.add("Legacy")
        self.assertEqual(self.db.get(eid)["peer_id"], "")

    # ------------------------------------------------------ 身份 = PeerID
    def test_same_peer_id_updates_name_instead_of_adding(self):
        """同一个人改了名字 → 还是那一条，只更新名字。"""
        eid = self.db.add("老王", peer_id="AABBCCDD00112233")
        again = self.db.add("老王改名了", peer_id="aabbccdd00112233")
        self.assertEqual(again, eid)                    # 同一条
        self.assertEqual(self.db.get_count(), 1)
        self.assertEqual(self.db.get(eid)["player_name"], "老王改名了")

    def test_rename_keeps_existing_note(self):
        """改名时备注留空 = 别动我原来写的备注。"""
        eid = self.db.add("老王", "故意TK", peer_id="AABBCCDD00112233")
        self.db.add("老王改名了", "", peer_id="AABBCCDD00112233")
        self.assertEqual(self.db.get(eid)["note"], "故意TK")
        # 但真的填了备注就按填的来
        self.db.add("老王又改名", "这次是别的", peer_id="AABBCCDD00112233")
        self.assertEqual(self.db.get(eid)["note"], "这次是别的")

    def test_same_name_different_peer_id_keeps_both(self):
        """同名、不同 PeerID = 两个人 → 各留一条。"""
        a = self.db.add("PlayerX", peer_id="AABBCCDD00112233")
        b = self.db.add("PlayerX", peer_id="1111222233334444")
        self.assertNotEqual(a, b)
        self.assertEqual(self.db.get_count(), 2)
        self.assertEqual(sorted(e["peer_id"] for e in self.db.get_all()),
                         ["1111222233334444", "AABBCCDD00112233"])

    def test_named_entry_coexists_with_same_name_peer_entry(self):
        """没 ID 的「老王」和有 ID 的「老王」也算两个人（ID 不同）。"""
        self.db.add("老王")
        self.db.add("老王", peer_id="AABBCCDD00112233")
        self.assertEqual(self.db.get_count(), 2)

    def test_two_entries_without_peer_id_must_differ_by_name(self):
        """两条都没 ID 时，名字就是身份 —— 同名必须挡住，否则分不清谁是谁。"""
        self.db.add("老王")
        with self.assertRaises(ValueError):
            self.db.add("老王")
        with self.assertRaises(ValueError):
            self.db.add("老王", peer_id="")             # 空串等于没有
        self.assertEqual(self.db.get_count(), 1)

    def test_unique_index_allows_duplicate_names(self):
        """约束本身也是这么定的：唯一索引只看 PeerID（无 ID 的才看名字）。"""
        self.db.add("Homer", peer_id="AABBCCDD00112233")
        self.db.add("Homer", peer_id="1111222233334444")     # 不该被索引拦住
        self.assertEqual(self.db.get_count(), 2)

    def test_set_peer_id_fills_and_clears(self):
        eid = self.db.add("A")
        self.assertTrue(self.db.set_peer_id(eid, "AABBCCDD00112233"))
        self.assertEqual(self.db.get(eid)["peer_id"], "AABBCCDD00112233")
        self.assertTrue(self.db.set_peer_id(eid, ""))
        self.assertEqual(self.db.get(eid)["peer_id"], "")

    # ------------------------------------------------------ 名字自动同步
    def test_rename_by_peer_id(self):
        eid = self.db.add("老名字", "备注留着", peer_id="AABBCCDD00112233")
        self.assertEqual(self.db.rename_by_peer_id("aabbccdd00112233", "新名字"),
                         "老名字")                     # 返回旧名字
        row = self.db.get(eid)
        self.assertEqual(row["player_name"], "新名字")
        self.assertEqual(row["note"], "备注留着")        # 别的字段一个不动
        self.assertEqual(row["peer_id"], "AABBCCDD00112233")

    def test_rename_by_peer_id_no_change_returns_empty(self):
        self.db.add("同名", peer_id="AABBCCDD00112233")
        self.assertEqual(self.db.rename_by_peer_id("AABBCCDD00112233", "同名"), "")
        self.assertEqual(self.db.rename_by_peer_id("AABBCCDD00112233", "  同名 "),
                         "")

    def test_rename_by_peer_id_ignores_unknown_and_empty(self):
        self.db.add("在名单里的", peer_id="AABBCCDD00112233")
        self.assertEqual(self.db.rename_by_peer_id("1111222233334444", "谁"), "")
        self.assertEqual(self.db.rename_by_peer_id("", "谁"), "")
        self.assertEqual(self.db.rename_by_peer_id("AABBCCDD00112233", ""), "")
        self.assertEqual(self.db.rename_by_peer_id("AABBCCDD00112233", "   "), "")
        self.assertEqual(self.db.get_all()[0]["player_name"], "在名单里的")

    def test_rename_does_not_touch_same_name_other_id(self):
        a = self.db.add("同名", peer_id="AABBCCDD00112233")
        b = self.db.add("同名", peer_id="1111222233334444")
        self.db.rename_by_peer_id("AABBCCDD00112233", "改了")
        self.assertEqual(self.db.get(a)["player_name"], "改了")
        self.assertEqual(self.db.get(b)["player_name"], "同名")   # 另一个不动

    def test_rename_never_adds_a_row(self):
        self.db.add("老名字", peer_id="AABBCCDD00112233")
        self.db.rename_by_peer_id("AABBCCDD00112233", "新名字")
        self.assertEqual(self.db.get_count(), 1)

    # ------------------------------------------------------ 曾用名
    def test_rename_records_prev_name(self):
        eid = self.db.add("老王", peer_id="AABBCCDD00112233")
        self.db.rename_by_peer_id("AABBCCDD00112233", "老王改名了")
        row = self.db.get(eid)
        self.assertEqual(row["player_name"], "老王改名了")
        self.assertEqual(format_prev_names(row["prev_names"]), "老王")
        self.db.rename_by_peer_id("AABBCCDD00112233", "又改了")
        self.assertEqual(format_prev_names(self.db.get(eid)["prev_names"]),
                         "老王 / 老王改名了")

    def test_manual_rename_records_prev_name(self):
        eid = self.db.add("老王")
        self.db.update(eid, player_name="老王二号")
        self.assertEqual(format_prev_names(self.db.get(eid)["prev_names"]),
                         "老王")

    def test_explicit_prev_names_wins_over_auto(self):
        """编辑框里改过的曾用名以用户为准（清空就是清空）。"""
        eid = self.db.add("老王")
        self.db.update(eid, player_name="老王二号", prev_names="只留这个")
        row = self.db.get(eid)
        self.assertEqual(format_prev_names(row["prev_names"]), "只留这个")
        self.db.update(eid, player_name="老王三号", prev_names="")
        self.assertEqual(self.db.get(eid)["prev_names"], "")

    def test_rename_without_name_change_keeps_prev(self):
        eid = self.db.add("老王", peer_id="AABBCCDD00112233")
        self.db.rename_by_peer_id("AABBCCDD00112233", "老王")
        self.assertEqual(self.db.get(eid)["prev_names"], "")

    def test_add_same_peer_id_records_prev_name(self):
        eid = self.db.add("老王", peer_id="AABBCCDD00112233")
        again = self.db.add("新名字", peer_id="aabbccdd00112233")
        self.assertEqual(again, eid)
        self.assertEqual(format_prev_names(self.db.get(eid)["prev_names"]),
                         "老王")

    def test_add_with_empty_prev_names_does_not_clear_history(self):
        """加入名单时不给曾用名 = "没指定"，不能把已有的历史抹掉。"""
        eid = self.db.add("老王", peer_id="AABBCCDD00112233")
        self.db.rename_by_peer_id("AABBCCDD00112233", "老二")
        self.db.add("老三", peer_id="AABBCCDD00112233", prev_names="")
        self.assertEqual(format_prev_names(self.db.get(eid)["prev_names"]),
                         "老王 / 老二")

    def test_import_merges_prev_names(self):
        eid = self.db.add("老王", peer_id="AABBCCDD00112233")
        self.db.import_entries(
            [{"player_name": "老王新名", "peer_id": "AABBCCDD00112233",
              "prev_names": '["文件里的旧名"]'}], strategy="skip")
        got = format_prev_names(self.db.get(eid)["prev_names"])
        self.assertIn("老王", got)
        self.assertIn("文件里的旧名", got)

    def test_export_carries_prev_names(self):
        eid = self.db.add("老王", peer_id="AABBCCDD00112233")
        self.db.rename_by_peer_id("AABBCCDD00112233", "新名")
        self.assertEqual(format_prev_names(self.db.export_all()[0]["prev_names"]),
                         "老王")
        self.assertEqual(self.db.get(eid)["player_name"], "新名")

    def test_set_peer_id_refuses_taken_id(self):
        self.db.add("A", peer_id="AABBCCDD00112233")
        eid_b = self.db.add("B")
        with self.assertRaises(ValueError):
            self.db.set_peer_id(eid_b, "aabbccdd00112233")

    def test_find_by_peer_id(self):
        eid = self.db.add("A", peer_id="AABBCCDD00112233")
        self.assertEqual(self.db.find_by_peer_id("aabbccdd00112233")["id"], eid)
        self.assertIsNone(self.db.find_by_peer_id(""))
        self.assertIsNone(self.db.find_by_peer_id("0000000000000000"))

    def test_blacklist_peers_map(self):
        self.db.add("A", peer_id="AABBCCDD00112233")
        self.db.add("B")
        self.assertEqual(list(self.db.blacklist_peers()), ["AABBCCDD00112233"])

    def test_search(self):
        self.db.add("Alpha", "备注A")
        self.db.add("Beta", "备注B")
        self.db.add("gamma", "特殊")
        self.assertEqual(len(self.db.search("alph")), 1)
        self.assertEqual(len(self.db.search("备注")), 2)
        self.assertEqual(len(self.db.search("GAMMA")), 1)   # 大小写不敏感
        self.assertEqual(len(self.db.search("")), 3)

    def test_delete_keeps_seen_players(self):
        """删名单条目**不该**把"遇到过"这个事实一起删掉。"""
        eid = self.db.add("A", peer_id="AABBCCDD00112233")
        self.db.record_seen("AABBCCDD00112233", "A", game_pid=1)
        self.assertTrue(self.db.delete(eid))
        self.assertIsNone(self.db.get(eid))
        self.assertEqual(self.db.count_seen(), 1)

    def test_record_seen_upsert_and_game_counting(self):
        self.db.record_seen("1111222233334444", "甲", game_pid=100)
        self.assertEqual(self.db.count_seen(), 1)
        # 同一局里重复出现：次数不涨（不是"写了几行日志"）
        self.db.record_seen("1111222233334444", "甲", game_pid=100)
        self.assertEqual(self.db.get_seen("1111222233334444")["seen_count"], 1)
        # 换一局：涨一次
        self.db.record_seen("1111222233334444", "甲改名", game_pid=200)
        row = self.db.get_seen("1111222233334444")
        self.assertEqual(row["seen_count"], 2)
        self.assertEqual(row["name_first"], "甲")       # 第一次的名字留着
        self.assertEqual(row["name_last"], "甲改名")
        self.assertEqual(row["last_game_pid"], 200)

    def test_record_seen_fills_name_late(self):
        """名字比 peer 晚几秒才从名册读到：先存空名，后来补上。"""
        self.db.record_seen("1111222233334444", "", game_pid=1)
        self.assertEqual(self.db.get_seen("1111222233334444")["name_first"], "")
        self.db.record_seen("1111222233334444", "补上的名字", game_pid=1)
        row = self.db.get_seen("1111222233334444")
        self.assertEqual(row["name_first"], "补上的名字")
        self.assertEqual(row["name_last"], "补上的名字")

    def test_record_seen_ignores_bad_peer_id(self):
        self.db.record_seen("", "无id")
        self.db.record_seen("zzz", "坏id")
        self.db.record_seen(None, "None")
        self.assertEqual(self.db.count_seen(), 0)

    def test_recent_seen_ordering_and_limit(self):
        from datetime import datetime, timedelta
        fmt = "%Y-%m-%d %H:%M:%S"
        older = (datetime.now() - timedelta(days=3)).strftime(fmt)
        newer = (datetime.now() - timedelta(days=1)).strftime(fmt)
        self.db.record_seen("1111222233334444", "先", game_pid=1, seen_at=older)
        self.db.record_seen("2222333344445555", "后", game_pid=1, seen_at=newer)
        rows = self.db.get_recent_seen()
        self.assertEqual([r["name_last"] for r in rows], ["后", "先"])
        self.assertEqual(len(self.db.get_recent_seen(limit=1)), 1)
        # days=0 = 不限时间
        self.assertEqual(len(self.db.get_recent_seen(days=0)), 2)

    def test_recent_seen_days_filter(self):
        self.db.record_seen("1111222233334444", "很旧", game_pid=1,
                            seen_at="2020-01-01 00:00:00")
        self.assertEqual(self.db.count_seen(days=7), 0)
        self.assertEqual(self.db.count_seen(), 1)

    def test_forget_and_clear_seen(self):
        self.db.record_seen("1111222233334444", "甲", game_pid=1)
        self.db.record_seen("2222333344445555", "乙", game_pid=1)
        self.assertTrue(self.db.forget_seen("1111222233334444"))
        self.assertFalse(self.db.forget_seen("1111222233334444"))
        self.assertEqual(self.db.count_seen(), 1)
        self.db.clear_seen()
        self.assertEqual(self.db.count_seen(), 0)

    def test_clear_all_clears_both_tables(self):
        self.db.add("A", peer_id="AABBCCDD00112233")
        self.db.record_seen("1111222233334444", "甲", game_pid=1)
        self.db.clear_all()
        self.assertEqual(self.db.get_count(), 0)
        self.assertEqual(self.db.count_seen(), 0)

    def test_clear_blacklist_keeps_seen(self):
        self.db.add("A")
        self.db.record_seen("1111222233334444", "甲", game_pid=1)
        self.db.clear_blacklist()
        self.assertEqual(self.db.get_count(), 0)
        self.assertEqual(self.db.count_seen(), 1)

    # ------------------------------------------------------ 忽略名单
    def test_ignore_removes_from_seen(self):
        """忽略的语义：以后不提醒，也不再进「最近遇到」。"""
        self.db.record_seen("AABBCCDD00112233", "甲", game_pid=1)
        self.assertTrue(self.db.ignore_peer("aabbccdd00112233", "甲"))
        self.assertTrue(self.db.is_ignored("AABBCCDD00112233"))
        self.assertEqual(self.db.count_seen(), 0)        # 已从列表里清掉

    def test_is_ignored_rejects_garbage(self):
        self.db.ignore_peer("AABBCCDD00112233")
        self.assertFalse(self.db.is_ignored(""))
        self.assertFalse(self.db.is_ignored(None))
        self.assertFalse(self.db.is_ignored("zzz"))
        self.assertFalse(self.db.is_ignored("0000000000000000"))

    def test_ignore_peer_without_seen_row(self):
        """从别处得知的 id（还没在游戏里遇到）也要能先拉黑。"""
        self.db.ignore_peer("AABBCCDD00112233", "还没遇到的人")
        self.assertEqual(len(self.db.get_ignored()), 1)
        self.assertEqual(self.db.get_ignored()[0]["name"], "还没遇到的人")

    def test_ignore_is_idempotent(self):
        self.db.ignore_peer("AABBCCDD00112233", "甲")
        self.db.ignore_peer("aabbccdd00112233", "甲改名了")
        self.assertEqual(len(self.db.get_ignored()), 1)
        self.assertEqual(self.db.get_ignored()[0]["name"], "甲改名了")

    def test_unignore(self):
        self.db.ignore_peer("AABBCCDD00112233")
        self.assertTrue(self.db.unignore_peer("AABBCCDD00112233"))
        self.assertFalse(self.db.unignore_peer("AABBCCDD00112233"))
        self.assertFalse(self.db.is_ignored("AABBCCDD00112233"))
        # 取消忽略不会凭空补一条"遇到过"
        self.assertEqual(self.db.count_seen(), 0)

    def test_clear_ignored(self):
        self.db.ignore_peer("AABBCCDD00112233")
        self.db.ignore_peer("1111222233334444")
        self.db.clear_ignored()
        self.assertEqual(self.db.ignored_peers(), set())

    def test_ignored_peers_set(self):
        self.db.ignore_peer("aabbccdd00112233")
        self.db.ignore_peer("1111222233334444")
        self.assertEqual(self.db.ignored_peers(),
                         {"AABBCCDD00112233", "1111222233334444"})

    def test_clear_all_keeps_ignored(self):
        """「清空全部」不动忽略名单 —— 那是你主动做的选择，不是数据。"""
        self.db.add("A")
        self.db.record_seen("1111222233334444", "甲", game_pid=1)
        self.db.ignore_peer("AABBCCDD00112233")
        self.db.clear_all()
        self.assertEqual(self.db.get_count(), 0)
        self.assertEqual(self.db.count_seen(), 0)
        self.assertTrue(self.db.is_ignored("AABBCCDD00112233"))

    def test_get_ignored_has_metadata(self):
        self.db.ignore_peer("AABBCCDD00112233", "甲")
        row = self.db.get_ignored()[0]
        self.assertEqual(set(row), {"peer_id", "name", "added_at"})
        self.assertTrue(row["added_at"])

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
        self.db.add("Name1", "备注", peer_id="AABBCCDD00112233")
        rows = self.db.export_all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row),
                         {"player_name", "note", "peer_id", "prev_names",
                          "created_at"})
        self.assertEqual(row["player_name"], "Name1")
        self.assertEqual(row["note"], "备注")
        self.assertEqual(row["peer_id"], "AABBCCDD00112233")

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
        self.assertEqual(res, {"inserted": 0, "updated": 0, "skipped": 1, "renamed": 0})
        self.assertEqual(self.db.get_all()[0]["note"], "原始备注")

    def test_import_update_note_keeps_created_at(self):
        eid = self.db.add("Name1", "原始备注")
        created = self.db.get(eid)["created_at"]

        res = self.db.import_entries(
            [{"player_name": "Name1", "note": "新备注",
              "created_at": "2000-01-01 00:00:00"}],
            strategy="update_note")
        self.assertEqual(res, {"inserted": 0, "updated": 1, "skipped": 0, "renamed": 0})
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
        self.assertEqual(res, {"inserted": 1, "updated": 0, "skipped": 0, "renamed": 0})
        row = self.db.get_all()[0]
        self.assertEqual(row["player_name"], "NewOne")
        self.assertEqual(row["created_at"], "2020-01-01 00:00:00")
        self.assertEqual(row["peer_id"], "")

    def test_import_carries_peer_id(self):
        """换台机器导回来，PeerID 精确匹配照样有效。"""
        res = self.db.import_entries(
            [{"player_name": "N", "note": "", "peer_id": "aabbccdd00112233"}],
            strategy="skip")
        self.assertEqual(res["inserted"], 1)
        self.assertEqual(self.db.get_all()[0]["peer_id"], "AABBCCDD00112233")

    def test_import_same_name_different_peer_id_keeps_both(self):
        """名字相同、PeerID 不同 → 依旧记录（各留一条）。"""
        self.db.add("Homer", peer_id="AABBCCDD00112233")
        res = self.db.import_entries(
            [{"player_name": "Homer", "note": "另一个 Homer",
              "peer_id": "1111222233334444"}], strategy="skip")
        self.assertEqual(res, {"inserted": 1, "updated": 0, "skipped": 0,
                               "renamed": 0})
        self.assertEqual(self.db.get_count(), 2)

    def test_import_same_peer_id_updates_name(self):
        """PeerID 相同、名字不同 → 是同一个人改了名 → 更新名字。"""
        eid = self.db.add("老王", "备注留着", peer_id="AABBCCDD00112233")
        res = self.db.import_entries(
            [{"player_name": "老王改名了", "note": "",
              "peer_id": "aabbccdd00112233"}], strategy="skip")
        self.assertEqual(res["renamed"], 1)
        self.assertEqual(res["inserted"], 0)          # 绝不新增第二条
        self.assertEqual(self.db.get_count(), 1)
        self.assertEqual(self.db.get(eid)["player_name"], "老王改名了")
        self.assertEqual(self.db.get(eid)["note"], "备注留着")   # skip 不动备注

    def test_import_rename_happens_under_every_strategy(self):
        """改名不受策略影响（"谁"是确定的）；策略只管备注/时间。"""
        for i, strategy in enumerate(("skip", "update_note", "overwrite")):
            db = BlacklistDB(self.path(f"rename_{i}.db"))
            try:
                db.add("旧名字", "我的备注", peer_id="AABBCCDD00112233")
                res = db.import_entries(
                    [{"player_name": "新名字", "note": "文件备注",
                      "peer_id": "AABBCCDD00112233"}], strategy=strategy)
                self.assertEqual(res["renamed"], 1, strategy)
                self.assertEqual(res["inserted"], 0, strategy)
                self.assertEqual(db.get_count(), 1, strategy)
                row = db.get_all()[0]
                self.assertEqual(row["player_name"], "新名字", strategy)
                want = "我的备注" if strategy == "skip" else "文件备注"
                self.assertEqual(row["note"], want, strategy)
            finally:
                db.close()

    def test_import_name_only_row_does_not_overwrite_id_row(self):
        """文件里那条没有 ID、库里同名那条有 ID → 是两个人 → 各留一条。

        （有 ID 的那条**不会**被文件里的裸名字顶掉：身份不同。）
        """
        eid = self.db.add("N", "有ID的", peer_id="AABBCCDD00112233")
        res = self.db.import_entries(
            [{"player_name": "N", "note": "没ID的"}], strategy="overwrite")
        self.assertEqual(res["inserted"], 1)
        self.assertEqual(self.db.get_count(), 2)
        self.assertEqual(self.db.get(eid)["note"], "有ID的")     # 没被动过

    def test_import_name_only_row_matches_name_only_row(self):
        """两边都没有 ID → 名字就是身份 → 按策略处理，不新增。"""
        eid = self.db.add("老王", "原来")
        res = self.db.import_entries(
            [{"player_name": "老王", "note": "文件里的"}], strategy="skip")
        self.assertEqual(res["inserted"], 0)
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(self.db.get_count(), 1)
        self.assertEqual(self.db.get(eid)["note"], "原来")

    def test_import_skips_rows_without_name(self):
        res = self.db.import_entries(
            [{"player_name": ""}, {"note": "只有备注"}, {"player_name": "  "}],
            strategy="skip")
        self.assertEqual(res, {"inserted": 0, "updated": 0, "skipped": 3, "renamed": 0})
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
                         {"inserted": 0, "updated": 0, "skipped": 0, "renamed": 0})
        self.assertEqual(self.db.import_entries(None),
                         {"inserted": 0, "updated": 0, "skipped": 0, "renamed": 0})


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
            self.assertEqual(cols, {"id", "player_name", "note", "peer_id",
                                    "prev_names", "created_at"})
            names = sorted(r["player_name"] for r in db.get_all())
            # 名称栏为空的条目用 player_id 补名字；没有名字的占位行丢弃
            self.assertEqual(names, ["PlayerX", "PlayerZ", "？"])
            # 备注与添加时间保留
            by_name = {r["player_name"]: r for r in db.get_all()}
            self.assertEqual(by_name["PlayerZ"]["note"], "名字填在ID栏")
            self.assertEqual(by_name["PlayerZ"]["created_at"],
                             "2026-01-01 10:00:00")
            # 老条目一律没有 PeerID（凭空编一个只会认错人）
            self.assertTrue(all(not r["peer_id"] for r in db.get_all()))
            # 名单里的 ? 依然可索引可命中
            m = Matcher(db)
            self.assertEqual(len(m.check(["？"])), 1)
        finally:
            db.close()

    def test_migration_drops_legacy_encounters(self):
        """抓屏时代的 encounters 表没有任何生产者/读者了，直接删掉。"""
        path = self._make_legacy_db()
        db = BlacklistDB(path)
        try:
            tables = {r[0] for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("encounters", tables)
            self.assertIn("seen_players", tables)
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

    def test_v1_1_db_gets_peer_id_column(self):
        """v1.1.x 的库（有 encounters、没有 peer_id）也能无损升上来。"""
        import sqlite3
        path = self.path("v11.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_name TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
        conn.execute("INSERT INTO blacklist (player_name, note) "
                     "VALUES ('老条目', '还在')")
        conn.execute("INSERT INTO encounters (blacklist_id, name_seen) "
                     "VALUES (1, '老条目')")
        conn.commit()
        conn.close()

        db = BlacklistDB(path)
        try:
            rows = db.get_all()
            self.assertEqual(len(rows), 1)          # 名字与备注都保住
            self.assertEqual(rows[0]["note"], "还在")
            self.assertEqual(rows[0]["peer_id"], "")   # 老条目留空
            tables = {r[0] for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("encounters", tables)
        finally:
            db.close()


# ==========================================================================
class TestNormalizePeerId(unittest.TestCase):
    """PeerID 归一化：洗掉大小写/空格/连字符/0x 前缀，非法的给空串。"""

    def test_normalizes(self):
        self.assertEqual(normalize_peer_id("aabbccdd00112233"),
                         "AABBCCDD00112233")
        self.assertEqual(normalize_peer_id("AABBCCDD 0011-2233"),
                         "AABBCCDD00112233")
        self.assertEqual(normalize_peer_id("0xAABBCCDD00112233"),
                         "AABBCCDD00112233")
        self.assertEqual(normalize_peer_id("  aabbccdd00112233  "),
                         "AABBCCDD00112233")

    def test_short_value_is_padded(self):
        """插件永远给 16 位，但手工粘进来的 8 位也不该因此匹配不上。"""
        self.assertEqual(normalize_peer_id("ff"), "00000000000000FF")

    def test_rejects_garbage(self):
        for bad in ("", None, "   ", "zzz", "0x", "12345678901234567",
                    "AABBCCDD-0011-2233-x"):
            self.assertEqual(normalize_peer_id(bad), "", repr(bad))


# ==========================================================================
class TestPrevNames(unittest.TestCase):
    """曾用名的存/取/合并（纯函数，不碰数据库）。"""

    def test_parse_json_and_plain(self):
        self.assertEqual(parse_prev_names('["甲", "乙"]'), ["甲", "乙"])
        self.assertEqual(parse_prev_names("甲 / 乙"), ["甲", "乙"])
        self.assertEqual(parse_prev_names("甲,乙;丙\n丁"), ["甲", "乙", "丙", "丁"])
        self.assertEqual(parse_prev_names(""), [])
        self.assertEqual(parse_prev_names(None), [])

    def test_format(self):
        self.assertEqual(format_prev_names('["甲","乙"]'), "甲 / 乙")
        self.assertEqual(format_prev_names(""), "")

    def test_dump_dedupes_and_skips_empty(self):
        self.assertEqual(dump_prev_names(["甲", "甲", " ", "乙"]),
                         '["甲", "乙"]')
        self.assertEqual(dump_prev_names([]), "")
        self.assertEqual(dump_prev_names("甲 / 乙"), '["甲", "乙"]')

    def test_push_dedupes(self):
        self.assertEqual(push_prev_name('["甲"]', "甲"), '["甲"]')
        self.assertEqual(push_prev_name('["甲"]', "乙"), '["甲", "乙"]')
        self.assertEqual(push_prev_name("", ""), "")

    def test_push_caps_length(self):
        value = ""
        for i in range(20):
            value = push_prev_name(value, f"名字{i}")
        self.assertEqual(len(parse_prev_names(value)), PREV_NAME_LIMIT)
        # 留的是最近几个
        self.assertEqual(parse_prev_names(value)[-1], "名字19")

    def test_names_with_separators_survive_json(self):
        """名字里带逗号/斜杠也不能被切碎（所以存 JSON 而不是分隔串）。"""
        value = dump_prev_names(["Smith, John", "a/b"])
        self.assertEqual(parse_prev_names(value), ["Smith, John", "a/b"])


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
class TestPeerIdLayer(TempCase):
    """PeerID 是第一判据：有 ID 的条目只走精确层，不进名字索引。"""

    def setUp(self):
        super().setUp()
        self.db = BlacklistDB(self.path("blacklist.db"))
        self.with_id = self.db.add("有ID的人", "备注", peer_id="AABBCCDD00112233")
        self.legacy = self.db.add("老条目")
        self.matcher = Matcher(self.db)

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def test_stats_split(self):
        self.assertEqual(self.matcher.stats(), {"peers": 1, "names": 1})

    def test_check_peer_hits(self):
        hit = self.matcher.check_peer("aabbccdd 00112233")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0]["id"], self.with_id)
        self.assertEqual(hit[1], 100.0)

    def test_check_peer_misses(self):
        self.assertIsNone(self.matcher.check_peer("0000000000000000"))
        self.assertIsNone(self.matcher.check_peer(""))
        self.assertIsNone(self.matcher.check_peer("zzz"))
        self.assertIsNone(self.matcher.check_peer(None))

    def test_entry_with_peer_id_is_not_name_indexed(self):
        """有 ID 就不再靠名字 —— 否则冒用同名的人也会被算进来。"""
        self.assertEqual(self.matcher.check(["有ID的人"]), [])
        self.assertNotIn("有id的人", self.matcher.name_allowlist())

    def test_legacy_entry_still_name_indexed(self):
        hits = self.matcher.check(["老条目"])
        self.assertEqual([h[0]["id"] for h in hits], [self.legacy])

    def test_adding_peer_id_moves_entry_to_exact_layer(self):
        self.db.set_peer_id(self.legacy, "1111222233334444")
        self.matcher.reload()
        self.assertEqual(self.matcher.stats(), {"peers": 2, "names": 0})
        self.assertEqual(self.matcher.check(["老条目"]), [])
        self.assertEqual(self.matcher.check_peer("1111222233334444")[0]["id"],
                         self.legacy)

    def test_reload_after_delete(self):
        self.db.delete(self.with_id)
        self.matcher.reload()
        self.assertIsNone(self.matcher.check_peer("AABBCCDD00112233"))
        self.assertEqual(self.matcher.stats(), {"peers": 0, "names": 1})


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

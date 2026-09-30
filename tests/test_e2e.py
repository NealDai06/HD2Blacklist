# -*- coding: utf-8 -*-
"""阶段 8-9 验收测试：端到端联调。

覆盖：
    * ProcessWatcher 状态去重 / 工具快照 / 回调
    * HD2BlacklistApp 的完整装配（无 GUI 模式）
    * 一条真实的数据流：游戏启动 → 冷启动会话 → 聊天框命中 →
      证据落盘 → DB 原子更新 → on_encounter 回调
    * 关停顺序与幂等性

所有落盘路径都被重定向到临时目录，不会污染交付的 data/。

运行： python -m unittest tests.test_e2e -v
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import unittest

from PIL import Image

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import database as db_mod                        # noqa: E402
import main as main_mod                          # noqa: E402
import notification_config as nc_mod             # noqa: E402
import region_config as rc_mod                   # noqa: E402
import scan_scheduler as sched_mod               # noqa: E402
from config import COLD_START_SESSION            # noqa: E402
from process_watcher import ProcessWatcher, list_process_names   # noqa: E402

TMP_ROOT = os.path.join(_ROOT, ".test_tmp")


class FakeCapture:
    def __init__(self, std=0.0):
        self.default = Image.new("RGB", (420, 160), (12, 12, 12))
        self.std = std
        self.grab_calls = 0

    def grab(self, region_key):
        self.grab_calls += 1
        return self.default

    def grab_region(self, region):
        return self.default

    def region_std(self, img):
        return self.std

    def clamp_region(self, r):
        return r

    def virtual_screen(self):
        return {"left": 0, "top": 0, "width": 1920, "height": 1080}

    def close(self):
        pass


class FakeOCR:
    def __init__(self, results=None):
        self.default = list(results or [])
        self.calls = 0

    def recognize_raw(self, img):
        self.calls += 1
        return list(self.default)

    def recognize_text(self, img, min_conf=0.0):
        return " ".join(t for t, c in self.recognize_raw(img) if c >= min_conf)

    def warmup(self):
        return True

    def preprocess(self, img):
        from ocr_engine import OCREngine
        return OCREngine().preprocess(img)


class FakeNotifier:
    def __init__(self):
        self.alerts = []
        self.batches = []          # 每次 alert_batch 的入参

    def alert(self, entry, score, source):
        self.alerts.append((entry.get("player_name"), score, source))
        return True

    def alert_batch(self, hits):
        hits = list(hits)
        self.batches.append(hits)
        for entry, score, source in hits:
            self.alerts.append((entry.get("player_name"), score, source))
        return len(hits)

    def close(self):
        pass

    def preview(self, *a, **k):
        return True

    def render_preview_image(self, *a, **k):
        return Image.new("RGBA", (360, 100))

    def play_sound(self, *a, **k):
        pass


class TempCase(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.tmp = os.path.join(TMP_ROOT, self._testMethodName)
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)
        self._restore = []

    def tearDown(self):
        for obj, attr, old in reversed(self._restore):
            setattr(obj, attr, old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *p):
        return os.path.join(self.tmp, *p)

    def _patch(self, obj, attr, value):
        self._restore.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def redirect_defaults(self):
        """把所有配置/DB 的默认路径指向临时目录。"""
        self._patch(db_mod.BlacklistDB.__init__, "__defaults__",
                    (self.path("blacklist.db"), 10.0))
        self._patch(nc_mod.NotificationConfig.__init__, "__defaults__",
                    (self.path("notification.json"),))
        self._patch(rc_mod.RegionConfig.__init__, "__defaults__",
                    (self.path("user_config.json"),))
        self._patch(sched_mod, "EVIDENCE_DIR", self.path("evidence"))
        os.makedirs(self.path("evidence"), exist_ok=True)


# ==========================================================================
class TestProcessWatcher(TempCase):
    def test_toolhelp_lists_processes(self):
        names = list_process_names()
        if not names:
            self.skipTest("CreateToolhelp32Snapshot 不可用")
        self.assertTrue(any(n.endswith(".exe") for n in names))

    def test_check_now_false_for_bogus_target(self):
        events = []
        w = ProcessWatcher(on_start=lambda: events.append("start"),
                           on_stop=lambda: events.append("stop"),
                           process_names=("definitely_not_a_real_game.exe",))
        self.assertFalse(w.check_now())
        self.assertEqual(events, [])

    def test_check_now_true_for_running_python(self):
        events = []
        w = ProcessWatcher(on_start=lambda: events.append("start"),
                           on_stop=lambda: events.append("stop"),
                           process_names=("python.exe",))
        self.assertTrue(w.check_now())
        self.assertEqual(events, ["start"])
        self.assertTrue(w.is_game_running())

    def test_state_change_is_deduplicated(self):
        events = []
        w = ProcessWatcher(on_start=lambda: events.append("start"),
                           on_stop=lambda: events.append("stop"),
                           process_names=("python.exe",))
        w._set_state(True, "t")
        w._set_state(True, "t")
        w._set_state(False, "t")
        w._set_state(False, "t")
        self.assertEqual(events, ["start", "stop"])

    def test_callback_exception_does_not_propagate(self):
        w = ProcessWatcher(on_start=lambda: 1 / 0,
                           process_names=("python.exe",))
        w._set_state(True, "t")          # 不应抛异常
        self.assertTrue(w.is_game_running())

    def test_stop_sets_event(self):
        w = ProcessWatcher(process_names=("python.exe",))
        w.stop()
        self.assertTrue(w._stop.is_set())


# ==========================================================================
class TestAppEndToEnd(TempCase):
    def setUp(self):
        super().setUp()
        self.redirect_defaults()
        # 把冷启动扫描推到很远的未来，避免它干扰其它用例
        self._patch(main_mod, "COLD_START_DELAY", 999.0)

    def _build(self):
        app = main_mod.HD2BlacklistApp(gui_enabled=False)
        self.addCleanup(app.shutdown)

        cap = FakeCapture()
        ocr = FakeOCR([("PlayerX has joined the game", 0.99)])
        notifier = FakeNotifier()
        self.events = []

        app.capture = cap
        app.ocr = ocr
        app.notifier = notifier
        app.chat_scanner.capture = cap
        app.chat_scanner.ocr = ocr
        app.chat_scanner.scheduler = app.scheduler
        app.esc_trigger.capture = cap
        app.esc_trigger.ocr = ocr
        app.scheduler.capture = cap
        app.scheduler.ocr = ocr
        app.scheduler.notifier = notifier
        app.scheduler.on_encounter = self.events.append
        return app

    def test_wiring(self):
        app = self._build()
        self.assertIsNotNone(app.db)
        self.assertIsNotNone(app.scheduler)
        self.assertIsNotNone(app.chat_scanner)
        self.assertIsNotNone(app.esc_trigger)
        self.assertIsNotNone(app.proc_watcher)
        self.assertIsNone(app.gui)
        self.assertIsNone(app.chat_hotkey)          # 默认关闭
        # ChatScanner 与调度器已正确接线
        self.assertIs(app.chat_scanner.scheduler, app.scheduler)
        self.assertIs(app.esc_trigger.game_active, app.scheduler.game_active)

    def test_no_chat_monitor_attribute(self):
        """旧的持续扫描器必须彻底消失。"""
        app = self._build()
        self.assertFalse(hasattr(app, "chat_monitor"))
        self.assertFalse(hasattr(app, "keyword_cfg"))

    def test_app_starts_with_hotkey_enabled(self):
        """回归：启用热键后启动不能再崩。

        曾经的 bug：__init__ 里先启动热键、后建 _threads，于是
        _spawn() 抛 AttributeError: no attribute '_threads'，
        打包版一点「启用热键扫描」下次启动就弹 PyInstaller 报错框。
        """
        import json
        import chat_hotkey as chat_hotkey_mod
        import hotkey_config as hkc_mod
        self._patch(hkc_mod.HotkeyConfig.__init__, "__defaults__",
                    (self.path("hotkey.json"),))
        with open(self.path("hotkey.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "enabled": True, "vk": 0x77,
                       "ctrl": False, "alt": False, "shift": False}, f)
        # 测试里不要真去抢系统全局热键，也不要真读键盘
        self._patch(chat_hotkey_mod, "register_supported", lambda: False)
        self._patch(chat_hotkey_mod, "_get_async_key_state", lambda vk: 0)

        app = main_mod.HD2BlacklistApp(gui_enabled=False)
        self.addCleanup(app.shutdown)

        self.assertIsNotNone(app.chat_hotkey)
        self.assertTrue(app.chat_hotkey.enabled)
        self.assertTrue(any(t.name == "ChatScanHotkey" for t in app._threads))

    def test_chat_scan_full_dataflow(self):
        app = self._build()
        app.ocr.default = [("PlayerX", 0.99)]
        app.db.add("7656119", "PlayerX", "恶意TK", 3)
        app.scheduler.reload_blacklist()

        app._on_game_start()
        self.assertTrue(app.scheduler.game_active.is_set())

        res = app.chat_scanner.scan_now()        # 按需触发一次
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["hits"], 1)

        rows = app.db.get_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["encounter_count"], 1)
        self.assertIsNotNone(rows[0]["last_seen"])

        enc = app.db.get_recent_encounters()
        self.assertEqual(len(enc), 1)
        self.assertEqual(enc[0]["name_seen"], "PlayerX")
        self.assertEqual(enc[0]["source"], "chat_manual")

        # 证据落盘
        files = os.listdir(self.path("evidence"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith(".jpg"))

        # on_encounter 回调
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["player_name"], "PlayerX")
        self.assertEqual(self.events[0]["encounter_count"], 1)

        # 提示
        self.assertEqual(len(app.notifier.alerts), 1)

    def test_repeated_clicks_are_deduped(self):
        """连点扫描按钮：60 秒内同一玩家只计数一次。"""
        app = self._build()
        app.ocr.default = [("PlayerX", 0.99)]
        app.db.add("7656119", "PlayerX")
        app.scheduler.reload_blacklist()

        first = app.chat_scanner.scan_now()
        second = app.chat_scanner.scan_now()
        third = app.chat_scanner.scan_now()

        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(third["processed"], 0)
        self.assertEqual(app.db.get_all()[0]["encounter_count"], 1)
        self.assertEqual(len(app.notifier.batches), 1)

    def test_batch_hit_two_players_one_sound_call(self):
        """一次扫描命中 2 个玩家 → 2 个通知栏，1 次 alert_batch。"""
        app = self._build()
        app.ocr.default = [("Alpha joined", 0.99), ("Bravo joined", 0.99)]
        app.db.add("a", "Alpha")
        app.db.add("b", "Bravo")
        app.scheduler.reload_blacklist()

        res = app.chat_scanner.scan_now()
        self.assertEqual(res["processed"], 2)
        self.assertEqual(len(app.notifier.batches), 1)
        self.assertEqual(len(app.notifier.batches[0]), 2)
        self.assertEqual(app.db.get_total_encounter_count(), 2)
        self.assertEqual(len(self.events), 2)

    def test_chat_scanner_works_without_game_running(self):
        """手动扫描是用户主动行为，不要求游戏在跑。"""
        app = self._build()
        app.ocr.default = [("PlayerX", 0.99)]
        app.db.add("7656119", "PlayerX")
        app.scheduler.reload_blacklist()
        self.assertFalse(app.scheduler.game_active.is_set())
        res = app.chat_scanner.scan_now()
        self.assertEqual(res["status"], "ok")

    def test_game_stop_deactivates(self):
        app = self._build()
        app._on_game_start()
        self.assertTrue(app.scheduler.is_active())
        app._on_game_stop()
        self.assertFalse(app.scheduler.game_active.is_set())
        self.assertFalse(app.scheduler.is_active())
        self.assertFalse(app.scheduler.session_busy())    # 会话已被终止

    def test_no_background_chat_scanning(self):
        """没有任何后台循环会去抓聊天框：不主动调用就永远是 0 次。"""
        app = self._build()
        app._on_game_start()
        time.sleep(0.3)                     # 等一会儿，什么也不该发生
        app._on_game_stop()
        self.assertEqual(app.capture.grab_calls, 0)
        self.assertEqual(app.chat_scanner.scan_count, 0)

    def test_pause_blocks_monitoring(self):
        app = self._build()
        app._on_game_start()
        app.on_pause(True)
        self.assertTrue(app.scheduler.game_active.is_set())
        self.assertFalse(app.scheduler.is_active())
        app.on_pause(False)
        self.assertTrue(app.scheduler.is_active())

    def test_cold_start_session_runs(self):
        self._patch(main_mod, "COLD_START_DELAY", 0.05)
        app = self._build()
        app.db.add("7656119", "PlayerX")
        app.scheduler.reload_blacklist()
        app.capture.default = Image.new("RGB", (400, 120), (30, 30, 30))
        app.ocr.default = [("PlayerX", 0.99), ("Other", 0.95)]

        app._on_game_start()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if app.db.get_all()[0]["encounter_count"] > 0:
                break
            time.sleep(0.05)
        self.assertGreater(app.db.get_all()[0]["encounter_count"], 0)
        self.assertEqual(app.db.get_recent_encounters()[0]["source"],
                         "cold_start")

    def test_shutdown_is_idempotent(self):
        app = self._build()
        app._on_game_start()
        app.shutdown()
        app.shutdown()                              # 第二次不应抛异常
        self.assertTrue(app._stop.is_set())

    def test_shutdown_closes_db(self):
        app = self._build()
        app.shutdown()
        import sqlite3
        with self.assertRaises(sqlite3.ProgrammingError):
            app.db.conn.execute("SELECT 1")

    def test_esc_menu_flow(self):
        """ESC → 菜单已打开 → 启动 esc_menu 会话 → 命中入库。"""
        app = self._build()
        app.db.add("7656119", "PlayerX")
        app.scheduler.reload_blacklist()
        app._on_game_start()

        app.ocr.default = [("PlayerX", 0.99)]        # 玩家列表是纯名字
        app.capture.std = 99.0                      # 菜单已打开
        app.esc_trigger.active_check = app.scheduler.is_active
        app.esc_trigger._on_press()

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if app.db.get_all()[0]["encounter_count"] > 0:
                break
            time.sleep(0.05)
        self.assertGreater(app.db.get_all()[0]["encounter_count"], 0)
        self.assertEqual(app.db.get_recent_encounters()[0]["source"], "esc_menu")


# ==========================================================================
class TestAppDefaults(TempCase):
    """不重定向路径，验证首次启动会生成示例配置。"""

    def test_first_run_generates_example_configs(self):
        import config
        from notification_config import ensure_notification_file

        backups = []
        for p in (config.NOTIFICATION_PATH,):
            if os.path.exists(p):
                b = p + ".bak"
                shutil.move(p, b)
                backups.append((p, b))
        try:
            ensure_notification_file()
            config.ensure_dirs()
            self.assertTrue(os.path.exists(config.NOTIFICATION_PATH))
            self.assertTrue(os.path.isdir(config.EVIDENCE_DIR))
            self.assertTrue(os.path.isdir(config.LOG_DIR))
            # keywords.json 已随关键词系统一起移除
            self.assertFalse(hasattr(config, "KEYWORDS_PATH"))
        finally:
            for p, b in backups:
                try:
                    os.remove(p)
                except OSError:
                    pass
                shutil.move(b, p)


# ==========================================================================
class TestDeletedModulesAreGone(unittest.TestCase):
    """改进点一：旧模块必须彻底删除，且无残留引用。"""

    def test_module_files_deleted(self):
        for name in ("chat_monitor.py", "keyword_config.py"):
            self.assertFalse(os.path.exists(os.path.join(_ROOT, name)), name)
        self.assertFalse(
            os.path.exists(os.path.join(_ROOT, "data", "keywords.json")))

    def test_modules_not_importable(self):
        for mod in ("chat_monitor", "keyword_config"):
            with self.assertRaises(ImportError):
                __import__(mod)
            sys.modules.pop(mod, None)

    def test_no_dangling_references_in_source(self):
        """全项目源码里不应再出现 keyword_config / chat_monitor 引用。"""
        # 拼接出关键字，避免本测试自身的字符串把检查搞脏
        needles = []
        for mod in ("keyword_config", "chat_monitor"):
            for prefix in ("import ", "from "):
                needles.append(prefix + mod)

        offenders = []
        for folder in (_ROOT, os.path.join(_ROOT, "tests"),
                       os.path.join(_ROOT, "tools")):
            if not os.path.isdir(folder):
                continue
            for fn in sorted(os.listdir(folder)):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(folder, fn)
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                for needle in needles:
                    if needle in text:
                        offenders.append(f"{fn}: {needle}")
        self.assertEqual(offenders, [], f"仍有残留引用: {offenders}")

    def test_chat_scanner_and_hotkey_exist(self):
        import chat_hotkey
        import chat_scanner
        self.assertTrue(hasattr(chat_scanner, "ChatScanner"))
        self.assertTrue(hasattr(chat_hotkey, "ChatScanHotkey"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

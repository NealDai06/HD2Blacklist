# -*- coding: utf-8 -*-
"""自定义「聊天框扫描快捷键」测试。

运行： python -m unittest tests.test_hotkey -v
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sys
import threading
import time
import unittest
from ctypes import wintypes

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.scanning import chat_hotkey as hk_mod  # noqa: E402
from app import config  # noqa: E402
from app.settings import hotkey_config as hkc  # noqa: E402
from app.ui import hotkey_dialog as hkd  # noqa: E402
from app import application as main_mod  # noqa: E402
from app.settings.hotkey_config import (HotkeyConfig, combo_name, keysym_to_vk,   # noqa: E402
                           modifier_of_keysym, vk_to_name)

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
class TestKeyMapping(unittest.TestCase):
    def test_function_keys(self):
        self.assertEqual(keysym_to_vk("F1"), 0x70)
        self.assertEqual(keysym_to_vk("F8"), 0x77)
        self.assertEqual(keysym_to_vk("F12"), 0x7B)
        self.assertEqual(keysym_to_vk("F24"), 0x87)

    def test_letters_and_digits(self):
        self.assertEqual(keysym_to_vk("a"), ord("A"))
        self.assertEqual(keysym_to_vk("Z"), ord("Z"))
        self.assertEqual(keysym_to_vk("5"), ord("5"))

    def test_special_keys(self):
        self.assertEqual(keysym_to_vk("Escape"), 0x1B)
        self.assertEqual(keysym_to_vk("Return"), 0x0D)
        self.assertEqual(keysym_to_vk("space"), 0x20)

    def test_unknown_keysym(self):
        self.assertIsNone(keysym_to_vk("F25"))
        self.assertIsNone(keysym_to_vk(""))
        self.assertIsNone(keysym_to_vk(None))
        self.assertIsNone(keysym_to_vk("Super_L"))

    def test_modifier_keysym_detection(self):
        self.assertEqual(modifier_of_keysym("Control_L"), "ctrl")
        self.assertEqual(modifier_of_keysym("Alt_R"), "alt")
        self.assertEqual(modifier_of_keysym("Shift_L"), "shift")
        self.assertIsNone(modifier_of_keysym("F8"))

    def test_vk_to_name(self):
        self.assertEqual(vk_to_name(0x77), "F8")
        self.assertEqual(vk_to_name(ord("S")), "S")
        self.assertEqual(vk_to_name(0x31), "1")
        self.assertTrue(vk_to_name(0x99).startswith("VK 0x"))
        self.assertEqual(vk_to_name(None), "?")

    def test_combo_name(self):
        self.assertEqual(combo_name(0x77), "F8")
        self.assertEqual(combo_name(0x77, ctrl=True), "Ctrl+F8")
        self.assertEqual(combo_name(0x77, ctrl=True, shift=True, alt=True),
                         "Ctrl+Alt+Shift+F8")


# ==========================================================================
class TestHotkeyConfig(TempCase):
    def test_defaults_when_missing(self):
        cfg = HotkeyConfig(self.path("hotkey.json"))
        data = cfg.get()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["vk"], config.CHAT_SCAN_HOTKEY_VK)
        self.assertEqual(cfg.display, "F8")
        self.assertFalse(os.path.exists(self.path("hotkey.json")))

    def test_set_binding_persists(self):
        cfg = HotkeyConfig(self.path("hotkey.json"))
        cfg.set_binding(ord("S"), ctrl=True, shift=True)
        again = HotkeyConfig(self.path("hotkey.json"))
        self.assertEqual(again.vk, ord("S"))
        self.assertTrue(again.get()["ctrl"])
        self.assertTrue(again.get()["shift"])
        self.assertFalse(again.get()["alt"])
        self.assertEqual(again.display, "Ctrl+Shift+S")

    def test_set_enabled_persists(self):
        cfg = HotkeyConfig(self.path("hotkey.json"))
        cfg.set_enabled(True)
        self.assertTrue(HotkeyConfig(self.path("hotkey.json")).enabled)
        cfg.set_enabled(False)
        self.assertFalse(HotkeyConfig(self.path("hotkey.json")).enabled)

    def test_rejects_escape(self):
        cfg = HotkeyConfig(self.path("hotkey.json"))
        with self.assertRaises(ValueError) as ctx:
            cfg.set_binding(0x1B)
        self.assertIn("ESC", str(ctx.exception))
        self.assertEqual(cfg.vk, config.CHAT_SCAN_HOTKEY_VK)   # 没被改掉

    def test_rejects_invalid_vk(self):
        cfg = HotkeyConfig(self.path("hotkey.json"))
        for bad in (0, 5, 0x1000, "abc", None):
            with self.assertRaises(ValueError):
                cfg.set_binding(bad)

    def test_sanitizes_corrupt_file(self):
        with open(self.path("hotkey.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        cfg = HotkeyConfig(self.path("hotkey.json"))
        self.assertEqual(cfg.vk, config.CHAT_SCAN_HOTKEY_VK)

    def test_reads_file_saved_with_bom(self):
        """用记事本改过 hotkey.json（UTF-8 BOM）不能让热键静默失效。"""
        import json
        with open(self.path("hotkey.json"), "w", encoding="utf-8-sig") as f:
            json.dump({"version": 1, "enabled": True, "vk": 0x78,
                       "ctrl": True, "alt": False, "shift": False}, f)
        cfg = HotkeyConfig(self.path("hotkey.json"))
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.vk, 0x78)
        self.assertEqual(cfg.display, "Ctrl+F9")

    def test_sanitizes_forbidden_vk_in_file(self):
        import json
        with open(self.path("hotkey.json"), "w", encoding="utf-8") as f:
            json.dump({"vk": 0x1B, "enabled": True}, f)
        cfg = HotkeyConfig(self.path("hotkey.json"))
        self.assertEqual(cfg.vk, config.CHAT_SCAN_HOTKEY_VK)   # ESC 被拒
        self.assertTrue(cfg.enabled)                           # 其它字段保留

    def test_sanitizes_out_of_range_vk(self):
        import json
        with open(self.path("hotkey.json"), "w", encoding="utf-8") as f:
            json.dump({"vk": 99999}, f)
        self.assertEqual(HotkeyConfig(self.path("hotkey.json")).vk,
                         config.CHAT_SCAN_HOTKEY_VK)

    def test_binding_dict(self):
        cfg = HotkeyConfig(self.path("hotkey.json"))
        cfg.set_binding(0x77, alt=True)
        b = cfg.binding()
        self.assertEqual(b, {"vk": 0x77, "ctrl": False, "alt": True,
                             "shift": False})

    def test_reset_removes_file(self):
        cfg = HotkeyConfig(self.path("hotkey.json"))
        cfg.set_binding(ord("K"))
        cfg.set_enabled(True)
        cfg.reset()
        self.assertFalse(os.path.exists(self.path("hotkey.json")))
        self.assertEqual(cfg.vk, config.CHAT_SCAN_HOTKEY_VK)
        self.assertFalse(cfg.enabled)

    def test_ensure_hotkey_file(self):
        real = config.HOTKEY_PATH
        backup = None
        if os.path.exists(real):
            backup = real + ".bak"
            shutil.move(real, backup)
        try:
            hkc.ensure_hotkey_file()
            self.assertTrue(os.path.exists(real))
            import json
            with open(real, encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("vk", data)
            self.assertFalse(data["enabled"])
            # 二次调用不覆盖
            before = os.path.getmtime(real)
            hkc.ensure_hotkey_file()
            self.assertEqual(before, os.path.getmtime(real))
        finally:
            try:
                os.remove(real)
            except OSError:
                pass
            if backup:
                shutil.move(backup, real)


# ==========================================================================
class FakeScanner:
    def __init__(self):
        self.calls = []

    def scan_now(self, source=None):
        self.calls.append(source)
        return {"status": "ok", "hits": 0}


class TestHotkeyCombo(unittest.TestCase):
    """组合键判定 + 上升沿 + 去抖（用假的按键状态）。"""

    def setUp(self):
        self._orig = hk_mod._get_async_key_state
        self.down = set()

        def fake_state(vk):
            return 0x8000 if vk in self.down else 0

        hk_mod._get_async_key_state = fake_state

    def tearDown(self):
        hk_mod._get_async_key_state = self._orig

    def _hotkey(self, **hk):
        scanner = FakeScanner()
        h = hk_mod.ChatScanHotkey(scanner, hotkey=hk or {"vk": 0x77})
        return h, scanner

    def test_plain_key(self):
        h, _ = self._hotkey(vk=0x77)
        self.assertFalse(h.combo_down())
        self.down.add(0x77)
        self.assertTrue(h.combo_down())

    def test_ctrl_must_match(self):
        h, _ = self._hotkey(vk=ord("S"), ctrl=True)
        self.down.add(ord("S"))
        self.assertFalse(h.combo_down())          # 少了 Ctrl
        self.down.add(hk_mod.VK_CONTROL)
        self.assertTrue(h.combo_down())

    def test_extra_ctrl_blocks_when_not_bound(self):
        h, _ = self._hotkey(vk=0x77)
        self.down.update({0x77, hk_mod.VK_CONTROL})
        self.assertFalse(h.combo_down())          # 没绑 Ctrl 却按了 Ctrl

    def test_extra_alt_blocks_when_not_bound(self):
        h, _ = self._hotkey(vk=0x77)
        self.down.update({0x77, hk_mod.VK_MENU})
        self.assertFalse(h.combo_down())

    def test_shift_is_ignored_when_not_bound(self):
        """游戏中按着 Shift 跑动时，不带 Shift 的热键仍要能触发。"""
        h, _ = self._hotkey(vk=0x77)
        self.down.update({0x77, hk_mod.VK_SHIFT})
        self.assertTrue(h.combo_down())

    def test_shift_required_when_bound(self):
        h, _ = self._hotkey(vk=ord("S"), shift=True)
        self.down.add(ord("S"))
        self.assertFalse(h.combo_down())
        self.down.add(hk_mod.VK_SHIFT)
        self.assertTrue(h.combo_down())

    def test_display_name(self):
        h, _ = self._hotkey(vk=0x77, ctrl=True)
        self.assertEqual(h.display_name, "Ctrl+F8")

    def test_rising_edge_triggers_once(self):
        h, scanner = self._hotkey(vk=0x77)
        h._loop_thread = None
        t = threading.Thread(target=h._loop, daemon=True)
        t.start()
        time.sleep(0.05)
        self.down.add(0x77)                       # 按下
        time.sleep(0.35)                          # 一直按住
        h.stop()
        t.join(timeout=2)
        self.assertEqual(h.trigger_count, 1)
        self.assertEqual(len(scanner.calls), 1)
        self.assertEqual(scanner.calls[0], config.CHAT_SCAN_HOTKEY_SOURCE)

    def test_debounce_blocks_rapid_repress(self):
        h, scanner = self._hotkey(vk=0x77)
        h._on_press()
        h._on_press()
        h._on_press()
        self.assertEqual(h.trigger_count, 1)

    def test_active_check_skips(self):
        scanner = FakeScanner()
        calls = {"n": 0}

        def active():
            calls["n"] += 1
            return False

        h = hk_mod.ChatScanHotkey(scanner, hotkey={"vk": 0x77},
                                  active_check=active)
        self.down.add(0x77)
        t = threading.Thread(target=h._loop, daemon=True)
        t.start()
        time.sleep(0.25)
        h.stop()
        t.join(timeout=2)
        self.assertEqual(h.trigger_count, 0)
        self.assertGreater(calls["n"], 0)

    def test_stop_is_prompt(self):
        h, _ = self._hotkey(vk=0x77)
        t = threading.Thread(target=h._loop, daemon=True)
        t.start()
        time.sleep(0.05)
        t0 = time.time()
        h.stop(wait=True, timeout=2)
        self.assertLess(time.time() - t0, 1.0)

    def test_get_async_key_state_failure_stops_loop(self):
        def boom(vk):
            raise RuntimeError("no win32")

        hk_mod._get_async_key_state = boom
        h, _ = self._hotkey(vk=0x77)
        t = threading.Thread(target=h._loop, daemon=True)
        t.start()
        t.join(timeout=2)
        self.assertFalse(t.is_alive())            # 线程干净退出，没挂死


# ==========================================================================
class _FakeUser32:
    """假的 user32 行为记录器：只记录调用，不碰真实系统。"""

    def __init__(self, register_ok=True, error=0, window=0x1234):
        self.register_ok = register_ok
        self.error = error
        self.window = window
        self.registered = []
        self.unregistered = []
        self.destroyed = []
        self.quit_posts = []
        self.messages = []          # [(message, wParam)]
        self.closed = False
        self.thread_id = 4242

    # ---- 装到 chat_hotkey 模块上的假实现 ----
    def register(self, hwnd, hotkey_id, mods, vk):
        self.registered.append((hwnd, hotkey_id, mods, vk))
        return self.register_ok

    def unregister(self, hwnd, hotkey_id):
        self.unregistered.append((hwnd, hotkey_id))
        return True

    def create_window(self):
        return self.window

    def destroy_window(self, hwnd):
        self.destroyed.append(hwnd)

    def post_quit(self, thread_id):
        self.quit_posts.append(thread_id)
        return True

    def get_message(self, msg_ptr, hwnd=None):
        """阻塞式取消息（直到测试把 closed 打开）。"""
        while not self.closed:
            if self.messages:
                kind, wparam = self.messages.pop(0)
                msg = ctypes.cast(
                    msg_ptr, ctypes.POINTER(wintypes.MSG)).contents
                msg.message = kind
                msg.wParam = wparam
                return 1
            time.sleep(0.01)
        return 0


class TestGlobalHotkey(unittest.TestCase):
    """RegisterHotKey 路径：成功 / 失败回退 / 消息循环 / 注销。"""

    def setUp(self):
        self.fake = _FakeUser32()
        self._orig = {}
        for name, value in (("register_supported", lambda: True),
                            ("_create_message_window", self.fake.create_window),
                            ("_destroy_window", self.fake.destroy_window),
                            ("_register_hotkey", self.fake.register),
                            ("_unregister_hotkey", self.fake.unregister),
                            ("_get_message", self.fake.get_message),
                            ("_post_thread_quit", self.fake.post_quit),
                            ("_current_thread_id",
                             lambda: self.fake.thread_id),
                            ("_last_error", lambda: self.fake.error),
                            ("_get_async_key_state", lambda vk: 0)):
            self._orig[name] = getattr(hk_mod, name)
            setattr(hk_mod, name, value)
        self.hotkeys = []

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(hk_mod, name, value)
        for h in self.hotkeys:
            h.stop(wait=True, timeout=2)

    def _hotkey(self, **hk):
        scanner = FakeScanner()
        h = hk_mod.ChatScanHotkey(scanner, hotkey=hk or {"vk": 0x77})
        self.hotkeys.append(h)
        return h, scanner

    def _close(self):
        self.fake.closed = True

    # ------------------------------------------------------------ 注册参数
    def test_register_mods(self):
        h, _ = self._hotkey(vk=0x77, ctrl=True, shift=True)
        mods = h.register_mods()
        self.assertTrue(mods & hk_mod.MOD_CONTROL)
        self.assertTrue(mods & hk_mod.MOD_SHIFT)
        self.assertFalse(mods & hk_mod.MOD_ALT)
        self.assertTrue(mods & hk_mod.MOD_NOREPEAT)     # 长按不连发

    # ------------------------------------------------------------ 成功路径
    def test_register_mode_uses_system_hotkey(self):
        h, _ = self._hotkey(vk=0x77)
        h.start()
        self.assertTrue(h.wait_ready(2.0))
        self.assertEqual(h.mode, hk_mod.ChatScanHotkey.MODE_REGISTER)
        self.assertTrue(h.registered)
        self.assertEqual(len(self.fake.registered), 1)
        (hwnd, hotkey_id, mods, vk) = self.fake.registered[0]
        self.assertEqual((hwnd, hotkey_id, vk),
                         (self.fake.window, hk_mod.HOTKEY_ID, 0x77))
        self.assertEqual(mods, hk_mod.MOD_NOREPEAT)
        self.assertIn("全局热键", h.status_text())

    def test_wm_hotkey_triggers_scan_once(self):
        h, scanner = self._hotkey(vk=0x77)
        h.start()
        self.assertTrue(h.wait_ready(2.0))
        # 别的 id 不该触发
        self.fake.messages.append((hk_mod.WM_HOTKEY, 999))
        # 正确的 id 触发一次；紧随其后的重复按键被去抖挡掉
        self.fake.messages.append((hk_mod.WM_HOTKEY, hk_mod.HOTKEY_ID))
        self.fake.messages.append((hk_mod.WM_HOTKEY, hk_mod.HOTKEY_ID))
        deadline = time.time() + 2
        while time.time() < deadline and not scanner.calls:
            time.sleep(0.02)
        time.sleep(0.2)                                  # 再等等，确认真没第二次
        self.assertEqual(h.trigger_count, 1)
        self.assertEqual(scanner.calls, [config.CHAT_SCAN_HOTKEY_SOURCE])

    def test_active_check_blocks_register_mode(self):
        h, scanner = self._hotkey(vk=0x77)
        h.active_check = lambda: False
        h.start()
        self.assertTrue(h.wait_ready(2.0))
        self.fake.messages.append((hk_mod.WM_HOTKEY, hk_mod.HOTKEY_ID))
        time.sleep(0.3)
        self.assertEqual(h.trigger_count, 0)
        self.assertEqual(scanner.calls, [])

    # ------------------------------------------------------------ 注销
    def test_stop_unregisters_and_quits_loop(self):
        h, _ = self._hotkey(vk=0x77)
        h.start()
        self.assertTrue(h.wait_ready(2.0))
        self._close()
        h.stop(wait=True, timeout=2)
        self.assertEqual(self.fake.quit_posts, [self.fake.thread_id])
        self.assertEqual(self.fake.unregistered,
                         [(self.fake.window, hk_mod.HOTKEY_ID)])
        self.assertEqual(self.fake.destroyed, [self.fake.window])
        self.assertFalse(h.registered)

    # ------------------------------------------------------------ 回退路径
    def test_register_failure_falls_back_to_polling(self):
        self.fake.register_ok = False
        self.fake.error = hk_mod.ERROR_HOTKEY_ALREADY_REGISTERED
        h, _ = self._hotkey(vk=0x77)
        h.start()
        self.assertTrue(h.wait_ready(2.0))
        time.sleep(0.05)
        self.assertEqual(h.mode, hk_mod.ChatScanHotkey.MODE_POLLING)
        self.assertIn("占用", h.last_error)
        self.assertEqual(self.fake.destroyed, [self.fake.window])
        self.assertFalse(h.registered)
        self.assertIn("轮询", h.status_text())

    def test_missing_message_window_falls_back(self):
        self.fake.window = None
        h, _ = self._hotkey(vk=0x77)
        h.start()
        self.assertTrue(h.wait_ready(2.0))
        time.sleep(0.05)
        self.assertEqual(h.mode, hk_mod.ChatScanHotkey.MODE_POLLING)
        self.assertIn("消息窗口", h.last_error)
        self.assertEqual(self.fake.registered, [])

    def test_register_exception_falls_back(self):
        def boom(*a, **k):
            raise RuntimeError("no user32")

        hk_mod._create_message_window = boom
        h, _ = self._hotkey(vk=0x77)
        h.start()
        self.assertTrue(h.wait_ready(2.0))
        time.sleep(0.05)
        self.assertEqual(h.mode, hk_mod.ChatScanHotkey.MODE_POLLING)
        self.assertIn("异常", h.last_error)

    def test_register_disabled_by_flag(self):
        h, _ = self._hotkey(vk=0x77)
        h.use_register_hotkey = False
        h.start()
        self.assertTrue(h.wait_ready(2.0))
        self.assertEqual(h.mode, hk_mod.ChatScanHotkey.MODE_POLLING)
        self.assertEqual(self.fake.registered, [])


class _FakeHotkeyCfg:
    """给 main.HD2BlacklistApp._start_hotkey 用的最小配置替身。"""

    enabled = True

    def binding(self):
        return {"vk": 0x77, "ctrl": False, "alt": False, "shift": False}

    @property
    def display(self):
        return "F8"


class TestAppHotkeyWiring(unittest.TestCase):
    """回归：_threads 必须在任何 _spawn 之前建好（曾经导致启动即崩溃）。"""

    def setUp(self):
        self._orig_supported = hk_mod.register_supported
        self._orig_state = hk_mod._get_async_key_state
        hk_mod.register_supported = lambda: False    # 测试里不抢真正的全局热键
        hk_mod._get_async_key_state = lambda vk: 0
        # 故意绕过 __init__：模拟「构造函数还没跑完就启动热键」的场景
        self.app = object.__new__(main_mod.HD2BlacklistApp)
        self.app.log = config.get_logger("app")
        self.app.chat_scanner = FakeScanner()
        self.app.hotkey_cfg = _FakeHotkeyCfg()
        self.app.chat_hotkey = None

    def tearDown(self):
        hk_mod.register_supported = self._orig_supported
        hk_mod._get_async_key_state = self._orig_state
        if self.app.chat_hotkey is not None:
            self.app.chat_hotkey.stop(wait=True, timeout=2)

    def test_spawn_creates_threads_list_if_missing(self):
        t = main_mod.HD2BlacklistApp._spawn(self.app, lambda: None, "Probe")
        self.assertIn(t, self.app._threads)

    def test_start_hotkey_without_threads_attr(self):
        main_mod.HD2BlacklistApp._start_hotkey(self.app)
        self.assertIsNotNone(self.app.chat_hotkey)

    def test_apply_hotkey_config_returns_status_text(self):
        cfg = {"enabled": True, "vk": 0x77, "ctrl": False, "alt": False,
               "shift": False}
        text = main_mod.HD2BlacklistApp.apply_hotkey_config(self.app, cfg)
        self.assertIsInstance(text, str)
        self.assertIn("F8", text)

        off = dict(cfg, enabled=False)
        text2 = main_mod.HD2BlacklistApp.apply_hotkey_config(self.app, off)
        self.assertIn("停用", text2)
        self.assertIsNone(self.app.chat_hotkey)


# ==========================================================================
@unittest.skipUnless(TK_OK, "当前会话没有可用桌面")
class TestHotkeyDialog(TempCase):
    def setUp(self):
        super().setUp()
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.cfg = HotkeyConfig(self.path("hotkey.json"))

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:                            # noqa: BLE001
            pass
        super().tearDown()

    class FakeEvent:
        def __init__(self, keysym):
            self.keysym = keysym

    def _dialog(self, mods=None, on_saved=None):
        """构造对话框（不进入 wait_window）。"""
        import tkinter as tk
        from app.ui.hotkey_dialog import HotkeyDialog
        dlg = HotkeyDialog.__new__(HotkeyDialog)
        dlg.cfg = self.cfg
        dlg.on_saved = on_saved
        dlg.result = None
        dlg.log = hkd.get_logger("hotkey")
        cur = self.cfg.get()
        dlg.vk_var = tk.IntVar(value=cur["vk"])
        dlg.ctrl_var = tk.BooleanVar(value=cur["ctrl"])
        dlg.alt_var = tk.BooleanVar(value=cur["alt"])
        dlg.shift_var = tk.BooleanVar(value=cur["shift"])
        dlg.enabled_var = tk.BooleanVar(value=cur["enabled"])
        dlg.key_text = tk.StringVar()
        dlg.status_var = tk.StringVar()
        dlg.top = tk.Toplevel(self.root)
        dlg.top.withdraw()
        dlg._build()
        dlg._refresh_key_text()
        if mods is not None:
            hkd._live_modifiers = lambda: mods
        return dlg

    def test_build_and_show_current(self):
        dlg = self._dialog()
        try:
            self.assertEqual(dlg.key_text.get(), "F8（未启用）")
        finally:
            dlg.close()

    def test_capture_function_key(self):
        dlg = self._dialog(mods={"ctrl": False, "alt": False,
                                 "shift": False})
        try:
            dlg._on_key(self.FakeEvent("F9"))
            self.assertEqual(dlg.vk_var.get(), 0x78)
            self.assertIn("F9", dlg.key_text.get())
        finally:
            dlg.close()

    def test_capture_letter_without_modifiers(self):
        dlg = self._dialog(mods={"ctrl": False, "alt": False,
                                 "shift": False})
        try:
            dlg._on_key(self.FakeEvent("s"))
            self.assertEqual(dlg.vk_var.get(), ord("S"))
            self.assertEqual(dlg.ctrl_var.get(), False)
        finally:
            dlg.close()

    def test_capture_with_ctrl(self):
        dlg = self._dialog(mods={"ctrl": True, "alt": False, "shift": False})
        try:
            dlg._on_key(self.FakeEvent("F8"))
            self.assertTrue(dlg.ctrl_var.get())
            self.assertEqual(dlg.key_text.get(), "Ctrl+F8（未启用）")
        finally:
            dlg.close()

    def test_capture_with_ctrl_alt_shift(self):
        dlg = self._dialog(mods={"ctrl": True, "alt": True, "shift": True})
        try:
            dlg._on_key(self.FakeEvent("K"))
            self.assertEqual(dlg.key_text.get(), "Ctrl+Alt+Shift+K（未启用）")
        finally:
            dlg.close()

    def test_modifier_only_press_does_not_change_main_key(self):
        dlg = self._dialog(mods={"ctrl": True, "alt": False, "shift": False})
        try:
            before = dlg.vk_var.get()
            dlg._on_key(self.FakeEvent("Control_L"))
            self.assertEqual(dlg.vk_var.get(), before)   # 主键没变
            self.assertTrue(dlg.ctrl_var.get())
        finally:
            dlg.close()

    def test_escape_is_rejected(self):
        dlg = self._dialog(mods={"ctrl": False, "alt": False,
                                 "shift": False})
        try:
            before = dlg.vk_var.get()
            dlg._on_key(self.FakeEvent("Escape"))
            self.assertEqual(dlg.vk_var.get(), before)
            self.assertIn("取消", dlg.status_var.get())
        finally:
            dlg.close()

    def test_unsupported_key_reports(self):
        dlg = self._dialog(mods={"ctrl": False, "alt": False,
                                 "shift": False})
        try:
            dlg._on_key(self.FakeEvent("Super_L"))
            self.assertIn("不支持", dlg.status_var.get())
        finally:
            dlg.close()

    def test_restore_default(self):
        dlg = self._dialog(mods={"ctrl": False, "alt": False,
                                 "shift": False})
        try:
            dlg._on_key(self.FakeEvent("K"))
            dlg.restore_default()
            self.assertEqual(dlg.vk_var.get(), config.CHAT_SCAN_HOTKEY_VK)
            self.assertFalse(dlg.ctrl_var.get())
            self.assertFalse(dlg.alt_var.get())
            self.assertFalse(dlg.shift_var.get())
        finally:
            dlg.close()

    def test_save_persists_and_calls_back(self):
        seen = []
        dlg = self._dialog(mods={"ctrl": True, "alt": False,
                                 "shift": False}, on_saved=seen.append)
        try:
            dlg._on_key(self.FakeEvent("F9"))
            dlg.enabled_var.set(True)
            dlg.save()
            self.assertEqual(self.cfg.vk, 0x78)
            self.assertTrue(self.cfg.get()["ctrl"])
            self.assertTrue(self.cfg.enabled)
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0]["name"], "Ctrl+F9")
        finally:
            dlg.close()

    def test_save_rejects_escape_even_if_forced(self):
        """即使有人绕过捕获直接塞 ESC，保存也要被拦下。"""
        import tkinter.messagebox as mb
        dlg = self._dialog(mods={"ctrl": False, "alt": False,
                                 "shift": False})
        calls = []
        orig = mb.showwarning
        mb.showwarning = lambda *a, **k: calls.append(a)
        try:
            dlg.vk_var.set(0x1B)
            dlg.save()
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.cfg.vk, config.CHAT_SCAN_HOTKEY_VK)
        finally:
            mb.showwarning = orig
            dlg.close()

    def test_key_text_reflects_enabled_state(self):
        dlg = self._dialog()
        try:
            dlg.enabled_var.set(True)
            dlg._refresh_key_text()
            self.assertEqual(dlg.key_text.get(), "F8")
            dlg.enabled_var.set(False)
            dlg._refresh_key_text()
            self.assertIn("未启用", dlg.key_text.get())
        finally:
            dlg.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

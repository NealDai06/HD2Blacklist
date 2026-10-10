# -*- coding: utf-8 -*-
"""single_instance.py 测试：互斥体 + 已有窗口唤醒 + 启动入口接线。

运行： python -m unittest tests.test_single -v
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import config  # noqa: E402
from app import application as main_mod  # noqa: E402
from app.core import single_instance as si  # noqa: E402


def _unique_name() -> str:
    """每次测试用不同的互斥体名，避免和真正运行中的程序/其它测试抢。"""
    return f"Local\\hd2_test_{os.getpid()}_{time.time_ns()}"


# ==========================================================================
class TestMutex(unittest.TestCase):
    def test_first_acquire_ok(self):
        inst = si.SingleInstance(_unique_name())
        try:
            self.assertTrue(inst.acquire())
            self.assertTrue(inst.held)
        finally:
            inst.release()
        self.assertFalse(inst.held)

    def test_second_acquire_is_refused(self):
        """同一个名字第二次拿不到 —— 这就是「只能开一个」的核心。"""
        name = _unique_name()
        first = si.SingleInstance(name)
        second = si.SingleInstance(name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            self.assertFalse(second.held)       # 没抢到就不能持有
            self.assertTrue(first.held)         # 第一个不受影响
        finally:
            first.release()
            second.release()

    def test_released_mutex_can_be_taken_again(self):
        """第一个进程退出（释放句柄）后，必须还能正常启动。"""
        name = _unique_name()
        first = si.SingleInstance(name)
        self.assertTrue(first.acquire())
        first.release()

        second = si.SingleInstance(name)
        try:
            self.assertTrue(second.acquire())
        finally:
            second.release()

    def test_non_windows_is_unrestricted(self):
        with mock.patch.object(si, "_libs", return_value=(None, None)):
            inst = si.SingleInstance(_unique_name())
            self.assertTrue(inst.acquire())
            self.assertFalse(inst.held)         # 没真的持有句柄
            inst.release()                      # 不应抛异常

    def test_release_is_idempotent(self):
        inst = si.SingleInstance(_unique_name())
        inst.acquire()
        inst.release()
        inst.release()                          # 再释放一次也不炸

    def test_uses_local_namespace(self):
        self.assertTrue(si.MUTEX_NAME.startswith("Local\\"))


# ==========================================================================
class _FakeUser32:
    def __init__(self, hwnd=0x1234, iconic=False):
        self.hwnd = hwnd
        self.iconic = iconic
        self.calls = []

    def FindWindowW(self, cls, title):
        self.calls.append(("find", cls, title))
        return self.hwnd

    def IsIconic(self, hwnd):
        self.calls.append(("iconic", hwnd))
        return self.iconic

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("show", hwnd, cmd))
        return True

    def BringWindowToTop(self, hwnd):
        self.calls.append(("top", hwnd))
        return True

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("fg", hwnd))
        return True


class TestBringToFront(unittest.TestCase):
    def test_finds_and_shows_window(self):
        fake = _FakeUser32()
        with mock.patch.object(si, "_libs", return_value=(None, fake)):
            self.assertTrue(si.bring_to_front())
        kinds = [c[0] for c in fake.calls]
        self.assertEqual(kinds, ["find", "iconic", "show", "top", "fg"])
        self.assertEqual(fake.calls[2], ("show", 0x1234, si.SW_SHOW))
        self.assertEqual(fake.calls[0][2], config.WINDOW_TITLE)

    def test_minimized_window_is_restored(self):
        fake = _FakeUser32(iconic=True)
        with mock.patch.object(si, "_libs", return_value=(None, fake)):
            self.assertTrue(si.bring_to_front())
        self.assertIn(("show", 0x1234, si.SW_RESTORE), fake.calls)

    def test_missing_window_returns_false(self):
        fake = _FakeUser32(hwnd=0)
        with mock.patch.object(si, "_libs", return_value=(None, fake)):
            self.assertFalse(si.bring_to_front())
        self.assertEqual([c[0] for c in fake.calls], ["find"])

    def test_no_user32_returns_false(self):
        with mock.patch.object(si, "_libs", return_value=(None, None)):
            self.assertFalse(si.bring_to_front())

    def test_exceptions_are_swallowed(self):
        class Boom(_FakeUser32):
            def ShowWindow(self, hwnd, cmd):
                raise OSError("boom")

        with mock.patch.object(si, "_libs", return_value=(None, Boom())):
            self.assertFalse(si.bring_to_front())


class TestNotifyExistingInstance(unittest.TestCase):
    def test_raised_window_is_enough(self):
        with mock.patch.object(si, "bring_to_front", return_value=True), \
                mock.patch.object(si, "_dialog") as dlg:
            self.assertEqual(si.notify_existing_instance(), "raised")
        dlg.assert_not_called()                 # 窗口都叫出来了，不必再弹框

    def test_falls_back_to_dialog(self):
        with mock.patch.object(si, "bring_to_front", return_value=False), \
                mock.patch.object(si, "_dialog") as dlg:
            self.assertEqual(si.notify_existing_instance(), "dialog")
        dlg.assert_called_once()

    def test_dialog_can_be_suppressed(self):
        with mock.patch.object(si, "bring_to_front", return_value=False), \
                mock.patch.object(si, "_dialog") as dlg:
            self.assertEqual(
                si.notify_existing_instance(show_dialog=False), "none")
        dlg.assert_not_called()

    def test_dialog_swallows_tcl_errors(self):
        with mock.patch("tkinter.Tk", side_effect=RuntimeError("no display")):
            si._dialog()                        # 不应抛异常


# ==========================================================================
class _FakeApp:
    def __init__(self, *a, **kw):
        self.started = False
        self.shutdown_called = False

    def start(self):
        self.started = True

    def shutdown(self):
        self.shutdown_called = True


class _FakeSingle:
    """记录 acquire / release 调用顺序。"""

    instances = []

    def __init__(self, *a, **kw):
        self.acquired = None
        self.released = False
        _FakeSingle.instances.append(self)

    def acquire(self):
        self.acquired = True
        return True

    def release(self):
        self.released = True


class _RefusingSingle(_FakeSingle):
    def acquire(self):
        self.acquired = True
        return False


class TestCheckTee(unittest.TestCase):
    """打包成 --noconsole 后 stdout 是 None，自检结果必须落到日志里。"""

    class _Log:
        def __init__(self):
            self.lines = []

        def info(self, msg, *args):
            self.lines.append(msg % args if args else msg)

    def test_lines_go_to_logger(self):
        log = self._Log()
        tee = main_mod._CheckTee(log)
        tee.write("第一行\n第二行\n半行")
        self.assertEqual(log.lines, ["第一行", "第二行"])
        tee.flush()
        self.assertEqual(log.lines[-1], "半行")

    def test_real_stdout_still_gets_text(self):
        import io
        log = self._Log()
        real = io.StringIO()
        tee = main_mod._CheckTee(log, real)
        tee.write("[ OK ] 依赖 numpy\n")
        tee.flush()
        self.assertIn("numpy", real.getvalue())
        self.assertEqual(log.lines, ["[ OK ] 依赖 numpy"])

    def test_broken_real_stdout_is_ignored(self):
        class Boom:
            def write(self, text):
                raise OSError("closed")

            def flush(self):
                raise OSError("closed")

        log = self._Log()
        tee = main_mod._CheckTee(log, Boom())
        tee.write("还能记日志\n")                     # 不应抛异常
        tee.flush()
        self.assertEqual(log.lines, ["还能记日志"])

    def test_logged_check_uses_tee(self):
        with mock.patch.object(main_mod, "run_self_check",
                               return_value=0) as chk, \
                mock.patch.object(main_mod, "_CheckTee") as tee_cls:
            self.assertEqual(main_mod.run_self_check_logged(), 0)
        chk.assert_called_once()
        tee_cls.assert_called_once()
        self.assertTrue(tee_cls.return_value.flush.called)


class TestMainWiring(unittest.TestCase):
    def setUp(self):
        _FakeSingle.instances = []
        p = mock.patch.object(config, "enable_dpi_awareness", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_gui_start_acquires_and_releases(self):
        with mock.patch.object(si, "SingleInstance", _FakeSingle), \
                mock.patch.object(main_mod, "HD2BlacklistApp", _FakeApp), \
                mock.patch.object(si, "notify_existing_instance") as notif:
            self.assertEqual(main_mod.main([]), 0)
        notif.assert_not_called()
        inst = _FakeSingle.instances[-1]
        self.assertTrue(inst.acquired)
        self.assertTrue(inst.released)          # 退出时必须释放

    def test_second_instance_does_not_build_app(self):
        with mock.patch.object(si, "SingleInstance", _RefusingSingle), \
                mock.patch.object(si, "notify_existing_instance",
                                  return_value="raised") as notif, \
                mock.patch.object(main_mod, "HD2BlacklistApp") as app_cls:
            self.assertEqual(main_mod.main([]), 1)
        app_cls.assert_not_called()             # 关键：第二个进程什么都不建
        notif.assert_called_once()
        self.assertTrue(_FakeSingle.instances[-1].acquired)

    def test_check_mode_bypasses_single_instance(self):
        """--check 等诊断命令要能在程序运行时照跑。"""
        with mock.patch.object(si, "SingleInstance") as cls, \
                mock.patch.object(main_mod, "run_self_check",
                                  return_value=0) as chk:
            self.assertEqual(main_mod.main(["--check"]), 0)
        chk.assert_called_once()
        cls.assert_not_called()

    def test_watch_debug_bypasses_single_instance(self):
        with mock.patch.object(si, "SingleInstance") as cls, \
                mock.patch.object(main_mod, "run_watch_debug",
                                  return_value=0) as fn:
            self.assertEqual(main_mod.main(["--watch-debug"]), 0)
        fn.assert_called_once()
        cls.assert_not_called()

    def test_replay_bypasses_single_instance(self):
        with mock.patch.object(si, "SingleInstance") as cls, \
                mock.patch.object(main_mod, "run_replay",
                                  return_value=0) as fn:
            self.assertEqual(main_mod.main(["--replay", "some.log"]), 0)
        fn.assert_called_once_with("some.log")
        cls.assert_not_called()

    def test_window_title_is_shared_constant(self):
        self.assertEqual(config.WINDOW_TITLE,
                         f"{config.APP_NAME} v{config.VERSION}")
        gui_src = open(os.path.join(_ROOT, "app", "ui", "gui.py"),
                       encoding="utf-8").read()
        self.assertIn("config.WINDOW_TITLE", gui_src)


if __name__ == "__main__":
    unittest.main(verbosity=2)

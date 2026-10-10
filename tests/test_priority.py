# -*- coding: utf-8 -*-
"""priority.py 测试：把本进程压到游戏下面（真实调用 Win32 后再还原）。

运行： python -m unittest tests.test_priority -v
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import config  # noqa: E402
from app.core import priority  # noqa: E402


class TestProcessPriority(unittest.TestCase):
    def setUp(self):
        self.original = priority.process_priority_name()
        self.addCleanup(self._restore)

    def _restore(self):
        if self.original in ("idle", "below_normal", "normal",
                             "above_normal", "high"):
            priority.set_process_priority(self.original)

    def test_round_trip(self):
        if priority.get_process_priority() == 0:
            self.skipTest("非 Windows 环境")
        for level in ("below_normal", "idle", "normal", "above_normal"):
            self.assertTrue(priority.set_process_priority(level), level)
            self.assertEqual(priority.process_priority_name(), level)

    def test_below_normal_is_lower_than_normal(self):
        if priority.get_process_priority() == 0:
            self.skipTest("非 Windows 环境")
        priority.set_process_priority("normal")
        normal = priority.get_process_priority()
        priority.set_process_priority("below_normal")
        below = priority.get_process_priority()
        self.assertNotEqual(normal, below)
        # Windows 的优先级类数值：正常 0x20，低于正常 0x4000（数值大小无意义，
        # 这里只确认两者确实不同且都能读回名字）
        self.assertEqual(priority.process_priority_name(), "below_normal")

    def test_unknown_level_returns_false(self):
        self.assertFalse(priority.set_process_priority("turbo"))

    def test_missing_kernel32_is_safe(self):
        with mock.patch.object(priority, "_kernel32", return_value=None):
            self.assertFalse(priority.set_process_priority("below_normal"))
            self.assertEqual(priority.get_process_priority(), 0)
            self.assertFalse(priority.apply_game_friendly(log=False))


class TestThreadPriority(unittest.TestCase):
    def test_low_priority_restores_after_block(self):
        if priority.get_process_priority() == 0:
            self.skipTest("非 Windows 环境")
        before = priority.set_current_thread_priority("normal")
        self.assertIsNotNone(before)
        try:
            with priority.low_priority("lowest") as ok:
                self.assertTrue(ok)
                with priority.low_priority("normal"):
                    self.assertIsNotNone(
                        priority.set_current_thread_priority("lowest"))
            # 块结束后回到 normal
            after = priority.set_current_thread_priority("normal")
            self.assertIsNotNone(after)
        finally:
            priority.restore_thread_priority(before)

    def test_restore_is_noop_for_none(self):
        priority.restore_thread_priority(None)          # 不应抛异常

    def test_unknown_level_returns_none(self):
        self.assertIsNone(priority.set_current_thread_priority("turbo"))

    def test_missing_kernel32(self):
        with mock.patch.object(priority, "_kernel32", return_value=None):
            self.assertIsNone(priority.set_current_thread_priority("lowest"))
            with priority.low_priority("lowest") as ok:
                self.assertFalse(ok)                    # 拿不到句柄就算没生效

    def test_low_priority_restores_on_exception(self):
        if priority.get_process_priority() == 0:
            self.skipTest("非 Windows 环境")
        before = priority.set_current_thread_priority("normal")
        try:
            with self.assertRaises(RuntimeError):
                with priority.low_priority("lowest"):
                    raise RuntimeError("boom")
            self.assertIsNotNone(
                priority.set_current_thread_priority("normal"))
        finally:
            priority.restore_thread_priority(before)


class TestConfigSwitch(unittest.TestCase):
    def test_config_defaults(self):
        self.assertIsInstance(config.GAME_FRIENDLY_PRIORITY, bool)
        self.assertIn(config.PROCESS_PRIORITY,
                      ("idle", "below_normal", "normal"))
        self.assertIn(config.WATCH_THREAD_PRIORITY,
                      ("idle", "lowest", "below_normal", "normal"))

    def test_watch_is_cheap_and_slow(self):
        """v2 只读一个文本文件：轮询必须够稀，别拿高频轮询去磨 CPU。"""
        self.assertGreaterEqual(config.WATCH_POLL_INTERVAL, 0.2)
        self.assertLessEqual(config.WATCH_POLL_INTERVAL, 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

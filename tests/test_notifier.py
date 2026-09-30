# -*- coding: utf-8 -*-
"""阶段 7 验收测试：notifier 渲染 / 模板 / 无焦点 Overlay / 音效。

Overlay 部分会真的创建一个 Win32 分层窗口并截图验证像素，
如果当前会话没有可用桌面（例如纯 SSH），相关用例会自动跳过。

运行： python -m unittest tests.test_notifier -v
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sys
import time
import unittest

from PIL import Image

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.notify import notifier as nt  # noqa: E402
from app import config  # noqa: E402
from app.config import DEFAULT_NOTIFICATION        # noqa: E402
from app.settings.notification_config import NotificationConfig   # noqa: E402

TMP_ROOT = os.path.join(_ROOT, ".test_tmp")

ENTRY = {
    "id": 1, "player_id": "76561198000000001", "player_name": "PlayerX",
    "note": "恶意TK", "tk_count": 3, "encounter_count": 7,
    "last_seen": "2026-01-02 03:04:05",
}


def _has_desktop() -> bool:
    try:
        return bool(nt.user32.GetSystemMetrics(0))
    except Exception:                          # noqa: BLE001
        return False


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

    def cfg(self, **override):
        ncfg = NotificationConfig(self.path("notification.json"))
        if override:
            ncfg.save(override)
        return ncfg


# ==========================================================================
class TestTemplate(unittest.TestCase):
    def test_all_placeholders(self):
        tpl = ("{player_name}|{match_score}|{note}|{tk_count}|{source}"
               "|{last_seen}")
        out = nt.render_template(tpl, ENTRY, 92.5, "chat")
        self.assertEqual(out, "PlayerX|92|恶意TK|3|chat|2026-01-02 03:04:05")

    def test_time_placeholder(self):
        out = nt.render_template("{time}", ENTRY, 100, "chat")
        self.assertRegex(out, r"^\d{2}:\d{2}:\d{2}$")

    def test_unknown_placeholder_kept(self):
        self.assertEqual(nt.render_template("{nope}", ENTRY, 1, "x"), "{nope}")

    def test_missing_values_use_fallbacks(self):
        out = nt.render_template("{note}|{tk_count}|{player_name}",
                                 {}, 0, "")
        self.assertEqual(out, "无|0|未知玩家")

    def test_none_template(self):
        self.assertEqual(nt.render_template(None, ENTRY, 1, "x"), "")

    def test_broken_template_does_not_raise(self):
        self.assertEqual(nt.render_template("{player_name", ENTRY, 1, "x"),
                         "{player_name")


class TestFieldFiltering(unittest.TestCase):
    def test_disabled_field_line_removed(self):
        show = {"note": False, "match_score": True}
        body = "{player_name}\n备注：{note}\n匹配度：{match_score}%"
        self.assertEqual(nt.filter_hidden_lines(body, show),
                         "{player_name}\n匹配度：{match_score}%")

    def test_enabled_fields_kept(self):
        show = {"note": True, "match_score": True}
        body = "备注：{note}\n匹配度：{match_score}%"
        self.assertEqual(nt.filter_hidden_lines(body, show), body)

    def test_lines_without_placeholders_kept(self):
        self.assertEqual(nt.filter_hidden_lines("纯文本", {"note": False}),
                         "纯文本")

    def test_empty_show_fields_keeps_everything(self):
        body = "备注：{note}"
        self.assertEqual(nt.filter_hidden_lines(body, {}), body)


class TestColors(unittest.TestCase):
    def test_hex(self):
        self.assertEqual(nt.hex_to_rgb("#ff5555"), (255, 85, 85))
        self.assertEqual(nt.hex_to_rgb("2b2b2b"), (43, 43, 43))
        self.assertEqual(nt.hex_to_rgb("#abc"), (170, 187, 204))

    def test_invalid_falls_back(self):
        self.assertEqual(nt.hex_to_rgb("nope", (1, 2, 3)), (1, 2, 3))
        self.assertEqual(nt.hex_to_rgb(None, (9, 9, 9)), (9, 9, 9))


# ==========================================================================
class TestRenderOverlay(TempCase):
    def base_cfg(self, **ap):
        cfg = dict(DEFAULT_NOTIFICATION)
        cfg["appearance"] = dict(DEFAULT_NOTIFICATION["appearance"])
        cfg["appearance"].update(ap)
        return cfg

    def test_render_respects_size(self):
        img = nt.render_overlay(self.base_cfg(), ENTRY, 100, "chat", 360, 100)
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.size, (360, 100))

    def test_render_grows_height_for_long_text(self):
        cfg = self.base_cfg()
        cfg["body_template"] = "\n".join(f"行{i}" for i in range(20))
        img = nt.render_overlay(cfg, ENTRY, 100, "chat", 360, 100)
        self.assertGreater(img.height, 100)

    def test_opacity_applied_to_background(self):
        img = nt.render_overlay(self.base_cfg(opacity=0.5), ENTRY, 100, "chat",
                                360, 100)
        # 角落是圆角外的透明区，取中心点检查 alpha
        px = img.getpixel((img.width // 2, img.height - 3))
        self.assertAlmostEqual(px[3], 127, delta=6)

    def test_image_mode_none_leaves_text_at_left(self):
        cfg = self.base_cfg()
        cfg["image_mode"] = "none"
        img = nt.render_overlay(cfg, ENTRY, 100, "chat", 360, 100)
        self.assertEqual(img.size, (360, 100))

    def test_custom_image_missing_falls_back(self):
        cfg = self.base_cfg()
        cfg["image_mode"] = "custom"
        cfg["image_path"] = self.path("not_exists.png")
        img = nt.render_overlay(cfg, ENTRY, 100, "chat", 360, 100)
        self.assertEqual(img.size[0], 360)

    def test_custom_image_used(self):
        p = self.path("icon.png")
        Image.new("RGBA", (32, 32), (0, 255, 0, 255)).save(p)
        cfg = self.base_cfg()
        cfg["image_mode"] = "custom"
        cfg["image_path"] = p
        cfg["image_size"] = [20, 20]
        img = nt.render_overlay(cfg, ENTRY, 100, "chat", 360, 100)
        self.assertEqual(img.size[0], 360)

    def test_chinese_text_renders_visible_pixels(self):
        cfg = self.base_cfg()
        cfg["title_template"] = "黑名单玩家"
        cfg["body_template"] = "备注：恶意TK"
        img = nt.render_overlay(cfg, ENTRY, 100, "chat", 360, 100)
        colors = img.convert("RGB").getcolors(maxcolors=100000)
        self.assertGreater(len(colors), 2)     # 至少背景色 + 文字色

    def test_default_icon_is_generated(self):
        target = self.path("assets", "icon.png")
        out = nt.ensure_default_icon(target)
        self.assertTrue(os.path.exists(out))
        self.assertTrue(out.lower().endswith(".png"))
        with Image.open(out) as im:
            self.assertEqual(im.mode, "RGBA")
        # 第二次调用不覆盖
        before = os.path.getmtime(out)
        nt.ensure_default_icon(target)
        self.assertEqual(before, os.path.getmtime(out))

    def test_image_cache_is_used(self):
        p = self.path("c.png")
        Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(p)
        a = nt.load_image_cached(p)
        b = nt.load_image_cached(p)
        self.assertIs(a, b)

    def test_image_cache_invalidated_on_change(self):
        p = self.path("c2.png")
        Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(p)
        a = nt.load_image_cached(p)
        time.sleep(0.01)
        Image.new("RGBA", (8, 8), (9, 9, 9, 255)).save(p)
        t = os.path.getmtime(p) + 5
        os.utime(p, (t, t))
        b = nt.load_image_cached(p)
        self.assertIsNot(a, b)


# ==========================================================================
@unittest.skipUnless(_has_desktop(), "当前会话没有可用桌面")
class TestOverlayWindow(TempCase):
    def test_window_created_with_no_activate_styles(self):
        w = nt._OverlayWindow()
        try:
            self.assertTrue(w.start(timeout=8.0), "Overlay 窗口创建失败")
            GWL_EXSTYLE = -20
            style = nt.user32.GetWindowLongW(w.hwnd, GWL_EXSTYLE)
            for flag, name in ((nt.WS_EX_NOACTIVATE, "NOACTIVATE"),
                               (nt.WS_EX_TRANSPARENT, "TRANSPARENT"),
                               (nt.WS_EX_TOOLWINDOW, "TOOLWINDOW"),
                               (nt.WS_EX_TOPMOST, "TOPMOST"),
                               (nt.WS_EX_LAYERED, "LAYERED")):
                self.assertTrue(style & flag, f"缺少 {name} 样式")
        finally:
            w.close()

    def test_show_and_hide(self):
        w = nt._OverlayWindow()
        try:
            if not w.start(timeout=8.0):
                self.skipTest("Overlay 不可用")
            img = Image.new("RGBA", (120, 40), (255, 0, 0, 255))
            w.show(img, 10, 10, 5.0)
            time.sleep(0.3)
            self.assertTrue(nt.user32.IsWindowVisible(w.hwnd),
                            "Overlay 应当可见")
            w.hide()
            time.sleep(0.15)
            self.assertFalse(nt.user32.IsWindowVisible(w.hwnd),
                             "hide() 后应当不可见")
        finally:
            w.close()

    def test_auto_hide_after_duration(self):
        w = nt._OverlayWindow()
        try:
            if not w.start(timeout=8.0):
                self.skipTest("Overlay 不可用")
            img = Image.new("RGBA", (120, 40), (0, 0, 255, 255))
            w.show(img, 10, 10, 0.3)
            time.sleep(0.15)
            self.assertTrue(nt.user32.IsWindowVisible(w.hwnd))
            time.sleep(1.0)
            self.assertFalse(nt.user32.IsWindowVisible(w.hwnd),
                             "超过 duration 后应当自动隐藏")
        finally:
            w.close()

    def test_show_does_not_steal_focus(self):
        w = nt._OverlayWindow()
        try:
            if not w.start(timeout=8.0):
                self.skipTest("Overlay 不可用")
            before = nt.user32.GetForegroundWindow()
            img = Image.new("RGBA", (120, 40), (0, 255, 0, 255))
            w.show(img, 20, 20, 0.5)
            time.sleep(0.3)
            after = nt.user32.GetForegroundWindow()
            self.assertEqual(before, after, "提示窗口抢走了前台焦点！")
        finally:
            w.close()

    def test_wndproc_is_process_wide_and_never_recreated(self):
        """回归：窗口过程必须是进程级常驻对象。

        曾经的 bug：每个 _OverlayWindow 实例各建一个 WNDPROC，而
        `RegisterClassW` 的窗口类在进程内是全局永久的（二次注册返回 1410），
        `CreateWindowExW` 用的仍是**首次**那个回调指针 ——
        实例被 GC 后 Windows 调用已释放地址，进程以 0xC000041D 原生崩溃。
        """
        saved = (nt._overlay_wndproc, nt._overlay_class_registered)
        try:
            first = nt._overlay_wndproc
            calls = []
            real = nt.user32.RegisterClassW

            def counting(ptr):
                calls.append(1)
                return real(ptr)

            nt._overlay_wndproc = first            # 保持旧回调存活（saved 里也有）
            nt._overlay_class_registered = False   # 强制走一次注册分支
            nt.user32.RegisterClassW = counting
            try:
                hinst = nt.kernel32.GetModuleHandleW(None)
                proc = nt._ensure_overlay_class(hinst)
                again = nt._ensure_overlay_class(hinst)
            finally:
                nt.user32.RegisterClassW = real
            self.assertEqual(len(calls), 1, "窗口类只应注册一次")
            self.assertIs(proc, again)
            self.assertIs(proc, first, "不能重建回调对象（会留下野指针）")
            self.assertIs(nt._overlay_wndproc, first)
        finally:
            nt._overlay_wndproc, nt._overlay_class_registered = saved

    def test_overlay_class_name_is_stable(self):
        self.assertEqual(nt.OVERLAY_CLASS_NAME, "HD2BlacklistOverlayWnd")
        src = open(nt.__file__, encoding="utf-8").read()
        # CreateWindowExW 必须用同一个类名常量，不能再写字面量
        self.assertIn("ex_style, OVERLAY_CLASS_NAME", src)

    def test_each_stack_slot_gets_its_own_window(self):
        """回归：每个通知栏必须有自己的窗口。

        真 bug：所有通知栏之前共用一个 WS_EX_LAYERED 窗口，而
        `UpdateLayeredWindow` 会把**整个窗口**重绘成新图并搬走它 ——
        后画的把先画的整个盖掉，用户只看得见最后一条。
        配合"只播一次音效"，现象就是"命中多个黑名单玩家却只播报一个人"。
        """
        w = nt._OverlayWindow()
        try:
            if not w.start(timeout=8.0):
                self.skipTest("Overlay 不可用")
            for i in range(3):
                w.show(Image.new("RGBA", (200, 60), (10, 10, 10, 255)),
                       100, 40 + i * 70, 5.0, slot=i)
            time.sleep(0.6)
            self.assertEqual(len(w._surfaces), 3, "3 个槽位应有 3 个窗口")
            hwnds = [s.hwnd for s in w._surfaces]
            self.assertTrue(all(hwnds), "窗口句柄不能为空")
            self.assertEqual(len(set(hwnds)), 3, "槽位之间不能共用窗口")
            for s in w._surfaces:
                self.assertTrue(nt.user32.IsWindowVisible(s.hwnd),
                                f"第 {s.index} 个通知栏应当可见")
            w.hide()
            time.sleep(0.2)
            for s in w._surfaces:
                self.assertFalse(nt.user32.IsWindowVisible(s.hwnd),
                                 "hide() 应当收起所有通知栏")
        finally:
            w.close()

    def test_slot_beyond_limit_is_not_created(self):
        w = nt._OverlayWindow()
        try:
            if not w.start(timeout=8.0):
                self.skipTest("Overlay 不可用")
            w.show(Image.new("RGBA", (120, 40), (0, 0, 0, 255)),
                   10, 10, 3.0, slot=nt.MAX_NOTIFY_STACK + 3)
            time.sleep(0.4)
            self.assertLessEqual(len(w._surfaces), nt.MAX_NOTIFY_STACK)
        finally:
            w.close()


# ==========================================================================
class TestNotifier(TempCase):
    def test_alert_without_overlay_falls_back(self):
        n = nt.Notifier(self.cfg())
        try:
            n.toast_enabled = False           # 测试中不要真的弹系统通知
            n._overlay.available = False
            ok = n.alert(ENTRY, 100.0, "chat")
            self.assertFalse(ok)               # Overlay 不可用 → 走系统通知
        finally:
            n.close()

    def test_alert_does_not_raise_on_bad_config(self):
        n = nt.Notifier(self.cfg())
        try:
            n.toast_enabled = False
            n.cfg.save({"appearance": {"width": "abc", "height": None,
                                       "opacity": "xx"},
                        "image_mode": "custom", "image_path": "Z:\\nope.png",
                        "image_size": "bad"})
            n.alert(ENTRY, 100.0, "chat")      # 不应抛异常
        finally:
            n.close()

    def test_alert_plays_sound_and_shows(self):
        n = nt.Notifier(self.cfg())
        try:
            n.toast_enabled = False
            played = []
            n.sound = type("S", (), {"play": lambda self, c: played.append(c),
                                     "stop": lambda self: None})()
            n.alert(ENTRY, 100.0, "chat")
            self.assertEqual(len(played), 1)
            self.assertTrue(played[0].get("enabled"))
        finally:
            n.close()

    def test_render_preview_image(self):
        n = nt.Notifier(self.cfg())
        try:
            img = n.render_preview_image(n.cfg.get(), ENTRY, 88.0, "preview")
            self.assertEqual(img.mode, "RGBA")
        finally:
            n.close()

    def test_target_position_all_modes(self):
        n = nt.Notifier(self.cfg())
        try:
            for pos in ("top_left", "top_center", "top_right",
                        "bottom_left", "bottom_center", "bottom_right",
                        "center", "garbage"):
                cfg = {"appearance": {"position": pos}}
                x, y = n._target_position(cfg, 300, 100)
                self.assertIsInstance(x, int)
                self.assertIsInstance(y, int)
        finally:
            n.close()


class TestSound(TempCase):
    def test_beep_does_not_block(self):
        s = nt.SoundPlayer()
        t0 = time.time()
        s.play({"enabled": True, "mode": "beep",
                "beep_freq": 1200, "beep_duration": 150})
        self.assertLess(time.time() - t0, 0.05)     # 立刻返回

    def test_disabled_and_none_modes(self):
        s = nt.SoundPlayer()
        s.play({"enabled": False, "mode": "beep"})
        s.play({"enabled": True, "mode": "none"})
        s.play({})
        s.play(None)

    def test_missing_wav_is_silent(self):
        s = nt.SoundPlayer()
        s._play_sync({"path": self.path("nope.wav")}, "wav")   # 不抛异常

    def test_invalid_beep_params_clamped(self):
        s = nt.SoundPlayer()
        s._play_sync({"beep_freq": 999999, "beep_duration": 5}, "beep")


class TestAlertBatch(TempCase):
    """改进点三：多栏通知 + 只播一次音效 + 垂直堆叠。"""

    class FakeOverlay:
        def __init__(self):
            self.available = True
            self.shown = []
            self.slots = []          # 每个通知栏落在哪个堆叠槽位

        def show(self, image, x, y, duration, slot=0):
            self.shown.append((image.size, x, y, duration))
            self.slots.append(slot)

        def hide(self):
            pass

        def close(self):
            pass

    class FakeSound:
        def __init__(self):
            self.played = []

        def play(self, cfg):
            self.played.append(cfg)

        def stop(self):
            pass

    def _notifier(self, **override):
        n = nt.Notifier(self.cfg(**override))
        n.toast_enabled = False
        n._overlay = self.FakeOverlay()
        n.sound = self.FakeSound()
        return n

    @staticmethod
    def _entries(count):
        return [{"id": i, "player_id": f"id{i}", "player_name": f"Player{i}",
                 "note": "TK", "tk_count": 1, "last_seen": None}
                for i in range(1, count + 1)]

    # ---- 音效次数 ----
    def test_batch_of_three_plays_sound_once(self):
        n = self._notifier()
        try:
            hits = [(e, 100.0, "chat_manual") for e in self._entries(3)]
            shown = n.alert_batch(hits)
            self.assertEqual(shown, 3)                       # 3 个通知栏
            self.assertEqual(len(n._overlay.shown), 3)
            self.assertEqual(len(n.sound.played), 1)         # 只播一次音效
        finally:
            n.close()

    def test_batch_of_two_plays_sound_once(self):
        n = self._notifier()
        try:
            hits = [(e, 100.0, "chat_manual") for e in self._entries(2)]
            n.alert_batch(hits)
            self.assertEqual(len(n._overlay.shown), 2)
            self.assertEqual(len(n.sound.played), 1)
        finally:
            n.close()

    def test_batch_of_one_matches_single_alert(self):
        n = self._notifier()
        try:
            hits = [(e, 100.0, "chat_manual") for e in self._entries(1)]
            n.alert_batch(hits)
            self.assertEqual(len(n._overlay.shown), 1)
            self.assertEqual(len(n.sound.played), 1)
        finally:
            n.close()

    def test_single_alert_still_plays_once(self):
        n = self._notifier()
        try:
            n.alert(self._entries(1)[0], 100.0, "chat")
            self.assertEqual(len(n._overlay.shown), 1)
            self.assertEqual(len(n.sound.played), 1)
        finally:
            n.close()

    def test_empty_batch_does_nothing(self):
        n = self._notifier()
        try:
            self.assertEqual(n.alert_batch([]), 0)
            self.assertEqual(n.alert_batch(None), 0)
            self.assertEqual(n.sound.played, [])
            self.assertEqual(n._overlay.shown, [])
        finally:
            n.close()

    def test_show_overlay_play_sound_false_is_silent(self):
        n = self._notifier()
        try:
            n._show_overlay(self._entries(1)[0], 100.0, "chat",
                            play_sound=False, stack_index=0)
            self.assertEqual(len(n._overlay.shown), 1)
            self.assertEqual(n.sound.played, [])
        finally:
            n.close()

    # ---- 堆叠 ----
    def test_batch_stacks_vertically_without_overlap(self):
        n = self._notifier(appearance={"position": "top_right",
                                       "height": 100, "width": 360})
        try:
            hits = [(e, 100.0, "chat") for e in self._entries(3)]
            n.alert_batch(hits)
            boxes = [(y, size[1]) for size, _x, y, _d in n._overlay.shown]
            self.assertEqual(len(boxes), 3)
            for i in range(len(boxes) - 1):
                y, h = boxes[i]
                next_y, _ = boxes[i + 1]
                self.assertGreaterEqual(next_y, y + h, "通知栏之间不能重叠")
            self.assertLess(boxes[0][0], boxes[1][0])        # 向下依次排开
            self.assertLess(boxes[1][0], boxes[2][0])
        finally:
            n.close()

    def test_stack_gap_is_respected(self):
        n = self._notifier(appearance={"position": "top_right",
                                       "height": 100, "width": 360})
        try:
            n.alert_batch([(e, 100.0, "chat") for e in self._entries(2)])
            y0 = n._overlay.shown[0][2]
            y1 = n._overlay.shown[1][2]
            self.assertEqual(y1 - y0, 100 + nt.NOTIFY_STACK_GAP)
        finally:
            n.close()

    def test_stack_index_zero_is_base_position(self):
        n = self._notifier(appearance={"position": "top_right"})
        try:
            cfg = n.cfg.get()
            base = n._target_position(cfg, 360, 100)
            self.assertEqual(
                n._stacked_position(cfg, base[0], base[1], 360, 100, 0), base)
        finally:
            n.close()

    def test_bottom_position_stacks_upwards(self):
        n = self._notifier(appearance={"position": "bottom_right",
                                       "height": 100, "width": 360})
        try:
            cfg = n.cfg.get()
            _x, y = n._target_position(cfg, 360, 100)
            pos0 = n._stacked_position(cfg, 100, y, 360, 100, 0)
            pos1 = n._stacked_position(cfg, 100, y, 360, 100, 1)
            pos2 = n._stacked_position(cfg, 100, y, 360, 100, 2)
            self.assertEqual(pos0[1], y)
            self.assertLess(pos1[1], pos0[1])          # 向上
            self.assertLess(pos2[1], pos1[1])
        finally:
            n.close()

    def test_offscreen_stack_is_skipped(self):
        n = self._notifier()
        try:
            cfg = {"appearance": {"position": "top_right", "monitor": 0}}
            # 高到一屏放不下 → 应当返回 None（跳过绘制）
            self.assertIsNone(
                n._stacked_position(cfg, 100, 40, 360, 100000, 1))
        finally:
            n.close()

    def test_offscreen_stack_does_not_reach_overlay(self):
        n = self._notifier()
        try:
            hits = [(e, 100.0, "chat") for e in self._entries(3)]
            n._stacked_position = lambda cfg, x, y, w, h, i: (
                (x, y) if i == 0 else None)
            shown = n.alert_batch(hits)
            self.assertEqual(shown, 1)                 # 只有第 0 个画出来了
            self.assertEqual(len(n.sound.played), 1)   # 音效照样只播一次
        finally:
            n.close()

    # ---- 上限 ----
    def test_max_stack_limit(self):
        from app import config as cfg_mod
        n = self._notifier()
        try:
            hits = [(e, 100.0, "chat") for e in self._entries(9)]
            shown = n.alert_batch(hits)
            self.assertEqual(shown, cfg_mod.MAX_NOTIFY_STACK)
            self.assertLessEqual(len(n._overlay.shown), cfg_mod.MAX_NOTIFY_STACK)
            self.assertEqual(len(n.sound.played), 1)
        finally:
            n.close()

    def test_batch_renders_each_player_separately(self):
        """每个通知栏渲染的是各自的玩家，不能串号。"""
        n = self._notifier()
        try:
            hits = [(e, 100.0, "chat") for e in self._entries(2)]
            n.alert_batch(hits)
            self.assertEqual(len(n._overlay.shown), 2)
            img1 = n.render_preview_image(n.cfg.get(), self._entries(1)[0],
                                          100.0, "chat")
            img2 = n.render_preview_image(n.cfg.get(), self._entries(2)[1],
                                          100.0, "chat")
            self.assertNotEqual(img1.tobytes(), img2.tobytes())
        finally:
            n.close()

    def test_overflow_is_summarised_in_the_last_slot(self):
        """命中数超过堆叠上限时，最后一个槽位换成汇总栏列出剩余玩家。

        没有这条，用户只知道"命中了"，却不知道还有谁没显示出来。
        """
        from app import config as cfg_mod
        limit = cfg_mod.MAX_NOTIFY_STACK
        n = self._notifier()
        try:
            calls = []
            real = n._show_overlay

            def spy(entry, score, source, play_sound=True, stack_index=0):
                calls.append((entry, stack_index))
                return real(entry, score, source, play_sound, stack_index)

            n._show_overlay = spy
            total = limit + 2
            hits = [(e, 100.0, "chat") for e in self._entries(total)]
            shown = n.alert_batch(hits)

            self.assertEqual(shown, limit)
            self.assertEqual([c[1] for c in calls], list(range(limit)))
            names = [c[0]["player_name"] for c in calls]
            self.assertEqual(names[:limit - 1],
                             [f"Player{i}" for i in range(1, limit)])
            summary = names[-1]
            self.assertIn(f"等 {total - (limit - 1)} 名", summary)
            for i in range(limit, total + 1):
                self.assertIn(f"Player{i}", summary)
            self.assertEqual(len(n.sound.played), 1)
        finally:
            n.close()

    def test_no_summary_when_everything_fits(self):
        """刚好占满堆叠上限时不应出现汇总栏（每个玩家都有自己的栏）。"""
        from app import config as cfg_mod
        limit = cfg_mod.MAX_NOTIFY_STACK
        n = self._notifier()
        try:
            calls = []
            real = n._show_overlay

            def spy(entry, score, source, play_sound=True, stack_index=0):
                calls.append(entry["player_name"])
                return real(entry, score, source, play_sound, stack_index)

            n._show_overlay = spy
            hits = [(e, 100.0, "chat") for e in self._entries(limit)]
            self.assertEqual(n.alert_batch(hits), limit)
            self.assertEqual(calls, [f"Player{i}" for i in range(1, limit + 1)])
        finally:
            n.close()


class TestFontSize(TempCase):
    """消息文字大小可编辑（appearance.title_font_size / body_font_size）。"""

    ENTRY = {"player_name": "PlayerX", "player_id": "1", "note": "恶意TK",
             "tk_count": 2, "last_seen": "2026-01-02 03:04:05"}

    def cfg(self, **ap):
        c = dict(DEFAULT_NOTIFICATION)
        c["appearance"] = dict(DEFAULT_NOTIFICATION["appearance"])
        c["appearance"].update(ap)
        return c

    @staticmethod
    def _ink_pixels(img):
        """统计非背景像素（近似"文字墨迹"数量）。"""
        from collections import Counter
        rgb = img.convert("RGB")
        counts = Counter(rgb.getdata())
        bg = counts.most_common(1)[0][0]
        return sum(n for color, n in counts.items() if color != bg)

    # ---- clamp ----
    def test_clamp_accepts_valid(self):
        self.assertEqual(nt.clamp_font_size(20, 13), 20)
        self.assertEqual(nt.clamp_font_size("24", 13), 24)
        self.assertEqual(nt.clamp_font_size(18.6, 13), 19)

    def test_clamp_falls_back_on_garbage(self):
        self.assertEqual(nt.clamp_font_size("abc", 16), 16)
        self.assertEqual(nt.clamp_font_size(None, 13), 13)
        self.assertEqual(nt.clamp_font_size("", 13), 13)

    def test_clamp_bounds(self):
        self.assertEqual(nt.clamp_font_size(1, 13), config.FONT_SIZE_MIN)
        self.assertEqual(nt.clamp_font_size(9999, 13), config.FONT_SIZE_MAX)
        self.assertEqual(nt.clamp_font_size(0, 13), config.FONT_SIZE_MIN)

    # ---- 渲染 ----
    def test_larger_font_grows_height(self):
        small = nt.render_overlay(self.cfg(title_font_size=13,
                                           body_font_size=11),
                                  self.ENTRY, 94.0, "chat", 360, 100)
        large = nt.render_overlay(self.cfg(title_font_size=30,
                                           body_font_size=24),
                                  self.ENTRY, 94.0, "chat", 360, 100)
        self.assertGreater(large.height, small.height)

    def test_larger_font_draws_more_ink(self):
        small = nt.render_overlay(self.cfg(title_font_size=12,
                                           body_font_size=10),
                                  self.ENTRY, 94.0, "chat", 500, 100)
        large = nt.render_overlay(self.cfg(title_font_size=24,
                                           body_font_size=20),
                                  self.ENTRY, 94.0, "chat", 500, 100)
        self.assertGreater(self._ink_pixels(large), self._ink_pixels(small))

    def test_default_font_size_used_when_missing(self):
        """老配置文件没有这两个字段时，用默认字号且不报错。"""
        c = dict(DEFAULT_NOTIFICATION)
        c["appearance"] = dict(DEFAULT_NOTIFICATION["appearance"])
        c["appearance"].pop("title_font_size")
        c["appearance"].pop("body_font_size")
        img = nt.render_overlay(c, self.ENTRY, 94.0, "chat", 360, 100)
        self.assertEqual(img.size[0], 360)

    def test_invalid_font_size_falls_back(self):
        img = nt.render_overlay(
            self.cfg(title_font_size="abc", body_font_size="xx"),
            self.ENTRY, 94.0, "chat", 360, 100)
        baseline = nt.render_overlay(self.cfg(), self.ENTRY, 94.0, "chat",
                                     360, 100)
        self.assertEqual(img.size, baseline.size)

    def test_extreme_font_size_is_clamped(self):
        img = nt.render_overlay(
            self.cfg(title_font_size=99999, body_font_size=99999),
            self.ENTRY, 94.0, "chat", 360, 100)
        # 被夹到 FONT_SIZE_MAX，不会真的画出 99999 像素的字
        max_img = nt.render_overlay(
            self.cfg(title_font_size=config.FONT_SIZE_MAX,
                     body_font_size=config.FONT_SIZE_MAX),
            self.ENTRY, 94.0, "chat", 360, 100)
        self.assertEqual(img.size, max_img.size)

    def test_title_and_body_are_independent(self):
        big_title = nt.render_overlay(
            self.cfg(title_font_size=32, body_font_size=11),
            self.ENTRY, 94.0, "chat", 500, 100)
        big_body = nt.render_overlay(
            self.cfg(title_font_size=13, body_font_size=32),
            self.ENTRY, 94.0, "chat", 500, 100)
        self.assertNotEqual(big_title.size, big_body.size)

    def test_large_font_wraps_instead_of_overflowing(self):
        """正文字号很大时靠换行 + 自动增高消化，不会横向溢出。"""
        img = nt.render_overlay(
            self.cfg(title_font_size=16, body_font_size=28),
            self.ENTRY, 94.0, "chat", 300, 100)
        self.assertEqual(img.width, 300)          # 宽度不变
        self.assertGreater(img.height, 100)       # 高度自动增加

    def test_long_text_with_large_font_grows_a_lot(self):
        c = self.cfg(body_font_size=24)
        c["body_template"] = "这是一段很长的备注文字，" * 5
        img = nt.render_overlay(c, self.ENTRY, 94.0, "chat", 360, 100)
        self.assertGreater(img.height, 200)

    def test_font_sizes_are_cached(self):
        """相同字号只加载一次字体对象。"""
        f1 = nt.load_font(21)
        f2 = nt.load_font(21)
        self.assertIs(f1, f2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

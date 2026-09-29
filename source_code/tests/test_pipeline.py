# -*- coding: utf-8 -*-
"""阶段 4-5 验收测试：capture / ocr(preprocess) / scan_session / scheduler /
chat_scanner / esc_trigger —— 全部使用假 capture / 假 OCR，不依赖真实屏幕。

运行： python -m unittest tests.test_pipeline -v
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

from PIL import Image

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config                                   # noqa: E402
import esc_trigger as esc_mod                   # noqa: E402
import scan_scheduler as sched_mod              # noqa: E402
from chat_scanner import ChatScanner            # noqa: E402
from config import COLD_START_SESSION, ESC_SESSION   # noqa: E402
from database import BlacklistDB                # noqa: E402
from esc_trigger import EscTrigger              # noqa: E402
from matcher import Matcher                     # noqa: E402
from ocr_engine import OCREngine                # noqa: E402
from region_config import RegionConfig          # noqa: E402
from scan_scheduler import ScanScheduler        # noqa: E402
from scan_session import (ScanSession, evaluate_scan, group_into_lines,   # noqa: E402
                          is_valid_player_name)

TMP_ROOT = os.path.join(_ROOT, ".test_tmp")


# ==========================================================================
# 测试替身
# ==========================================================================
class FakeCapture:
    def __init__(self, images=None, std=0.0):
        self.images = list(images or [])
        self.std = std
        self.grab_calls = 0
        self.default = Image.new("RGB", (40, 20), (10, 10, 10))

    def grab(self, region_key):
        self.grab_calls += 1
        if self.images:
            return self.images.pop(0)
        return self.default

    def region_std(self, img):
        return self.std

    def clamp_region(self, r):
        return r

    def virtual_screen(self):
        return {"left": 0, "top": 0, "width": 1920, "height": 1080}


class FakeOCR:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.default = []
        self.calls = 0

    def recognize_raw(self, img):
        self.calls += 1
        if self.results:
            return self.results.pop(0)
        return list(self.default)

    def recognize_text(self, img, min_conf=0.0):
        return " ".join(t for t, c in self.recognize_raw(img) if c >= min_conf)

    def preprocess(self, img):
        return OCREngine().preprocess(img)


class FakeNotifier:
    """记录 alert / alert_batch 调用。

    batches 的每一项对应一次 alert_batch —— 用于验证
    「一次扫描命中 N 个玩家 → 只调用一次批量接口（= 只播一次音效）」。
    """

    def __init__(self):
        self.alerts = []          # 逐条 alert（兼容旧用例）
        self.batches = []         # alert_batch 的入参

    def alert(self, entry, score, source):
        self.alerts.append((entry.get("player_name"), score, source))
        return True

    def alert_batch(self, hits):
        hits = list(hits)
        self.batches.append(hits)
        for entry, score, source in hits:
            self.alerts.append((entry.get("player_name"), score, source))
        return len(hits)


class FakeSchedulerForEsc:
    def __init__(self):
        self.game_active = threading.Event()
        self.started = []
        self.stopped = 0

    def stop_session(self, wait=True):
        self.stopped += 1

    def start_session(self, region_key, source, params):
        self.started.append((region_key, source, params))
        return object()


# ==========================================================================
class TempCase(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.tmp = os.path.join(TMP_ROOT, self._testMethodName)
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)
        self._evidence_backup = sched_mod.EVIDENCE_DIR
        sched_mod.EVIDENCE_DIR = os.path.join(self.tmp, "evidence")
        os.makedirs(sched_mod.EVIDENCE_DIR, exist_ok=True)

    def tearDown(self):
        sched_mod.EVIDENCE_DIR = self._evidence_backup
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *p):
        return os.path.join(self.tmp, *p)

    def make_scheduler(self, capture=None, ocr=None, notifier=None,
                       on_encounter=None, entries=(("id1", "PlayerX"),)):
        db = BlacklistDB(self.path("bl.db"))
        for pid, name in entries:
            db.add(pid, name)
        matcher = Matcher(db)
        region = RegionConfig(self.path("user_config.json"))
        sched = ScanScheduler(
            db=db, matcher=matcher, notifier=notifier or FakeNotifier(),
            ocr=ocr or FakeOCR(), capture=capture or FakeCapture(),
            region_config=region, on_encounter=on_encounter,
        )
        self.addCleanup(db.close)
        return sched, db


# ==========================================================================
class TestPlayerNameFilter(unittest.TestCase):
    def test_valid_names(self):
        for n in ("PlayerX", "John Doe", "阴影猎手", "abc123", "玩家_01"):
            self.assertTrue(is_valid_player_name(n), n)

    def test_invalid_names(self):
        for n in ("", " ", "a", "123456", "设置", "Resume", "!!!", "....",
                  "x" * 31, "Lv"):          # "Lv" 只有 2 字符但全 alnum → 有效
            if n == "Lv":
                self.assertTrue(is_valid_player_name(n))
                continue
            self.assertFalse(is_valid_player_name(n), n)

    def test_symbol_ratio_filter(self):
        self.assertFalse(is_valid_player_name("a........"))

    def test_evaluate_scan_filters_confidence(self):
        raw = [("PlayerX", 0.95), ("LowConf", 0.4), ("设置", 0.99), ("ab", 0.9)]
        self.assertEqual(evaluate_scan(raw), ["PlayerX", "ab"])

    def test_evaluate_scan_dedup_and_order(self):
        raw = [("Bravo", 0.9), ("Alpha", 0.9), ("Bravo", 0.9)]
        self.assertEqual(evaluate_scan(raw), ["Bravo", "Alpha"])

    def test_evaluate_scan_handles_garbage(self):
        self.assertEqual(evaluate_scan(None), [])
        self.assertEqual(evaluate_scan([("x",)]), [])
        self.assertEqual(evaluate_scan([("PlayerX", None)]), [])


class TestBoxLineGrouping(TempCase):
    """OCR 把一个名字切成多个框时，要能拼回同一行。

    继承 TempCase 很重要：它会重定向 EVIDENCE_DIR，
    否则测试产生的证据截图会落到真实的 data/evidence/ 里。
    """

    def test_merges_fragments_on_same_line(self):
        boxes = [("SamplePlayer", 0.99, (0, 0, 200, 40)),
                 ("01", 0.99, (205, 0, 235, 40))]
        self.assertEqual(group_into_lines(boxes), ["SamplePlayer01"])

    def test_keeps_separate_rows_apart(self):
        boxes = [("Alpha", 0.99, (0, 0, 100, 40)),
                 ("Beta", 0.99, (0, 60, 100, 100))]
        self.assertEqual(group_into_lines(boxes), ["Alpha", "Beta"])

    def test_long_horizontal_gap_does_not_merge(self):
        boxes = [("Alpha", 0.99, (0, 0, 100, 40)),
                 ("Beta", 0.99, (400, 0, 500, 40))]
        self.assertEqual(group_into_lines(boxes), ["Alpha", "Beta"])

    def test_filters_low_confidence(self):
        boxes = [("PlayerX", 0.3, (0, 0, 100, 40)),
                 ("Bravo", 0.9, (0, 60, 100, 100))]
        self.assertEqual(group_into_lines(boxes), ["Bravo"])

    def test_without_bbox_each_is_its_own_line(self):
        self.assertEqual(group_into_lines([("Alpha", 0.9), ("123456", 0.9)]),
                         ["Alpha"])

    def test_sorts_by_reading_order(self):
        boxes = [("Bravo", 0.9, (0, 60, 100, 100)),
                 ("Alpha", 0.9, (0, 0, 100, 40))]
        self.assertEqual(group_into_lines(boxes), ["Alpha", "Bravo"])

    def test_empty_and_garbage(self):
        self.assertEqual(group_into_lines([]), [])
        self.assertEqual(group_into_lines(None), [])
        # 框解析失败 → 退化成"独立一行"，仍然要过玩家名过滤
        self.assertEqual(group_into_lines([("Xy", 0.9, "bad")]), ["Xy"])
        self.assertEqual(group_into_lines([("X", 0.9, "bad")]), [])   # 太短

    def test_flat_eight_number_box(self):
        boxes = [("Alpha", 0.9, [0, 0, 100, 0, 100, 40, 0, 40])]
        self.assertEqual(group_into_lines(boxes), ["Alpha"])

    def test_merged_name_matches_blacklist_exactly(self):
        """拼回来的名字应能 100 分命中带下划线的黑名单条目。"""
        db = BlacklistDB(self.path("bl.db"))
        try:
            db.add("id1", "SamplePlayer_01")
            m = Matcher(db)
            boxes = [("SamplePlayer", 0.99, (0, 0, 200, 40)),
                     ("01", 0.99, (205, 0, 235, 40))]
            names = group_into_lines(boxes)
            hits = m.check(names)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0][0]["player_name"], "SamplePlayer_01")
            self.assertEqual(hits[0][1], 100.0)
        finally:
            db.close()

    def test_scan_session_prefers_recognize_lines(self):
        """真实引擎提供 recognize_lines 时应当被优先使用。"""

        class LineOCR(FakeOCR):
            def __init__(self):
                super().__init__()
                self.line_calls = 0

            def recognize_lines(self, img):
                self.line_calls += 1
                return ["PlayerX"]

            def recognize_raw(self, img):
                raise AssertionError("不应回退到 recognize_raw")

        db = BlacklistDB(self.path("bl.db"))
        try:
            db.add("id1", "PlayerX")
            sched = ScanScheduler(
                db=db, matcher=Matcher(db), notifier=FakeNotifier(),
                ocr=LineOCR(), capture=FakeCapture(),
                region_config=RegionConfig(self.path("u.json")),
            )
            s = ScanSession(sched, "player_list_hud", "test",
                            dict(COLD_START_SESSION, interval=0.01,
                                 max_duration=2, max_consecutive_empty=99,
                                 keep_alive_after_hit=0.05))
            s.start()
            s._thread.join(timeout=5)
            self.assertGreater(sched.ocr.line_calls, 0)
            self.assertEqual(db.get_all()[0]["encounter_count"], 1)
            # 证据必须落在被重定向的临时目录里
            self.assertEqual(len(os.listdir(sched_mod.EVIDENCE_DIR)), 1)
        finally:
            db.close()


# ==========================================================================
class TestScanSession(TempCase):
    def _params(self, **kw):
        p = dict(COLD_START_SESSION)
        p.update(kw)
        return p

    def test_terminates_after_consecutive_empty(self):
        sched, db = self.make_scheduler()
        ocr = FakeOCR()                       # 永远识别不到东西
        sched.ocr = ocr
        s = ScanSession(sched, "player_list_hud", "test",
                        self._params(interval=0.01, max_consecutive_empty=3,
                                     max_duration=10, keep_alive_after_hit=0,
                                     skip_unchanged=False))
        s.start()
        s._thread.join(timeout=5)
        self.assertFalse(s.alive)
        self.assertGreaterEqual(ocr.calls, 3)
        self.assertEqual(s.hit_count, 0)
        self.assertFalse(sched.session_busy())

    def test_terminates_on_max_duration(self):
        sched, db = self.make_scheduler()
        s = ScanSession(sched, "player_list_hud", "test",
                        self._params(interval=0.02, max_consecutive_empty=9999,
                                     max_duration=0.2, keep_alive_after_hit=0))
        t0 = time.time()
        s.start()
        s._thread.join(timeout=5)
        elapsed = time.time() - t0
        self.assertFalse(s.alive)
        self.assertLess(elapsed, 3.0)

    def test_keep_alive_after_hit_stops_early(self):
        sched, db = self.make_scheduler()
        sched.ocr = FakeOCR()
        sched.ocr.default = [("PlayerX", 0.99)]
        s = ScanSession(sched, "player_list_hud", "test",
                        self._params(interval=0.02, max_consecutive_empty=9999,
                                     max_duration=10, keep_alive_after_hit=0.15))
        t0 = time.time()
        s.start()
        s._thread.join(timeout=5)
        elapsed = time.time() - t0
        self.assertFalse(s.alive)
        self.assertLess(elapsed, 3.0)          # 远小于 max_duration
        self.assertGreaterEqual(elapsed, 0.15)

    def test_same_name_not_matched_twice_in_session(self):
        sched, db = self.make_scheduler()
        sched.ocr = FakeOCR()
        sched.ocr.default = [("PlayerX", 0.99)]
        s = ScanSession(sched, "player_list_hud", "test",
                        self._params(interval=0.02, max_consecutive_empty=9999,
                                     max_duration=10, keep_alive_after_hit=0.2))
        s.start()
        s._thread.join(timeout=5)
        self.assertEqual(db.get_all()[0]["encounter_count"], 1)   # 只命中一次

    def test_session_kept_alive_while_new_names_appear(self):
        """滚动不停出现新名字 → 会话持续；滚动停止后才按 keep_alive 收工。"""
        sched, db = self.make_scheduler()
        counter = {"n": 0}

        class RollingOCR(FakeOCR):
            def recognize_raw(self, img):
                counter["n"] += 1
                return [(f"Player{counter['n']}", 0.99)]

        sched.ocr = RollingOCR()
        s = ScanSession(sched, "menu_player_list", "esc_menu",
                        self._params(interval=0.02, max_consecutive_empty=9999,
                                     max_duration=0.4, keep_alive_after_hit=0.1,
                                     skip_unchanged=False))
        t0 = time.time()
        s.start()
        s._thread.join(timeout=5)
        elapsed = time.time() - t0
        self.assertFalse(s.alive)
        self.assertGreaterEqual(elapsed, 0.35)     # 一直在刷新 → 撑到 max_duration
        self.assertGreater(counter["n"], 5)

    def test_stop_is_prompt(self):
        sched, db = self.make_scheduler()
        s = ScanSession(sched, "player_list_hud", "test",
                        self._params(interval=5.0, max_duration=60,
                                     max_consecutive_empty=99))
        s.start()
        time.sleep(0.05)
        t0 = time.time()
        s.stop(wait=True, timeout=3)
        self.assertLess(time.time() - t0, 1.0)
        self.assertFalse(s.alive)

    # ------------------------------------------------ 静止画面的省电优化
    def test_static_screen_skips_ocr(self):
        """画面没变就不该重复 OCR（同图 OCR 是纯烧 CPU，还拖累游戏帧数）。"""
        sched, db = self.make_scheduler()      # FakeCapture 永远返回同一张图
        ocr = FakeOCR([("PlayerX", 0.99)])
        sched.ocr = ocr
        s = ScanSession(sched, "player_list_hud", "test",
                        self._params(interval=0.01, max_consecutive_empty=9999,
                                     max_duration=5, keep_alive_after_hit=0,
                                     max_static_frames=0))
        skipped = [s._scan_once()[2] for _ in range(8)]
        self.assertEqual(ocr.calls, 1)          # 只有第 1 帧真的识别
        self.assertEqual(skipped, [False] + [True] * 7)

    def test_changing_screen_still_scans(self):
        """画面一直在变（滚动）→ 每帧都要识别，不能漏。"""
        class ChangingCapture(FakeCapture):
            def __init__(self):
                super().__init__()
                self.n = 0

            def grab(self, region_key):
                self.n += 1
                self.grab_calls += 1
                return Image.new("RGB", (40, 20), (self.n * 7 % 255, 10, 10))

        sched, db = self.make_scheduler(capture=ChangingCapture())
        ocr = FakeOCR()
        sched.ocr = ocr
        s = ScanSession(sched, "player_list_hud", "test",
                        self._params(interval=0.01, max_consecutive_empty=9999,
                                     max_duration=5, keep_alive_after_hit=0,
                                     max_static_frames=0))
        skipped = [s._scan_once()[2] for _ in range(5)]
        self.assertEqual(ocr.calls, 5)
        self.assertEqual(skipped, [False] * 5)

    def test_static_frames_end_the_session(self):
        """菜单开着不动 → 连续静止 N 帧就收工，不再一次次抓屏。"""
        sched, db = self.make_scheduler()
        sched.ocr = FakeOCR([("PlayerX", 0.99)])
        s = ScanSession(sched, "player_list_hud", "test",
                        self._params(interval=0.01, max_consecutive_empty=9999,
                                     max_duration=10, keep_alive_after_hit=0,
                                     max_static_frames=3))
        s.start()
        s._thread.join(timeout=5)
        self.assertFalse(s.alive)
        self.assertGreaterEqual(s.skipped_frames, 3)
        # 抓屏次数 = 1 次真扫 + 3 次静止判定，远小于 10s 里能抓的次数
        self.assertLessEqual(sched.capture.grab_calls, 6)

    def test_static_limit_zero_disables_early_stop(self):
        """max_static_frames=0 → 不因为静止提前收工（只受 max_duration 约束）。"""
        sched, db = self.make_scheduler()
        sched.ocr = FakeOCR([("PlayerX", 0.99)])
        s = ScanSession(sched, "player_list_hud", "test",
                        self._params(interval=0.01, max_consecutive_empty=9999,
                                     max_duration=0.15, keep_alive_after_hit=0,
                                     max_static_frames=0))
        s.start()
        s._thread.join(timeout=5)
        self.assertFalse(s.alive)
        self.assertGreater(s.skipped_frames, 3)   # 一直跳过但没提前收工

    def test_skip_unchanged_can_be_disabled(self):
        sched, db = self.make_scheduler()
        ocr = FakeOCR([("PlayerX", 0.99)])
        sched.ocr = ocr
        s = ScanSession(sched, "player_list_hud", "test",
                        self._params(interval=0.01, max_consecutive_empty=9999,
                                     max_duration=5, keep_alive_after_hit=0,
                                     max_static_frames=0,
                                     skip_unchanged=False))
        for _ in range(4):
            s._scan_once()
        self.assertEqual(ocr.calls, 4)
        self.assertEqual(s.skipped_frames, 0)

    def test_capture_failure_does_not_hang(self):
        class BoomCapture(FakeCapture):
            def grab(self, region_key):
                raise RuntimeError("boom")

        sched, db = self.make_scheduler(capture=BoomCapture())
        s = ScanSession(sched, "player_list_hud", "test",
                        self._params(interval=0.01, max_consecutive_empty=3,
                                     max_duration=5))
        s.start()
        s._thread.join(timeout=5)
        self.assertFalse(s.alive)             # 连续异常也能正常退出

    def test_ocr_none_image_returns_empty(self):
        sched, db = self.make_scheduler()
        s = ScanSession(sched, "player_list_hud", "test",
                        self._params(interval=0.01, max_consecutive_empty=2,
                                     max_duration=5))
        s.start()
        s._thread.join(timeout=5)
        self.assertFalse(s.alive)


# ==========================================================================
class TestScanScheduler(TempCase):
    def test_handle_hit_updates_db_and_callback(self):
        events = []
        notifier = FakeNotifier()
        sched, db = self.make_scheduler(notifier=notifier,
                                        on_encounter=events.append)
        entry = db.get_all()[0]
        updated = sched.handle_hit(entry, 100.0, "chat",
                                   Image.new("RGB", (10, 10)), "PlayerX")
        self.assertEqual(updated["encounter_count"], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["entry_id"], entry["id"])
        self.assertEqual(events[0]["player_name"], "PlayerX")
        self.assertEqual(events[0]["score"], 100.0)
        self.assertEqual(events[0]["source"], "chat")
        self.assertEqual(len(notifier.alerts), 1)
        # 证据截图已保存
        self.assertTrue(os.listdir(sched_mod.EVIDENCE_DIR))

    def test_dedup_blocks_both_count_and_alert(self):
        """新的语义：去重窗口内 **既不计入 encounter_count，也不弹提示**。"""
        notifier = FakeNotifier()
        sched, db = self.make_scheduler(notifier=notifier)
        entry = db.get_all()[0]
        first = sched.handle_hit(entry, 100.0, "chat",
                                 Image.new("RGB", (10, 10)), "PlayerX")
        self.assertIsNotNone(first)
        for _ in range(4):
            sched.handle_hit(entry, 100.0, "chat",
                             Image.new("RGB", (10, 10)), "PlayerX")
        self.assertEqual(len(notifier.alerts), 1)              # 只弹一次
        self.assertEqual(len(notifier.batches), 1)
        self.assertEqual(sched.get_session_hit_count(), 1)     # 只计一次
        self.assertEqual(db.get(entry["id"])["encounter_count"], 1)
        self.assertEqual(db.get_total_encounter_count(), 1)

    def test_dedup_does_not_write_extra_encounter_rows(self):
        sched, db = self.make_scheduler()
        entry = db.get_all()[0]
        for _ in range(3):
            sched.handle_hit(entry, 100.0, "chat", None, "PlayerX")
        self.assertEqual(len(db.get_encounters(entry["id"])), 1)

    def test_process_text(self):
        sched, db = self.make_scheduler()
        n = sched.process_text("PlayerX has joined the game", "chat",
                               Image.new("RGB", (10, 10)))
        self.assertEqual(n, 1)
        self.assertEqual(db.get_all()[0]["encounter_count"], 1)
        rows = db.get_recent_encounters()
        self.assertEqual(rows[0]["name_seen"], "PlayerX")
        self.assertEqual(rows[0]["source"], "chat")

    def test_process_text_no_match(self):
        sched, db = self.make_scheduler()
        self.assertEqual(sched.process_text("Nobody here", "chat", None), 0)
        self.assertEqual(sched.process_text("", "chat", None), 0)
        self.assertEqual(db.get_total_encounter_count(), 0)

    def test_match_and_notify_list(self):
        sched, db = self.make_scheduler(entries=(("id1", "Alpha"),
                                                 ("id2", "Beta")))
        n = sched.match_and_notify(["Alpha", "Beta", "Nobody"], "esc_menu",
                                   Image.new("RGB", (10, 10)))
        self.assertEqual(n, 2)
        self.assertEqual(db.get_total_encounter_count(), 2)

    def test_evidence_disabled(self):
        sched, db = self.make_scheduler()
        sched.evidence_enabled = False
        entry = db.get_all()[0]
        sched.handle_hit(entry, 100.0, "chat",
                         Image.new("RGB", (10, 10)), "PlayerX")
        self.assertEqual(os.listdir(sched_mod.EVIDENCE_DIR), [])

    def test_evidence_none_image(self):
        sched, db = self.make_scheduler()
        entry = db.get_all()[0]
        sched.handle_hit(entry, 100.0, "chat", None, "PlayerX")
        self.assertEqual(os.listdir(sched_mod.EVIDENCE_DIR), [])

    def test_session_hit_count_and_reset(self):
        sched, db = self.make_scheduler()
        entry = db.get_all()[0]
        sched.handle_hit(entry, 100.0, "chat", None, "PlayerX")
        sched.handle_hit(entry, 100.0, "chat", None, "PlayerX")
        # 第二次被去重 → 本局命中只加了一次
        self.assertEqual(sched.get_session_hit_count(), 1)
        sched.reset_session_hit_count()
        self.assertEqual(sched.get_session_hit_count(), 0)

    def test_start_session_replaces_old_one(self):
        sched, db = self.make_scheduler()
        sched.capture = FakeCapture()
        s1 = sched.start_session("player_list_hud", "cold_start",
                                 dict(COLD_START_SESSION, interval=0.05,
                                      max_duration=30,
                                      max_consecutive_empty=9999))
        time.sleep(0.1)
        self.assertTrue(sched.session_busy())
        s2 = sched.start_session("menu_player_list", "esc_menu",
                                 dict(ESC_SESSION, interval=0.05,
                                      max_duration=30,
                                      max_consecutive_empty=9999))
        time.sleep(0.1)
        self.assertFalse(s1.alive)            # 旧会话被终止
        self.assertTrue(s2.alive)
        sched.stop_session()
        time.sleep(0.1)
        self.assertFalse(sched.session_busy())

    def test_set_game_active(self):
        sched, db = self.make_scheduler()
        self.assertFalse(sched.game_active.is_set())
        sched.set_game_active(True)
        self.assertTrue(sched.game_active.is_set())
        sched.set_game_active(False)
        self.assertFalse(sched.game_active.is_set())

    def test_reload_blacklist(self):
        sched, db = self.make_scheduler()
        db.add("id9", "Later")
        self.assertEqual(sched.reload_blacklist(), 2)

    def test_hit_on_missing_entry_is_swallowed(self):
        """单条坏数据不能拖垮整批：记录警告并跳过，不抛异常。"""
        sched, db = self.make_scheduler()
        ghost = {"id": 99999, "player_name": "Ghost", "player_id": "x"}
        self.assertIsNone(
            sched.handle_hit(ghost, 100.0, "chat", None, "Ghost"))

    def test_bad_entry_does_not_break_batch(self):
        sched, db = self.make_scheduler()
        good = db.get_all()[0]
        ghost = {"id": 99999, "player_name": "Ghost", "player_id": "x"}
        processed = sched.handle_hits(
            [(ghost, 100.0, "Ghost"), (good, 100.0, "PlayerX")],
            "chat", None)
        self.assertEqual(processed, 1)              # 好的那条照常处理
        self.assertEqual(db.get(good["id"])["encounter_count"], 1)

    def test_shutdown(self):
        sched, db = self.make_scheduler()
        sched.shutdown()
        self.assertIsNone(sched.start_session("player_list_hud", "x",
                                              dict(ESC_SESSION)))

    # ------------------------------------------------------ 命中去重
    def test_handle_hits_dedup_within_window(self):
        sched, db = self.make_scheduler()
        entry = db.get_all()[0]
        self.assertEqual(
            sched.handle_hits([(entry, 100.0, "PlayerX")], "chat", None), 1)
        # 窗口内再命中 → 直接跳过
        self.assertEqual(
            sched.handle_hits([(entry, 100.0, "PlayerX")], "chat", None), 0)
        self.assertEqual(db.get(entry["id"])["encounter_count"], 1)
        self.assertEqual(db.get_total_encounter_count(), 1)

    def test_dedup_is_per_entry(self):
        """一个玩家被去重，不影响同批次里的另一个玩家。"""
        sched, db = self.make_scheduler(
            entries=(("id1", "Alpha"), ("id2", "Bravo")))
        rows = {r["player_name"]: r for r in db.get_all()}
        alpha, bravo = rows["Alpha"], rows["Bravo"]

        sched.handle_hits([(alpha, 100.0, "Alpha")], "chat", None)
        # 第二批同时包含"已命中的 Alpha"和"新的 Bravo"
        processed = sched.handle_hits(
            [(alpha, 100.0, "Alpha"), (bravo, 100.0, "Bravo")], "chat", None)
        self.assertEqual(processed, 1)
        self.assertEqual(db.get(alpha["id"])["encounter_count"], 1)
        self.assertEqual(db.get(bravo["id"])["encounter_count"], 1)

    def test_dedup_expires(self):
        sched, db = self.make_scheduler()
        entry = db.get_all()[0]
        sched.handle_hits([(entry, 100.0, "PlayerX")], "chat", None)
        with sched._recent_lock:                    # 模拟窗口已过
            for k in list(sched._recent_hits):
                sched._recent_hits[k] -= (config.HIT_DEDUP_WINDOW + 1)
        self.assertEqual(
            sched.handle_hits([(entry, 100.0, "PlayerX")], "chat", None), 1)
        self.assertEqual(db.get(entry["id"])["encounter_count"], 2)

    def test_clear_dedup_cache_and_size(self):
        sched, db = self.make_scheduler()
        entry = db.get_all()[0]
        sched.handle_hits([(entry, 100.0, "PlayerX")], "chat", None)
        self.assertEqual(sched.dedup_cache_size(), 1)
        sched.clear_dedup_cache()
        self.assertEqual(sched.dedup_cache_size(), 0)
        self.assertEqual(
            sched.handle_hits([(entry, 100.0, "PlayerX")], "chat", None), 1)

    def test_dedup_cache_does_not_grow_unbounded(self):
        sched, db = self.make_scheduler()
        with sched._recent_lock:                    # 塞入大量过期记录
            for i in range(400):
                sched._recent_hits[i] = 0
        entry = db.get_all()[0]
        sched.handle_hits([(entry, 100.0, "PlayerX")], "chat", None)
        self.assertLess(sched.dedup_cache_size(), 400)

    def test_dedup_skip_is_logged(self, ):
        sched, db = self.make_scheduler()
        entry = db.get_all()[0]
        sched.handle_hits([(entry, 100.0, "chat_manual")], "chat_manual", None)
        sched.handle_hits([(entry, 100.0, "chat_manual")], "chat_manual", None)
        # 日志里应出现「去重跳过」
        log_path = config.LOG_PATH
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            tail = f.read()[-20000:]
        self.assertIn("去重跳过", tail)
        self.assertIn("entry_id=", tail)

    # ------------------------------------------------------ 批量通知
    def test_handle_hits_calls_alert_batch_once(self):
        notifier = FakeNotifier()
        sched, db = self.make_scheduler(
            notifier=notifier,
            entries=(("id1", "Alpha"), ("id2", "Bravo"), ("id3", "Charlie")))
        hits = [(r, 100.0, r["player_name"]) for r in db.get_all()]
        processed = sched.handle_hits(hits, "chat_manual", None)
        self.assertEqual(processed, 3)
        # 关键：只调用一次 alert_batch → Notifier 内部只播一次音效
        self.assertEqual(len(notifier.batches), 1)
        self.assertEqual(len(notifier.batches[0]), 3)
        self.assertEqual(db.get_total_encounter_count(), 3)

    def test_match_and_notify_batch_is_used_by_sessions(self):
        notifier = FakeNotifier()
        sched, db = self.make_scheduler(
            notifier=notifier,
            entries=(("id1", "Alpha"), ("id2", "Bravo")))
        n = sched.match_and_notify_batch(["Alpha", "Bravo"], "esc_menu", None)
        self.assertEqual(n, 2)
        self.assertEqual(len(notifier.batches), 1)

    def test_match_and_notify_alias_still_works(self):
        sched, db = self.make_scheduler()
        n = sched.match_and_notify(["PlayerX"], "cold_start", None)
        self.assertEqual(n, 1)

    def test_process_text_uses_batch(self):
        notifier = FakeNotifier()
        sched, db = self.make_scheduler(
            notifier=notifier,
            entries=(("id1", "Alpha"), ("id2", "Bravo")))
        n = sched.process_text("Alpha joined. Bravo joined.", "chat_manual",
                               None)
        self.assertEqual(n, 2)
        self.assertEqual(len(notifier.batches), 1)

    def test_empty_batch_is_noop(self):
        sched, db = self.make_scheduler()
        self.assertEqual(sched.handle_hits([], "chat", None), 0)
        self.assertEqual(sched.handle_hits(None, "chat", None), 0)

    def test_all_deduped_batch_sends_no_notification(self):
        notifier = FakeNotifier()
        sched, db = self.make_scheduler(notifier=notifier)
        entry = db.get_all()[0]
        sched.handle_hits([(entry, 100.0, "PlayerX")], "chat", None)
        notifier.batches.clear()
        self.assertEqual(
            sched.handle_hits([(entry, 100.0, "PlayerX")], "chat", None), 0)
        self.assertEqual(notifier.batches, [])       # 全被去重 → 不弹提示

    def test_on_encounter_called_per_hit(self):
        events = []
        sched, db = self.make_scheduler(
            on_encounter=events.append,
            entries=(("id1", "Alpha"), ("id2", "Bravo")))
        hits = [(r, 100.0, r["player_name"]) for r in db.get_all()]
        sched.handle_hits(hits, "chat_manual", None)
        self.assertEqual(len(events), 2)             # 每个玩家各自通知 GUI
        self.assertEqual({e["player_name"] for e in events}, {"Alpha", "Bravo"})
        self.assertTrue(all(e["encounter_count"] == 1 for e in events))


# ==========================================================================
class TestChatScanner(TempCase):
    """聊天框按需扫描：无循环、无定时、_busy 锁、批量命中。"""

    def _scanner(self, capture=None, ocr=None, notifier=None,
                 entries=(("id1", "PlayerX"),)):
        sched, db = self.make_scheduler(capture=capture, ocr=ocr,
                                        notifier=notifier or FakeNotifier(),
                                        entries=entries)
        sc = ChatScanner(sched, sched.capture, sched.ocr)
        return sc, sched, db

    # ---- 基本流程 ----
    def test_hit_updates_db_and_returns_ok(self):
        ocr = FakeOCR()
        ocr.default = [("PlayerX has joined the game", 0.99)]
        sc, sched, db = self._scanner(ocr=ocr)
        res = sc.scan_now()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["hits"], 1)
        self.assertEqual(res["processed"], 1)
        self.assertEqual(db.get_all()[0]["encounter_count"], 1)
        self.assertEqual(db.get_recent_encounters()[0]["source"], "chat_manual")
        self.assertEqual(sc.scan_count, 1)

    def test_source_can_be_overridden(self):
        ocr = FakeOCR()
        ocr.default = [("PlayerX", 0.99)]
        sc, sched, db = self._scanner(ocr=ocr)
        sc.scan_now(source="chat_hotkey")
        self.assertEqual(db.get_recent_encounters()[0]["source"], "chat_hotkey")

    def test_no_hit(self):
        ocr = FakeOCR()
        ocr.default = [("SomeRandomGuy", 0.99)]
        sc, sched, db = self._scanner(ocr=ocr)
        res = sc.scan_now()
        self.assertEqual(res["status"], "no_hit")
        self.assertEqual(res["hits"], 0)
        self.assertEqual(db.get_total_encounter_count(), 0)

    def test_empty_chat_box(self):
        sc, sched, db = self._scanner(ocr=FakeOCR())      # OCR 返回空
        res = sc.scan_now()
        self.assertEqual(res["status"], "empty")
        self.assertEqual(db.get_total_encounter_count(), 0)

    def test_low_confidence_text_ignored(self):
        ocr = FakeOCR()
        ocr.default = [("PlayerX", 0.2)]
        sc, sched, db = self._scanner(ocr=ocr)
        self.assertEqual(sc.scan_now()["status"], "empty")

    def test_capture_failure_returns_error(self):
        class NoImage(FakeCapture):
            def grab(self, region_key):
                return None

        sc, sched, db = self._scanner(capture=NoImage(), ocr=FakeOCR())
        res = sc.scan_now()
        self.assertEqual(res["status"], "error")
        self.assertIn("截图", res["error"])

    def test_capture_exception_is_swallowed(self):
        class Boom(FakeCapture):
            def grab(self, region_key):
                raise RuntimeError("boom")

        sc, sched, db = self._scanner(capture=Boom(), ocr=FakeOCR())
        res = sc.scan_now()                    # 不应抛异常
        self.assertEqual(res["status"], "error")
        self.assertIn("boom", res["error"])

    def test_ocr_exception_is_swallowed(self):
        class BoomOCR(FakeOCR):
            def recognize_raw(self, img):
                raise RuntimeError("ocr boom")

        sc, sched, db = self._scanner(ocr=BoomOCR())
        res = sc.scan_now()
        self.assertEqual(res["status"], "error")

    # ---- _busy 锁 ----
    def test_no_concurrent_ocr(self):
        """上一次扫描没结束时，第二次调用应立刻返回 busy，且不跑 OCR。"""
        gate = threading.Event()
        entered = threading.Event()

        class SlowOCR(FakeOCR):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def recognize_raw(self, img):
                self.calls += 1
                entered.set()
                gate.wait(timeout=5)
                return [("PlayerX", 0.99)]

        ocr = SlowOCR()
        sc, sched, db = self._scanner(ocr=ocr)

        results = []

        def first():
            results.append(sc.scan_now())

        t = threading.Thread(target=first, daemon=True)
        t.start()
        self.assertTrue(entered.wait(timeout=5))

        # 第一次还在跑 → 第二次必须被挡下
        second = sc.scan_now()
        self.assertEqual(second["status"], "busy")
        self.assertTrue(sc.is_busy())

        gate.set()
        t.join(timeout=5)
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(ocr.calls, 1)          # OCR 只跑了一次
        self.assertFalse(sc.is_busy())

    def test_is_busy_false_initially(self):
        sc, sched, db = self._scanner()
        self.assertFalse(sc.is_busy())

    def test_sequential_scans_are_allowed(self):
        ocr = FakeOCR()
        ocr.default = [("SomeRandomGuy", 0.99)]     # 有文字才会走到 scan_count
        sc, sched, db = self._scanner(ocr=ocr)
        sc.scan_now()
        self.assertFalse(sc.is_busy())
        sc.scan_now()                            # 不会被锁挡住
        self.assertEqual(sc.scan_count, 2)

    # ---- 去重 ----
    def test_second_scan_within_window_is_deduped(self):
        """连点扫描按钮：60 秒内同一玩家只计数一次。"""
        ocr = FakeOCR()
        ocr.default = [("PlayerX has joined the game", 0.99)]
        sc, sched, db = self._scanner(ocr=ocr)

        first = sc.scan_now()
        second = sc.scan_now()
        third = sc.scan_now()

        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 0)     # 被去重
        self.assertEqual(third["processed"], 0)
        self.assertEqual(second["hits"], 1)          # 仍然匹配到了，只是没计数
        self.assertEqual(db.get_all()[0]["encounter_count"], 1)
        self.assertEqual(db.get_total_encounter_count(), 1)

    def test_dedup_expires_after_window(self):
        ocr = FakeOCR()
        ocr.default = [("PlayerX", 0.99)]
        sc, sched, db = self._scanner(ocr=ocr)
        sc.scan_now()
        # 把去重缓存里的时间戳往前挪，模拟 60 秒已过
        with sched._recent_lock:
            for k in list(sched._recent_hits):
                sched._recent_hits[k] -= (config.HIT_DEDUP_WINDOW + 1)
        sc.scan_now()
        self.assertEqual(db.get_all()[0]["encounter_count"], 2)

    def test_clear_dedup_cache_allows_recount(self):
        ocr = FakeOCR()
        ocr.default = [("PlayerX", 0.99)]
        sc, sched, db = self._scanner(ocr=ocr)
        sc.scan_now()
        sched.clear_dedup_cache()
        self.assertEqual(sched.dedup_cache_size(), 0)
        sc.scan_now()
        self.assertEqual(db.get_all()[0]["encounter_count"], 2)

    # ---- 批量命中 ----
    def test_batch_hit_one_batch_one_sound(self):
        """一次扫描命中 2 个玩家 → 1 次 alert_batch（内部再拆成 2 个通知栏）。"""
        notifier = FakeNotifier()
        ocr = FakeOCR()
        ocr.default = [("PlayerX has joined the game", 0.99),
                       ("John Doe has joined the game", 0.99)]
        sc, sched, db = self._scanner(
            ocr=ocr, notifier=notifier,
            entries=(("id1", "PlayerX"), ("id2", "John Doe")))

        res = sc.scan_now()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["hits"], 2)
        self.assertEqual(res["processed"], 2)
        # 只调用了一次批量接口 → Notifier 内部只播一次音效
        self.assertEqual(len(notifier.batches), 1)
        self.assertEqual(len(notifier.batches[0]), 2)
        self.assertEqual(db.get_total_encounter_count(), 2)

    def test_batch_hit_three_players(self):
        notifier = FakeNotifier()
        ocr = FakeOCR()
        ocr.default = [("Alpha joined", 0.99), ("Bravo joined", 0.99),
                       ("Charlie joined", 0.99)]
        sc, sched, db = self._scanner(
            ocr=ocr, notifier=notifier,
            entries=(("a", "Alpha"), ("b", "Bravo"), ("c", "Charlie")))
        res = sc.scan_now()
        self.assertEqual(res["processed"], 3)
        self.assertEqual(len(notifier.batches), 1)
        self.assertEqual(len(notifier.batches[0]), 3)
        self.assertEqual(db.get_total_encounter_count(), 3)

    def test_single_hit_still_one_batch(self):
        notifier = FakeNotifier()
        ocr = FakeOCR()
        ocr.default = [("PlayerX", 0.99)]
        sc, sched, db = self._scanner(ocr=ocr, notifier=notifier)
        sc.scan_now()
        self.assertEqual(len(notifier.batches), 1)
        self.assertEqual(len(notifier.batches[0]), 1)

    def test_on_result_callback(self):
        seen = []
        ocr = FakeOCR()
        ocr.default = [("PlayerX", 0.99)]
        sc, sched, db = self._scanner(ocr=ocr)
        sc.on_result = seen.append
        sc.scan_now()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["status"], "ok")

    def test_on_result_exception_is_swallowed(self):
        def boom(_res):
            raise RuntimeError("callback boom")

        sc, sched, db = self._scanner()
        sc.on_result = boom
        res = sc.scan_now()                    # 不应抛异常
        self.assertIn(res["status"], ("empty", "no_hit", "ok"))

    def test_no_loop_no_timer_attributes(self):
        """确认按需扫描器里没有任何循环/定时/缓存残留。"""
        sc, sched, db = self._scanner()
        for forbidden in ("start", "_loop", "_tick", "_chat_changed",
                          "_last_full_scan_ts", "_last_chat_hash"):
            self.assertFalse(hasattr(sc, forbidden), forbidden)

    def test_scan_is_fast(self):
        ocr = FakeOCR()
        sc, sched, db = self._scanner(ocr=ocr)
        t0 = time.time()
        sc.scan_now()
        self.assertLess(time.time() - t0, 1.0)
# ==========================================================================
class TestEscTrigger(TempCase):
    def setUp(self):
        super().setUp()
        self._delay_backup = esc_mod.ESC_MENU_DELAY
        esc_mod.ESC_MENU_DELAY = 0.01
        self.sched = FakeSchedulerForEsc()
        self.sched.game_active.set()

    def tearDown(self):
        esc_mod.ESC_MENU_DELAY = self._delay_backup
        super().tearDown()

    def test_debounce(self):
        t = EscTrigger(self.sched, FakeCapture(std=50.0), FakeOCR())
        t._on_press()
        t._on_press()
        t._on_press()
        self.assertEqual(t.trigger_count, 1)

    def test_menu_open_starts_session(self):
        cap = FakeCapture(std=50.0)
        t = EscTrigger(self.sched, cap, FakeOCR())
        t._on_press()
        time.sleep(0.1)
        self.assertEqual(len(self.sched.started), 1)
        self.assertEqual(self.sched.started[0][0], "menu_player_list")
        self.assertEqual(self.sched.started[0][1], "esc_menu")
        self.assertEqual(self.sched.stopped, 1)      # 先停旧会话

    def test_menu_closed_skips(self):
        cap = FakeCapture(std=5.0)
        t = EscTrigger(self.sched, cap, FakeOCR())
        t._on_press()
        time.sleep(0.1)
        self.assertEqual(self.sched.started, [])
        self.assertEqual(t.skip_count, 1)

    def test_rising_edge_only(self):
        state = {"v": 0}
        orig = esc_mod._get_async_key_state
        esc_mod._get_async_key_state = lambda vk: state["v"]
        try:
            t = EscTrigger(self.sched, FakeCapture(std=50.0), FakeOCR())
            t._stop = threading.Event()
            # 模拟：按住不放 → 只应触发一次
            thread = threading.Thread(target=t._loop, daemon=True)
            thread.start()
            time.sleep(0.05)
            state["v"] = 0x8000            # 按下
            time.sleep(0.35)
            t.stop()
            thread.join(timeout=2)
            self.assertEqual(t.trigger_count, 1)
        finally:
            esc_mod._get_async_key_state = orig

    def test_no_trigger_when_game_inactive(self):
        self.sched.game_active.clear()
        t = EscTrigger(self.sched, FakeCapture(std=50.0), FakeOCR())
        t._on_press()
        time.sleep(0.05)
        self.assertEqual(self.sched.started, [])


# ==========================================================================
class TestOCREnginePreprocess(unittest.TestCase):
    def test_preprocess_scales_and_shapes(self):
        eng = OCREngine(upscale=2)
        arr = eng.preprocess(Image.new("RGB", (100, 50), (30, 30, 30)))
        self.assertEqual(arr.shape, (100, 200, 3))
        self.assertEqual(arr.dtype.name, "uint8")

    def test_preprocess_handles_tiny_and_gray(self):
        eng = OCREngine(upscale=3)
        arr = eng.preprocess(Image.new("L", (4, 3), 128))
        self.assertEqual(arr.shape, (9, 12, 3))

    def test_recognize_raw_without_engine_returns_list(self):
        """无论 rapidocr 是否安装，都只能返回 list，不能抛异常。"""
        eng = OCREngine()
        out = eng.recognize_raw(Image.new("RGB", (40, 20)))
        self.assertIsInstance(out, list)

    def test_recognize_raw_none(self):
        self.assertEqual(OCREngine().recognize_raw(None), [])


class TestOCRParse(unittest.TestCase):
    """_parse 必须同时兼容 rapidocr 各版本的返回结构。"""

    def setUp(self):
        self.eng = OCREngine()

    def test_classic_tuple_format(self):
        out = ([[[[0, 0], [10, 0], [10, 10], [0, 10]], "PlayerX", 0.98],
                [[[0, 10], [10, 10], [10, 20], [0, 20]], "加入了", 0.91]],
               [0.03, 0.02])
        self.assertEqual(self.eng._parse(out),
                         [("PlayerX", 0.98), ("加入了", 0.91)])

    def test_none_result(self):
        self.assertEqual(self.eng._parse((None, [0.0])), [])
        self.assertEqual(self.eng._parse(None), [])

    def test_bare_list_without_elapse(self):
        out = [[None, "Bob", 0.8]]
        self.assertEqual(self.eng._parse(out), [("Bob", 0.8)])

    def test_object_format_txts_scores(self):
        class Out:
            txts = ("A", "B")
            scores = (0.9, 0.7)

        self.assertEqual(self.eng._parse((Out(), None)),
                         [("A", 0.9), ("B", 0.7)])

    def test_object_format_missing_scores_defaults_to_one(self):
        class Out:
            txts = ("A",)

        self.assertEqual(self.eng._parse((Out(), None)), [("A", 1.0)])

    def test_two_element_item(self):
        self.assertEqual(self.eng._parse(([["Text", 0.5]], None)),
                         [("Text", 0.5)])

    def test_garbage_items_skipped(self):
        out = ([[None], "junk", [None, "Ok", 0.6], 42], None)
        self.assertEqual(self.eng._parse(out), [("Ok", 0.6)])

    def test_elapse_recorded(self):
        self.eng._parse(([[None, "x", 1.0]], [0.11, 0.22]))
        self.assertAlmostEqual(self.eng.last_elapse, 0.33, places=2)

    def test_recognize_text_joins_with_space(self):
        class FakeEngine(OCREngine):
            def recognize_raw(self, img):
                return [("Hello", 0.9), ("world", 0.8), ("low", 0.1)]

        self.assertEqual(FakeEngine().recognize_text(None, min_conf=0.5),
                         "Hello world")

    def test_preprocess_clamps_huge_image(self):
        eng = OCREngine(upscale=3)
        arr = eng.preprocess(Image.new("RGB", (2000, 100), (0, 0, 0)))
        self.assertLessEqual(max(arr.shape[:2]), 2560)

    def test_preprocess_zero_size_is_safe(self):
        eng = OCREngine()
        arr = eng.preprocess(Image.new("RGB", (1, 1)))
        self.assertEqual(arr.shape[2], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

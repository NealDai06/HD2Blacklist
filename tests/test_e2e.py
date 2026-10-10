# -*- coding: utf-8 -*-
"""阶段 8-9 验收测试：端到端联调（v2：日志驱动）。

覆盖一条完整的数据流：
    插件写 playerLog.txt → 尾随 → 解析成事件 → 落库「最近遇到」
    → 命中黑名单 → 弹提示（这里用 FakeNotifier 接住）

所有落盘路径都被重定向到临时目录，不会污染交付的 data/。

运行： python -m unittest tests.test_e2e
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest

from PIL import Image

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import application as main_mod  # noqa: E402
from app import config  # noqa: E402
from app.core import database as db_mod  # noqa: E402
from app.settings import notification_config as nc_mod  # noqa: E402
from app.watch import watcher as watcher_mod  # noqa: E402

TMP_ROOT = os.path.join(_ROOT, ".test_tmp")


# ==========================================================================
def make_line(ev, cid, **kw) -> str:
    """造一行和插件格式一致的记录（18 个字段）。"""
    rec = {
        "v": 2,
        "mod": kw.pop("mod", "hd2trackerv21.2"),
        "ev": ev,
        "capture_id": str(cid),
        "ts": kw.pop("ts", "2026-01-01T00:00:00Z"),
        "ts_local": kw.pop("ts_local", "2026-01-01 08:00:00"),
        "game_pid": kw.pop("pid", 4321),
        "game_exe": kw.pop("exe", r"C:\Games\helldivers2.exe"),
        "trigger": kw.pop("trigger", "test"),
        "peer_id_hex": kw.pop("peer", None),
        "is_self": kw.pop("self", False),
        "name": kw.pop("name", None),
        "name_src": kw.pop("name_src", "roster"),
        "active_peers": kw.pop("active", 1),
        "peers": kw.pop("peers", None),
        "squad": kw.pop("squad", None),
        "self_name": kw.pop("self_name", None),
        "note": kw.pop("note", None),
    }
    assert not kw, f"make_line 收到未用参数: {kw}"
    return json.dumps(rec, ensure_ascii=False)


class LogFile:
    """往临时目录里写一份合成日志（只追加，和插件一样）。"""

    def __init__(self, path):
        self.path = path
        open(self.path, "w", encoding="utf-8", newline="").close()

    def write(self, *lines):
        with open(self.path, "a", encoding="utf-8", newline="") as f:
            for line in lines:
                f.write(line + "\n")
        return self

    def write_raw(self, text):
        with open(self.path, "a", encoding="utf-8", newline="") as f:
            f.write(text)
        return self

    def truncate(self):
        open(self.path, "w", encoding="utf-8", newline="").close()
        return self


class FakeNotifier:
    """接住 alert()，好断言"到底弹了没有、弹的是什么"。"""

    def __init__(self):
        self.alerts = []

    def alert(self, entry, score, source):
        self.alerts.append({"name": entry.get("player_name"), "score": score,
                            "source": source, "id": entry.get("id")})
        return True

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
        self.app = None

    def tearDown(self):
        if self.app is not None:
            try:
                self.app.shutdown()
            except Exception:                            # noqa: BLE001
                pass
        for obj, attr, old in reversed(self._restore):
            setattr(obj, attr, old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *p):
        return os.path.join(self.tmp, *p)

    def _patch(self, obj, attr, value):
        self._restore.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def log(self) -> LogFile:
        return LogFile(self.path("playerLog.txt"))

    def build_app(self, gui_enabled: bool = False):
        """建一个 app：数据库/通知配置/日志路径全部指向临时目录。"""
        self._patch(db_mod.BlacklistDB.__init__, "__defaults__",
                    (self.path("blacklist.db"), 10.0))
        self._patch(nc_mod.NotificationConfig.__init__, "__defaults__",
                    (self.path("notification.json"),))
        self._patch(watcher_mod, "player_log_path",
                    lambda: self.path("playerLog.txt"))
        app = main_mod.HD2BlacklistApp(gui_enabled=gui_enabled)
        app.notifier = FakeNotifier()
        self.app = app
        return app

    def drain(self, first: bool = False):
        """同步跑一轮尾随（不起线程）。"""
        if first:
            return self.app.watcher.backfill()
        return self.app.watcher.poll_once()


# ==========================================================================
class TestAssembly(TempCase):
    def test_assembles_without_gui(self):
        app = self.build_app()
        self.assertIsNone(app.gui)
        self.assertTrue(hasattr(app.db, "record_seen"))
        self.assertEqual(app.db.get_count(), 0)
        self.assertFalse(app.watcher.running)      # 没 start() 就不该有线程

    def test_shutdown_is_idempotent(self):
        app = self.build_app()
        app.shutdown()
        app.shutdown()                             # 第二次不能炸
        self.assertTrue(app._stop.is_set())

    def test_watcher_starts_and_stops(self):
        app = self.build_app()
        self.log().write(make_line("file_start", 1, self_name="我"))
        self.assertTrue(app.watcher.start())
        self.assertFalse(app.watcher.start())      # 已经在跑 → False
        deadline = 3.0
        while deadline > 0 and app.watcher.snapshot()["offset"] == 0:
            import time as _t
            _t.sleep(0.05)
            deadline -= 0.05
        app.watcher.stop()
        self.assertFalse(app.watcher.running)
        self.assertGreater(app.watcher.snapshot()["offset"], 0)


# ==========================================================================
class TestAlertPipeline(TempCase):
    """告警铁律：只有 join + 不是自己 + 命中黑名单 才弹，而且只在"实时"批次里。

    ⚠ 注意测试写法：`drain(first=True)`（首启补读）走的是 **live=False** 那条路，
    按设计**不会**告警。所以要先补读把历史吃掉，再追加新行用 `drain()` 实时跑。
    """

    def _start(self, log):
        """把 file_start 先吃掉（补读，不告警），让后面追加的行走实时路径。"""
        log.write(make_line("file_start", 1, self_name="我"))
        self.drain(first=True)

    def test_blacklisted_peer_join_alerts(self):
        app = self.build_app()
        eid = app.db.add("坏人", "TK", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="AAAA000000000001", name="坏人"))
        self.drain()
        self.assertEqual(len(app.notifier.alerts), 1)
        alert = app.notifier.alerts[0]
        self.assertEqual(alert["id"], eid)
        self.assertEqual(alert["source"], "peer_join")
        self.assertEqual(alert["score"], 100.0)

    def test_legacy_entry_without_peer_id_does_not_alert(self):
        """**只认 PeerID**：没有 ID 的老条目不会告警（名字会重名、会改）。

        这是有意的取舍：名字当身份会认错人（别人取同名）也认不出改名。
        想让它生效得先补上 ID（「最近遇到」重新加入 / 右键 [补全 PeerID]）。
        """
        app = self.build_app()
        app.db.add("老条目", "没ID")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="BBBB000000000002", name="老条目"))
        self.drain()
        self.assertEqual(app.notifier.alerts, [])

    def test_legacy_entry_alerts_after_peer_id_is_filled(self):
        """补上 ID 之后就正常告警了（这是升级老名单的正路）。"""
        app = self.build_app()
        eid = app.db.add("老条目", "没ID")
        app.db.set_peer_id(eid, "BBBB000000000002")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="BBBB000000000002", name="他改名字了"))
        self.drain()
        self.assertEqual(len(app.notifier.alerts), 1)
        self.assertEqual(app.notifier.alerts[0]["id"], eid)
        self.assertEqual(app.notifier.alerts[0]["source"], "peer_join")

    def test_renamed_player_still_alerts(self):
        """同一个人改了名字：靠 PeerID 照样认出来（名字层做不到这件事）。"""
        app = self.build_app()
        eid = app.db.add("以前的名字", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="AAAA000000000001",
                            name="现在的名字"))
        self.drain()
        self.assertEqual([a["id"] for a in app.notifier.alerts], [eid])

    def test_same_name_other_peer_id_does_not_alert(self):
        """名单里是「PlayerX 的 ID」，进来的却是同名的另一个人 → 不提醒。"""
        app = self.build_app()
        app.db.add("PlayerX", "", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="BBBB000000000002", name="PlayerX"))
        self.drain()
        self.assertEqual(app.notifier.alerts, [])

    def test_squad_snapshot_never_alerts(self):
        """快照是状态，不是事件：人已经在队里不该被反复报警。"""
        app = self.build_app()
        app.db.add("坏人", "", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("squad", 2, squad="AAAA000000000001=坏人", active=1))
        self.drain()
        self.assertEqual(app.notifier.alerts, [])
        # 但"遇到过"照记：这确实是你同队的人
        self.assertEqual(app.db.count_seen(), 1)

    def test_self_never_alerts_and_is_not_recorded(self):
        app = self.build_app()
        app.db.add("我自己", "", peer_id="CCCC000000000003")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="CCCC000000000003", self=True,
                            name="我自己"))
        self.drain()
        self.assertEqual(app.notifier.alerts, [])
        self.assertEqual(app.db.count_seen(), 0)

    def test_self_is_excluded_from_squad_snapshot(self):
        app = self.build_app()
        log = self.log()
        log.write(
            make_line("file_start", 1, self_name="我自己"),
            make_line("join", 2, peer="CCCC000000000003", self=True, name="我自己"),
            make_line("squad", 3, squad="CCCC000000000003=我自己;AAAA000000000009=队友"))
        self.drain(first=True)
        squad = app.watcher.snapshot()["squad"]
        self.assertEqual([m["peer_id"] for m in squad], ["AAAA000000000009"])

    def test_update_and_leave_do_not_alert(self):
        app = self.build_app()
        app.db.add("坏人", "", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("update", 2, peer="AAAA000000000001", name="坏人"),
                  make_line("leave", 3, peer="AAAA000000000001", name="坏人"))
        self.drain()
        self.assertEqual(app.notifier.alerts, [])
        self.assertEqual(app.db.count_seen(), 1)     # 仍然是"遇到过"

    def test_backfill_does_not_alert_but_records(self):
        """首启补读：只记事实，绝不能把历史里的 join 炸成一堆通知。"""
        app = self.build_app()
        app.db.add("坏人", "", peer_id="AAAA000000000001")
        app.matcher.reload()
        self.log().write(
            make_line("file_start", 1, self_name="我"),
            make_line("join", 2, peer="AAAA000000000001", name="坏人"))
        app.watcher.backfill()
        self.assertEqual(app.notifier.alerts, [])
        self.assertEqual(app.db.get_seen("AAAA000000000001")["name_last"], "坏人")

    def test_same_game_dedup_window(self):
        """同一局里反复进出：只提醒一次。"""
        app = self.build_app()
        app.db.add("坏人", "", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="AAAA000000000001", name="坏人"))
        self.drain()
        log.write(make_line("join", 3, peer="AAAA000000000001", name="坏人"))
        self.drain()
        self.assertEqual(len(app.notifier.alerts), 1)

    def test_new_game_alerts_again(self):
        app = self.build_app()
        app.db.add("坏人", "", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="AAAA000000000001", name="坏人"))
        self.drain()
        # 新一次游戏运行：capture_id 从头上重数，也不该被去重窗口吃掉
        log.write(make_line("file_start", 1, pid=9999, self_name="我"),
                  make_line("join", 2, pid=9999, peer="AAAA000000000001",
                            name="坏人"))
        self.drain()
        self.assertEqual(len(app.notifier.alerts), 2)

    def test_unlisted_player_no_alert_but_recorded(self):
        app = self.build_app()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="DDDD000000000007", name="路人"))
        self.drain()
        self.assertEqual(app.notifier.alerts, [])
        self.assertEqual(app.db.count_seen(), 1)

    def test_notifier_failure_does_not_kill_pipeline(self):
        app = self.build_app()
        app.db.add("坏人", "", peer_id="AAAA000000000001")
        app.matcher.reload()

        class Boom(FakeNotifier):
            def alert(self, entry, score, source):
                raise RuntimeError("浮层炸了")

        app.notifier = Boom()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="AAAA000000000001", name="坏人"))
        self.drain()                                 # 不该抛出去
        self.assertEqual(app.db.count_seen(), 1)


# ==========================================================================
class TestSeenRecording(TempCase):
    def test_name_arrives_late(self):
        """名册里的名字往往比 peer 晚几秒 —— 先记 peer，后来把名字补上。"""
        app = self.build_app()
        log = self.log()
        log.write(make_line("file_start", 1, self_name="我"),
                  make_line("join", 2, peer="AAAA000000000001", name=None))
        self.drain(first=True)
        self.assertEqual(app.db.get_seen("AAAA000000000001")["name_last"], "")
        log.write(make_line("update", 3, peer="AAAA000000000001", name="补上的名字"))
        self.drain()
        row = app.db.get_seen("AAAA000000000001")
        self.assertEqual(row["name_last"], "补上的名字")
        self.assertEqual(row["seen_count"], 1)

    def test_same_game_does_not_inflate_count(self):
        app = self.build_app()
        log = self.log()
        log.write(make_line("file_start", 1, self_name="我"),
                  make_line("join", 2, peer="AAAA000000000001", name="甲"),
                  make_line("leave", 3, peer="AAAA000000000001", name="甲"),
                  make_line("join", 4, peer="AAAA000000000001", name="甲"))
        self.drain(first=True)
        self.assertEqual(app.db.get_seen("AAAA000000000001")["seen_count"], 1)

    def test_new_game_inflates_count(self):
        app = self.build_app()
        log = self.log()
        log.write(make_line("file_start", 1, self_name="我"),
                  make_line("join", 2, peer="AAAA000000000001", name="甲"))
        self.drain(first=True)
        log.write(make_line("file_start", 2, pid=8888, self_name="我"),
                  make_line("join", 3, pid=8888, peer="AAAA000000000001", name="甲"))
        self.drain()
        self.assertEqual(app.db.get_seen("AAAA000000000001")["seen_count"], 2)

    def test_cjk_name_passes_through_unharmed(self):
        """名字是 UTF-8 原始字节：中文/日文/韩文/俄文/emoji 都必须原样落库。"""
        app = self.build_app()
        names = ["鼠のテスト", "テスト", "한글닉", "Русский", "🎮玩家"]
        lines = [make_line("file_start", 1, self_name="我")]
        for i, name in enumerate(names, start=2):
            lines.append(make_line("join", i, peer=f"AAAA00000000000{i}", name=name))
        self.log().write(*lines)
        self.drain(first=True)
        got = sorted(r["name_last"] for r in app.db.get_recent_seen(days=0))
        self.assertEqual(got, sorted(names))


# ==========================================================================
class TestLogRobustness(TempCase):
    def test_half_line_is_not_consumed(self):
        """插件正在写的时候只能读到半截 —— 一个字都不能消费。"""
        app = self.build_app()
        log = self.log()
        log.write(make_line("file_start", 1, self_name="我"))
        self.drain(first=True)
        offset = app.watcher.snapshot()["offset"]

        log.write_raw('{"ev": "jo')                   # 半行
        self.drain()
        self.assertEqual(app.watcher.snapshot()["offset"], offset)  # 偏移不动
        self.assertEqual(app.db.count_seen(), 0)

        # 半行补全之后必须能被读到（如果当初被消费掉就永远丢了）
        log.write_raw('in", "capture_id": "2", "peer_id_hex": '
                      '"AAAA000000000001", "name": "队友"}' + "\n")
        self.drain()
        self.assertEqual(app.db.count_seen(), 1)
        self.assertEqual(app.watcher.snapshot()["bad_lines"], 0)

    def test_broken_lines_are_counted_not_fatal(self):
        app = self.build_app()
        self.log().write("not json at all", "[]", '{"ev": "join"',
                         make_line("join", 9, peer="AAAA000000000001", name="甲"))
        self.drain(first=True)
        s = app.watcher.snapshot()
        self.assertEqual(s["bad_lines"], 3)
        self.assertEqual(s["lines"], 1)
        self.assertEqual(app.db.count_seen(), 1)

    def test_truncated_file_recovers(self):
        app = self.build_app()
        log = self.log()
        log.write(make_line("file_start", 1, self_name="我"),
                  make_line("join", 2, peer="AAAA000000000001", name="甲"))
        self.drain(first=True)
        self.assertEqual(app.db.count_seen(), 1)
        # 用户手删 / 清空日志：文件变小 → 偏移自愈、从头重读
        log.truncate().write(make_line("join", 3, peer="BBBB000000000002", name="乙"))
        self.drain()
        self.assertEqual(app.db.count_seen(), 2)

    def test_duplicate_lines_are_ignored(self):
        """capture_id 幂等：同一局内偏移被重置后重读同一行不会重复计数。"""
        app = self.build_app()
        log = self.log()
        log.write(make_line("file_start", 1, self_name="我"))
        self.drain(first=True)
        log.write_raw(make_line("join", 7, peer="AAAA000000000001", name="甲") + "\n")
        self.drain()
        self.assertEqual(app.db.count_seen(), 1)
        # 把同一行再写一遍（capture_id 相同）→ 幂等丢掉
        log.write_raw(make_line("join", 7, peer="AAAA000000000001", name="甲") + "\n")
        self.drain()
        self.assertEqual(app.db.count_seen(), 1)
        self.assertEqual(app.watcher.snapshot()["dup_lines"], 1)

    def test_new_game_resets_capture_id_memory(self):
        """插件重启后 capture_id 从头上重数 —— 不能被上一局的记忆吃掉。"""
        app = self.build_app()
        log = self.log()
        log.write(make_line("file_start", 1, self_name="我"),
                  make_line("join", 2, peer="AAAA000000000001", name="甲"))
        self.drain(first=True)
        self.assertEqual(app.db.count_seen(), 1)
        log.write(make_line("file_start", 1, pid=555, self_name="我"),
                  make_line("join", 2, pid=555, peer="BBBB000000000002", name="乙"))
        self.drain()
        self.assertEqual(app.db.count_seen(), 2)
        self.assertEqual(app.watcher.snapshot()["dup_lines"], 0)

    def test_missing_file_is_not_an_error(self):
        app = self.build_app()
        self.drain(first=True)
        self.drain()
        self.assertEqual(app.db.count_seen(), 0)
        self.assertIn("还没出现", app.watcher.status_text())

    def test_plugin_version_is_reported(self):
        app = self.build_app()
        self.log().write(make_line("join", 1, mod="hd2trackerv19.1",
                                   peer="AAAA000000000001", name="甲"))
        self.drain(first=True)
        s = app.watcher.snapshot()
        self.assertEqual(s["plugin"], "hd2trackerv19.1")
        self.assertTrue(s["plugin_outdated"])
        self.assertIn("过旧", app.watcher.status_text())

    def test_current_plugin_is_not_flagged(self):
        app = self.build_app()
        self.log().write(make_line("join", 1, mod="hd2trackerv21.2",
                                   peer="AAAA000000000001", name="甲"))
        self.drain(first=True)
        self.assertFalse(app.watcher.snapshot()["plugin_outdated"])


class TestNameSync(TempCase):
    """局内看到黑名单玩家的 PeerID 时，名字不一样就**主动改名单里的名字**。"""

    def _start(self, log):
        log.write(make_line("file_start", 1, self_name="我"))
        self.drain(first=True)

    def test_join_with_new_name_updates_blacklist(self):
        app = self.build_app()
        eid = app.db.add("他以前叫这个", "备注别动", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="AAAA000000000001",
                            name="他现在叫这个"))
        self.drain()
        row = app.db.get(eid)
        self.assertEqual(row["player_name"], "他现在叫这个")
        self.assertEqual(row["note"], "备注别动")
        self.assertEqual(app.db.get_count(), 1)          # 没有多出一条

    def test_alert_shows_the_new_name(self):
        """改名在前、告警在后 —— 弹出来的提示里必须是新名字。"""
        app = self.build_app()
        app.db.add("旧名字", "", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="AAAA000000000001", name="新名字"))
        self.drain()
        self.assertEqual([a["name"] for a in app.notifier.alerts], ["新名字"])

    def test_update_event_syncs_name_without_alert(self):
        """名字晚几秒才从名册读到（update）：同步名字，但不告警。"""
        app = self.build_app()
        app.db.add("旧名字", "", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("update", 2, peer="AAAA000000000001",
                            name="补上的名字"))
        self.drain()
        self.assertEqual(app.notifier.alerts, [])
        self.assertEqual(app.db.find_by_peer_id("AAAA000000000001")["player_name"],
                         "补上的名字")

    def test_squad_snapshot_syncs_name(self):
        app = self.build_app()
        app.db.add("旧名字", "", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("squad", 2, squad="AAAA000000000001=名册里的名字"))
        self.drain()
        self.assertEqual(app.db.find_by_peer_id("AAAA000000000001")["player_name"],
                         "名册里的名字")

    def test_empty_name_does_not_wipe_blacklist_name(self):
        """名册还没填好时 name 是 null —— 绝不能把名单里的名字清成空。"""
        app = self.build_app()
        app.db.add("有用的名字", "", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="AAAA000000000001", name=None),
                  make_line("update", 3, peer="AAAA000000000001", name="   "))
        self.drain()
        self.assertEqual(app.db.find_by_peer_id("AAAA000000000001")["player_name"],
                         "有用的名字")

    def test_unlisted_peer_is_not_touched(self):
        app = self.build_app()
        app.db.add("别人", "", peer_id="AAAAAAAAAAAAAAAA")   # 名单里是另一个 ID
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="AAAA000000000001", name="随便"))
        self.drain()
        self.assertEqual(app.db.get_all()[0]["player_name"], "别人")
        self.assertEqual(app.db.get_count(), 1)

    def test_cooldown_blocks_flapping_renames(self):
        """名册抖动导致的反复改名要被冷却挡住（同一人 5 秒内最多一次）。"""
        app = self.build_app()
        app.db.add("初始名字", "", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="AAAA000000000001", name="第一次改"))
        self.drain()
        log.write(make_line("update", 3, peer="AAAA000000000001", name="抖动改"))
        self.drain()
        log.write(make_line("update", 4, peer="AAAA000000000001", name="又抖动"))
        self.drain()
        self.assertEqual(app.db.find_by_peer_id("AAAA000000000001")["player_name"],
                         "第一次改")

    def test_rename_makes_matcher_use_new_name(self):
        """改完名字匹配器要重新建索引，否则提示里还是旧名字。"""
        app = self.build_app()
        eid = app.db.add("旧名字", "", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="AAAA000000000001", name="新名字"))
        self.drain()
        hit = app.matcher.check_peer("AAAA000000000001")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0]["id"], eid)
        self.assertEqual(hit[0]["player_name"], "新名字")

    def test_renamed_away_name_lands_in_prev_names(self):
        """自动改名时旧名字要进「曾用名」——这是名单上能看见的历史。"""
        app = self.build_app()
        eid = app.db.add("他以前的名字", "备注", peer_id="AAAA000000000001")
        app.matcher.reload()
        log = self.log()
        self._start(log)
        log.write(make_line("join", 2, peer="AAAA000000000001",
                            name="他现在的名字"))
        self.drain()
        row = app.db.get(eid)
        self.assertEqual(row["player_name"], "他现在的名字")
        self.assertIn("他以前的名字",
                      app.db.get(eid)["prev_names"])


# ==========================================================================
class TestIgnoreList(TempCase):
    """忽略名单必须同时管住两件事：不提醒、也不进「最近遇到」。"""

    def test_ignored_peer_does_not_alert(self):
        app = self.build_app()
        app.db.add("坏人", "", peer_id="AAAA000000000001")
        app.db.ignore_peer("AAAA000000000001", "坏人")
        app.matcher.reload()
        log = self.log()
        log.write(make_line("file_start", 1, self_name="我"))
        self.drain(first=True)
        log.write(make_line("join", 2, peer="AAAA000000000001", name="坏人"))
        self.drain()
        self.assertEqual(app.notifier.alerts, [])

    def test_ignored_peer_is_not_recorded_as_seen(self):
        app = self.build_app()
        app.db.ignore_peer("AAAA000000000001", "甲")
        log = self.log()
        log.write(make_line("file_start", 1, self_name="我"))
        self.drain(first=True)
        log.write(make_line("join", 2, peer="AAAA000000000001", name="甲"))
        self.drain()
        self.assertEqual(app.db.count_seen(), 0)

    def test_ignored_peer_skipped_in_squad_snapshot(self):
        app = self.build_app()
        app.db.ignore_peer("AAAA000000000001", "甲")
        log = self.log()
        log.write(make_line("file_start", 1, self_name="我"))
        self.drain(first=True)
        log.write(make_line("squad", 2, squad="AAAA000000000001=甲;BBBB000000000002=乙"))
        self.drain()
        self.assertEqual(app.db.count_seen(), 1)          # 只记了没被忽略的那个

    def test_unignore_restores_recording(self):
        app = self.build_app()
        app.db.ignore_peer("AAAA000000000001", "甲")
        app.db.unignore_peer("AAAA000000000001")
        log = self.log()
        log.write(make_line("file_start", 1, self_name="我"))
        self.drain(first=True)
        log.write(make_line("join", 2, peer="AAAA000000000001", name="甲"))
        self.drain()
        self.assertEqual(app.db.count_seen(), 1)


# ==========================================================================
class TestPause(TempCase):
    def test_pause_and_resume(self):
        app = self.build_app()
        self.log().write(make_line("file_start", 1, self_name="我"))
        app.on_pause(True)
        self.assertTrue(app.paused)
        self.assertFalse(app.watcher.running)
        app.on_pause(False)
        self.assertFalse(app.paused)
        self.assertTrue(app.watcher.running)
        app.watcher.stop()
        self.assertFalse(app.watcher.running)

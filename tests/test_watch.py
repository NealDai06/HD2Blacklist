# -*- coding: utf-8 -*-
"""app/watch 的单元验收：字节偏移尾随 + 行流状态机 + 后台线程包装。

这一层是 v2 的**新代码**，全是"踩到就会静默丢数据"的地方，所以测得细一点：
半行、截断、坏字节、capture_id 跨局重数、快照 vs 事件、自己排除自己。

运行： python -m unittest tests.test_watch
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import config  # noqa: E402
from app.watch.log_tail import LogTail  # noqa: E402
from app.watch.state import Event, LogState, parse_squad, plugin_is_outdated  # noqa: E402
from app.watch.watcher import PlayerLogWatcher  # noqa: E402

TMP_ROOT = os.path.join(_ROOT, ".test_tmp")


def rec(ev, cid, **kw) -> dict:
    """造一条记录（和插件写的字段一致）。"""
    out = {
        "v": 2, "mod": kw.pop("mod", "hd2trackerv21.2"), "ev": ev,
        "capture_id": str(cid), "ts_local": kw.pop("ts_local", "2026-01-01 08:00:00"),
        "game_pid": kw.pop("pid", 4321), "peer_id_hex": kw.pop("peer", None),
        "is_self": kw.pop("self", False), "name": kw.pop("name", None),
        "squad": kw.pop("squad", None), "self_name": kw.pop("self_name", None),
        "note": kw.pop("note", None),
        "trigger": kw.pop("trigger", "test"),
        "active_peers": kw.pop("active", 1),
        "peers": kw.pop("peers", None),
    }
    assert not kw, f"rec 收到未用参数: {kw}"
    return out


def line(ev, cid, **kw) -> str:
    return json.dumps(rec(ev, cid, **kw), ensure_ascii=False)


class TempCase(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.tmp = os.path.join(TMP_ROOT, self.__class__.__name__,
                                self._testMethodName)
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *p):
        return os.path.join(self.tmp, *p)

    def write(self, text, name="playerLog.txt"):
        path = self.path(name)
        with open(path, "a", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def log(self, *lines, name="playerLog.txt"):
        return self.write("".join(l + "\n" for l in lines), name)


# ==========================================================================
class TestLogTail(TempCase):
    def test_missing_file_is_quiet(self):
        tail = LogTail(self.path("nope.txt"))
        self.assertFalse(tail.exists)
        self.assertEqual(tail.poll(), [])
        self.assertEqual(tail.offset, 0)

    def test_empty_path_is_quiet(self):
        tail = LogTail("")
        self.assertEqual(tail.poll(), [])
        self.assertEqual(tail.start_at_tail(), [])

    def test_reads_complete_lines(self):
        path = self.log('{"a":1}', '{"a":2}')
        tail = LogTail(path)
        self.assertEqual(tail.poll(), ['{"a":1}', '{"a":2}'])
        self.assertEqual(tail.poll(), [])              # 没有新内容
        self.write('{"a":3}\n')
        self.assertEqual(tail.poll(), ['{"a":3}'])

    def test_half_line_is_not_consumed(self):
        path = self.write('{"a":1}\n{"a"')
        tail = LogTail(path)
        self.assertEqual(tail.poll(), ['{"a":1}'])
        before = tail.offset
        self.assertEqual(tail.poll(), [])              # 半行还在等
        self.assertEqual(tail.offset, before)
        self.write(':2}\n')
        self.assertEqual(tail.poll(), ['{"a":2}'])

    def test_crlf_survives(self):
        """插件在 Windows 上写的是 CRLF；行尾的 \\r 不能污染 JSON。"""
        path = self.write('{"a":1}\r\n{"a":2}\r\n')
        tail = LogTail(path)
        self.assertEqual([json.loads(t) for t in tail.poll()],
                         [{"a": 1}, {"a": 2}])

    def test_truncation_resets_offset(self):
        path = self.log('{"a":1}', '{"a":2}', '{"a":3}')
        tail = LogTail(path)
        self.assertEqual(len(tail.poll()), 3)
        self.write("", name="playerLog.txt")
        open(path, "w", encoding="utf-8", newline="").close()   # 清空
        self.write('{"b":1}\n')
        self.assertEqual(tail.poll(), ['{"b":1}'])

    def test_blank_lines_are_dropped(self):
        path = self.write("\n\n{\"a\":1}\n\n")
        self.assertEqual(LogTail(path).poll(), ['{"a":1}'])

    def test_max_lines_keeps_the_newest(self):
        path = self.log(*[f'{{"n":{i}}}' for i in range(50)])
        tail = LogTail(path)
        got = tail.poll(max_lines=10)
        self.assertEqual(len(got), 10)
        self.assertEqual(got[-1], '{"n":49}')
        self.assertEqual(tail.skipped_lines, 40)

    def test_start_at_tail_does_not_replay_history(self):
        path = self.log(*[f'{{"n":{i}}}' for i in range(200)])
        tail = LogTail(path)
        got = tail.start_at_tail(max_lines=5)
        self.assertEqual(got[-1], '{"n":199}')
        self.assertEqual(tail.offset, os.path.getsize(path))   # 顶到末尾
        self.assertEqual(tail.poll(), [])                      # 不重放
        self.write('{"n":200}\n')
        self.assertEqual(tail.poll(), ['{"n":200}'])

    def test_start_at_tail_drops_partial_first_line(self):
        """往回读的那一段，第一行多半是被截断的半个 JSON —— 必须丢掉。"""
        path = self.log(*[f'{{"n":{i}}}' for i in range(200)])
        tail = LogTail(path)
        got = tail.start_at_tail(max_lines=100, back_bytes=400)
        for text in got:
            json.loads(text)                       # 每一行都必须是完整 JSON
        self.assertNotEqual(tail.offset, 0)

    def test_size_of_unreadable_path(self):
        self.assertEqual(LogTail(self.path("nope")).size(), -1)


# ==========================================================================
class TestHelpers(unittest.TestCase):
    def test_parse_squad(self):
        self.assertEqual(parse_squad(""), [])
        self.assertEqual(parse_squad(None), [])
        self.assertEqual(
            parse_squad("AAAA000000000001=甲;BBBB000000000002=乙"),
            [("AAAA000000000001", "甲"), ("BBBB000000000002", "乙")])
        # 名字里有等号：只按第一个 = 切
        self.assertEqual(parse_squad("AAAA000000000001=a=b"),
                         [("AAAA000000000001", "a=b")])
        # 坏 id / 空片段直接丢
        self.assertEqual(parse_squad("zz=名;;=无名"), [])

    def test_plugin_is_outdated(self):
        self.assertFalse(plugin_is_outdated("hd2trackerv21.2"))
        self.assertFalse(plugin_is_outdated("hd2trackerv21.20"))
        self.assertFalse(plugin_is_outdated("hd2trackerv22.0"))
        self.assertTrue(plugin_is_outdated("hd2trackerv21.1"))
        self.assertTrue(plugin_is_outdated("hd2trackerv20.0"))
        self.assertTrue(plugin_is_outdated("hd2trackerv19.1"))
        # 拿不到版本号时不吓唬用户
        self.assertFalse(plugin_is_outdated(""))
        self.assertFalse(plugin_is_outdated("hd2trackerv"))


# ==========================================================================
class TestLogState(TempCase):
    def setUp(self):
        super().setUp()
        self.st = LogState()

    def feed(self, *records):
        return self.st.feed_lines([json.dumps(r, ensure_ascii=False)
                                   for r in records])

    def test_parses_and_counts(self):
        events = self.feed(rec("file_start", 1, self_name="我"),
                           rec("join", 2, peer="AAAA000000000001", name="甲"))
        self.assertEqual([e.kind for e in events], ["file_start", "join"])
        self.assertEqual(self.st.lines, 2)
        self.assertEqual(self.st.counts, {"file_start": 1, "join": 1})
        self.assertEqual(self.st.self_name, "我")
        self.assertEqual(self.st.game_pid, 4321)

    def test_bad_lines_are_counted(self):
        self.st.feed_lines(["", "   ", "not json", "[]", '{"ev": ""}'])
        self.assertEqual(self.st.lines, 0)
        self.assertEqual(self.st.bad_lines, 3)          # 空行不算数

    def test_capture_id_dedup(self):
        self.feed(rec("join", 1, peer="AAAA000000000001"))
        self.feed(rec("join", 1, peer="AAAA000000000001"))
        self.assertEqual(self.st.lines, 1)
        self.assertEqual(self.st.dup_lines, 1)

    def test_missing_capture_id_is_not_deduped(self):
        """老插件没有 capture_id：不去重，但也不能崩。"""
        self.st.feed_lines(['{"ev":"join","peer_id_hex":"AAAA000000000001"}',
                            '{"ev":"join","peer_id_hex":"AAAA000000000001"}'])
        self.assertEqual(self.st.lines, 2)
        self.assertEqual(self.st.dup_lines, 0)

    def test_capture_id_memory_resets_on_file_start(self):
        """跨局：新一局的 capture_id 从头上重数，不能被上一局的记忆吃掉。"""
        self.feed(rec("file_start", 1, self_name="我"),
                  rec("join", 2, peer="AAAA000000000001"))
        self.feed(rec("file_start", 1, pid=555, self_name="我"),
                  rec("join", 2, pid=555, peer="BBBB000000000002"))
        self.assertEqual(self.st.lines, 4)
        self.assertEqual(self.st.dup_lines, 0)

    def test_file_start_is_never_treated_as_duplicate(self):
        """哪怕新一局的 file_start 和上一局用同一个 capture_id，也不能被丢掉。"""
        self.feed(rec("file_start", 1, self_name="我"),
                  rec("join", 2, peer="AAAA000000000001"))
        self.feed(rec("file_start", 1, pid=555, self_name="我"))
        self.assertEqual(self.st.game_pid, 555)
        self.assertEqual(self.st.dup_lines, 0)

    def test_squad_snapshot_replaces_and_excludes_self(self):
        self.feed(rec("file_start", 1, self_name="我自己"),
                  rec("join", 2, peer="CCCC000000000003", self=True, name="我自己"),
                  rec("squad", 3,
                      squad="CCCC000000000003=我自己;AAAA000000000009=队友"))
        self.assertEqual(list(self.st.squad), ["AAAA000000000009"])
        self.assertEqual(self.st.self_ids, {"CCCC000000000003"})
        self.assertTrue(self.st.is_self("CCCC000000000003"))
        self.assertFalse(self.st.is_self("AAAA000000000009"))

    def test_squad_snapshot_is_lossy_on_purpose(self):
        """快照是"现在队里有谁"：新的快照会覆盖旧的（人走了就不在名单里）。"""
        self.feed(rec("squad", 1, squad="AAAA000000000001=甲;BBBB000000000002=乙"))
        self.assertEqual(len(self.st.squad), 2)
        self.feed(rec("squad", 2, squad="AAAA000000000001=甲"))
        self.assertEqual(list(self.st.squad), ["AAAA000000000001"])

    def test_join_leave_update_maintain_squad(self):
        self.feed(rec("join", 1, peer="AAAA000000000001", name=None))
        self.assertEqual(self.st.squad, {"AAAA000000000001": ""})
        self.feed(rec("update", 2, peer="AAAA000000000001", name="补齐的名字"))
        self.assertEqual(self.st.squad["AAAA000000000001"], "补齐的名字")
        self.feed(rec("leave", 3, peer="AAAA000000000001"))
        self.assertEqual(self.st.squad, {})

    def test_update_does_not_add_to_squad(self):
        """update 不是"进队"：只给队里的人补名字，不能把人凭空塞进当前队伍。"""
        self.feed(rec("update", 1, peer="AAAA000000000001", name="甲"))
        self.assertEqual(self.st.squad, {})

    def test_leave_absorbs_and_rejoin_returns(self):
        self.feed(rec("join", 1, peer="AAAA000000000001", name="甲"),
                  rec("leave", 2, peer="AAAA000000000001", name="甲"),
                  rec("join", 3, peer="AAAA000000000001", name="甲"))
        self.assertEqual(list(self.st.squad), ["AAAA000000000001"])

    def test_self_join_is_not_added_to_squad(self):
        self.feed(rec("join", 1, peer="CCCC000000000003", self=True, name="我"))
        self.assertEqual(self.st.squad, {})

    def test_match_boundaries_keep_squad(self):
        """match_start / match_end 只是边界标记，不该把"当前队伍"清空。

        清掉的话，界面上的队伍会空一截（下一个 squad 事件来之前什么都不显示）。
        """
        self.feed(rec("join", 1, peer="AAAA000000000001", name="甲"),
                  rec("match_end", 2, note="本局共记录 1 人"),
                  rec("match_start", 3, peers="AAAA000000000001", active=1))
        self.assertEqual(list(self.st.squad), ["AAAA000000000001"])

    def test_file_start_clears_squad(self):
        self.feed(rec("join", 1, peer="AAAA000000000001", name="甲"),
                  rec("file_start", 2, self_name="我"))
        self.assertEqual(self.st.squad, {})

    def test_squad_list_is_sorted(self):
        self.feed(rec("squad", 1,
                      squad="BBBB000000000002=乙;AAAA000000000001=甲"))
        self.assertEqual([m["peer_id"] for m in self.st.squad_list()],
                         ["AAAA000000000001", "BBBB000000000002"])

    def test_events_expose_raw_record(self):
        self.feed(rec("join", 1, peer="AAAA000000000001", name="甲",
                      trigger="join"))
        ev = self.st.feed_lines([line("join", 2, peer="AAAA000000000001")])[0]
        self.assertEqual(ev.raw["trigger"], "test")
        self.assertTrue(ev.has_person)
        self.assertEqual(ev.kind, "join")
        self.assertEqual(ev.name, "")
        self.assertFalse(ev.is_self)
        self.assertEqual(ev.game_pid, 4321)

    def test_peer_id_is_normalized(self):
        self.feed(rec("join", 1, peer="aaaa000000000001"))
        self.assertEqual(list(self.st.squad), ["AAAA000000000001"])

    def test_unknown_event_kind_is_kept(self):
        """插件以后加了新事件类型：不能崩，也不能把整行丢掉。"""
        events = self.st.feed_lines([line("brand_new", 1)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "brand_new")

    def test_rejects_non_dict_json(self):
        self.st.feed_lines(['[1,2,3]', '"just a string"', "42"])
        self.assertEqual(self.st.bad_lines, 3)

    def test_reset_clears_everything(self):
        self.feed(rec("file_start", 1, self_name="我"),
                  rec("join", 2, peer="AAAA000000000001", name="甲"))
        self.st.reset()
        self.assertEqual(self.st.squad, {})
        self.assertEqual(self.st.lines, 0)
        self.assertEqual(self.st.self_name, "")
        self.assertEqual(self.st.last_event, "")


# ==========================================================================
class TestWatcher(TempCase):
    def test_status_text_when_dir_missing(self):
        w = PlayerLogWatcher(path=self.path("no_such_dir", "x.txt"))
        self.assertIn("找不到插件日志目录", w.status_text())

    def test_status_text_when_file_missing(self):
        w = PlayerLogWatcher(path=self.path("playerLog.txt"))
        self.assertIn("还没出现", w.status_text())

    def test_status_text_reports_progress(self):
        self.log(line("file_start", 1, self_name="我"))
        w = PlayerLogWatcher(path=self.path("playerLog.txt"))
        w.backfill()
        text = w.status_text()
        self.assertIn("已跟踪", text)
        self.assertIn("file_start", text)

    def test_status_text_flags_old_plugin(self):
        self.log(line("join", 1, mod="hd2trackerv19.1", peer="AAAA000000000001"))
        w = PlayerLogWatcher(path=self.path("playerLog.txt"))
        w.backfill()
        self.assertIn("过旧", w.status_text())

    def test_no_path_configured(self):
        w = PlayerLogWatcher(path="")
        self.assertIn("没有拿到插件日志路径", w.status_text())

    def test_poll_once_returns_line_count(self):
        self.log(line("join", 1, peer="AAAA000000000001", name="甲"))
        w = PlayerLogWatcher(path=self.path("playerLog.txt"))
        w.backfill()
        self.assertEqual(w.poll_once(), 0)
        self.log(line("join", 2, peer="BBBB000000000002", name="乙"))
        self.assertEqual(w.poll_once(), 1)

    def test_callback_receives_live_flag(self):
        seen = []
        self.log(line("file_start", 1, self_name="我"))
        w = PlayerLogWatcher(on_events=lambda evs, live: seen.append(
            (live, [e.kind for e in evs])), path=self.path("playerLog.txt"))
        w.backfill()
        self.log(line("join", 2, peer="AAAA000000000001", name="甲"))
        w.poll_once()
        self.assertEqual(seen, [(False, ["file_start"]), (True, ["join"])])

    def test_callback_exception_is_contained(self):
        def boom(events, live):
            raise RuntimeError("上层炸了")

        self.log(line("join", 1, peer="AAAA000000000001"))
        w = PlayerLogWatcher(on_events=boom, path=self.path("playerLog.txt"))
        self.assertEqual(w.backfill(), 1)          # 不抛出去
        self.assertEqual(w.snapshot()["lines"], 1)

    def test_snapshot_shape(self):
        self.log(line("file_start", 1, self_name="我"))
        w = PlayerLogWatcher(path=self.path("playerLog.txt"))
        w.backfill()
        snap = w.snapshot()
        for key in ("path", "exists", "offset", "size", "read_bytes", "running",
                    "live", "error", "lines", "bad_lines", "dup_lines",
                    "plugin", "plugin_outdated", "game_pid", "self_name",
                    "squad", "last_event", "last_event_at"):
            self.assertIn(key, snap)
        self.assertTrue(snap["live"])
        self.assertFalse(snap["running"])          # 没 start() 就没有线程

    def test_start_stop_thread(self):
        self.log(line("file_start", 1, self_name="我"))
        w = PlayerLogWatcher(path=self.path("playerLog.txt"),
                             poll_interval=0.05)
        self.assertTrue(w.start())
        self.assertTrue(w.running)
        self.assertFalse(w.start())                # 重复 start → False
        w.stop()
        self.assertFalse(w.running)

    def test_missing_file_is_not_an_error(self):
        w = PlayerLogWatcher(path=self.path("playerLog.txt"))
        self.assertEqual(w.backfill(), 0)
        self.assertEqual(w.poll_once(), 0)
        self.assertEqual(w.snapshot()["error"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)

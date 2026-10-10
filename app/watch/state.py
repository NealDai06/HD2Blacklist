# -*- coding: utf-8 -*-
"""state.py —— 把 playerLog.txt 的行流解释成「现在队里有谁」和「我遇到过谁」。

这里是**纯逻辑**：不碰文件、不碰线程、不碰界面 —— 喂字符串行进去，吐事件出来。
所以它可以用一份合成日志在测试里反复跑，完全不需要游戏在场。

字段含义见 HD2Tracker 的 docs\\记录字段说明.md。三条最容易搞错的先写在前面：
    * `join` / `leave` 是**事件**（有人进、有人出）
    * `squad` 是**快照**（"现在队里是这几个人"），不是事件 ——
      拿它触发告警，会把"进队就在队里的人"反复报一遍
    * `update` 是同一个人的信息补齐（典型：名字比 peer id 晚几秒才从名册读到）

线程模型：本类**不加锁**，只由 watcher 的工作线程调用；
界面要读状态请走 `PlayerLogWatcher.snapshot()`（那里统一加了锁）。
"""
from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from app.config import (
    EVENT_FILE_START, EVENT_JOIN, EVENT_LEAVE, EVENT_MATCH_END,
    EVENT_MATCH_START, EVENT_SQUAD, EVENT_UPDATE, PLUGIN_MIN_VERSION,
    WATCH_SEEN_IDS,
)
from app.core.database import normalize_peer_id


def parse_squad(text: str) -> list:
    """`PEERID=名字;PEERID=名字` → ``[(peer_id, name), ...]``。

    只按**第一个** `=` 切 —— 玩家名里完全可能有等号。
    """
    out = []
    for part in str(text or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        raw_id, _, name = part.partition("=")
        pid = normalize_peer_id(raw_id)
        if pid:
            out.append((pid, name.strip()))
    return out


#: 版本号必须长成 `...v<数字>[.<数字>...]` 这个样子（插件的命名习惯）。
#: 不用"抓出所有数字"那种粗暴写法 —— 那会把 `hd2trackerv` 里的 "2" 当成主版本号，
#: 于是永远显示"版本过旧"，属于对着用户喊狼来了。
_VER_RE = re.compile(r"v(\d+(?:\.\d+)*)\s*$", re.IGNORECASE)


def _version_key(text: str):
    """把 `hd2trackerv21.2` 抽成可比较的数字元组 (21, 2)；认不出来返回空元组。"""
    match = _VER_RE.search(str(text or "").strip())
    if not match:
        return ()
    return tuple(int(n) for n in match.group(1).split("."))


def plugin_is_outdated(found: str, required: str = PLUGIN_MIN_VERSION) -> bool:
    """插件版本是否过旧。拿不到版本号时一律当作"不过旧"（不吓唬用户）。"""
    a, b = _version_key(found), _version_key(required)
    if not a or not b:
        return False
    return a < b


@dataclass
class Event:
    """一条已经解释好的日志记录。"""

    kind: str                       # join / leave / update / squad / match_start ...
    peer_id: str = ""               # 16 位大写十六进制；空 = 这条不对应具体玩家
    name: str = ""
    is_self: bool = False
    game_pid: Optional[int] = None
    ts_local: str = ""
    note: str = ""
    squad: tuple = ()               # 仅 squad 事件：((peer_id, name), ...)
    raw: dict = field(default_factory=dict)

    @property
    def has_person(self) -> bool:
        return bool(self.peer_id)


class LogState:
    """行流 → 事件 + 当前队伍 + 插件状态。"""

    def __init__(self, dedup_ids: int = WATCH_SEEN_IDS):
        self.dedup_ids = max(16, int(dedup_ids))
        self.reset()

    def reset(self) -> None:
        self.game_pid: Optional[int] = None
        self.game_started_at = ""
        self.self_name = ""
        self.plugin_version = ""
        self.plugin_outdated = False
        self.last_event = ""
        self.last_event_at = ""
        self.squad: dict = {}          # peer_id -> 名字（**不含自己**）
        self.self_ids: set = set()     # 由 is_self 记录学到"哪个 id 是我"
        self.lines = 0                 # 成功解析的行数
        self.bad_lines = 0             # JSON 坏了 / 不是对象的行数
        self.dup_lines = 0             # capture_id 重复（幂等丢弃）的行数
        self.counts: dict = {}         # 事件类型 -> 条数
        self._capture_ids: deque = deque()
        self._capture_set: set = set()

    # ------------------------------------------------------------------ 入口
    def feed_lines(self, lines) -> list:
        """喂一批行，返回这一批里所有可用的 Event（顺序保持）。"""
        out = []
        for line in lines or []:
            ev = self.feed_line(line)
            if ev is not None:
                out.append(ev)
        return out

    def feed_line(self, line: str) -> Optional[Event]:
        text = (line or "").strip()
        if not text:
            return None
        try:
            rec = json.loads(text)
        except (ValueError, TypeError):
            self.bad_lines += 1
            return None
        if not isinstance(rec, dict):
            self.bad_lines += 1
            return None
        return self.feed_record(rec)

    def feed_record(self, rec: dict) -> Optional[Event]:
        kind = str(rec.get("ev") or "").strip()
        if not kind:
            self.bad_lines += 1
            return None

        # 插件每次加载 = 新的 Lua 状态：capture_id 那个计数器**从头上重新数**。
        # 所以 file_start 必须把上一局的去重记忆清掉，而且**要在查重之前清** ——
        # 否则新一局的第一行（file_start 自己，id 多半也是 1）会被上一局的记忆
        # 当成重复丢掉，于是"清理"永远不执行，整局的前几行跟着一起被丢。
        if kind == EVENT_FILE_START:
            self._capture_ids.clear()
            self._capture_set.clear()

        # capture_id 幂等：同一行被读到第二次（例如偏移被重置后重读）不再处理
        cid = str(rec.get("capture_id") or "")
        if not self._remember(cid):
            self.dup_lines += 1
            return None

        mod = str(rec.get("mod") or "").strip()
        if mod:
            self.plugin_version = mod
            self.plugin_outdated = plugin_is_outdated(mod)

        pid_raw = rec.get("peer_id_hex")
        peer = normalize_peer_id(pid_raw) if pid_raw else ""
        is_self = bool(rec.get("is_self"))
        name = str(rec.get("name") or "").strip()
        game_pid = _as_int(rec.get("game_pid"))

        if is_self and peer:
            self.self_ids.add(peer)          # 学到"哪个 id 是我"
        if game_pid is not None and game_pid != self.game_pid:
            self.game_pid = game_pid

        ev = Event(kind=kind, peer_id=peer, name=name, is_self=is_self,
                   game_pid=game_pid if game_pid is not None else self.game_pid,
                   ts_local=str(rec.get("ts_local") or "").strip(),
                   note=str(rec.get("note") or ""), raw=rec)

        if kind == EVENT_FILE_START:
            # 插件每次加载 = 游戏每次启动：上一次运行留下的队伍作废。
            # （capture_id 的记忆已经在上面查重之前清掉了。）
            self.self_name = str(rec.get("self_name") or "").strip()
            self.game_started_at = ev.ts_local
            self.squad.clear()

        elif kind == EVENT_SQUAD:
            # 快照：整队覆盖。名册里**包含你自己**，所以用 self_ids 把他剔掉。
            members = parse_squad(str(rec.get("squad") or ""))
            ev.squad = tuple(members)
            self.squad = {p: n for p, n in members if p not in self.self_ids}

        elif kind == EVENT_JOIN:
            if peer and not is_self:
                self.squad[peer] = name

        elif kind == EVENT_LEAVE:
            self.squad.pop(peer, None)

        elif kind == EVENT_UPDATE:
            # 只补名字，不因为一条 update 就把人塞回"当前队伍"里
            if peer and name and peer in self.squad:
                self.squad[peer] = name

        elif kind in (EVENT_MATCH_START, EVENT_MATCH_END):
            # 对局边界只当标记用。**不清理 squad** ——
            # match_start 之后不一定立刻有新的 squad 事件，
            # 清掉会让界面上的"当前队伍"空一截。
            pass

        self.lines += 1
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.last_event = kind
        self.last_event_at = ev.ts_local or self.last_event_at
        return ev

    # ------------------------------------------------------------------ 查询
    def squad_list(self) -> list:
        """当前队伍（不含自己），按 peer_id 排序保证显示稳定。"""
        return [{"peer_id": p, "name": self.squad.get(p, "")}
                for p in sorted(self.squad)]

    def is_self(self, peer_id: str) -> bool:
        return bool(peer_id) and peer_id in self.self_ids

    # ------------------------------------------------------------------ 内部
    def _remember(self, capture_id: str) -> bool:
        """记一个 capture_id；已经见过就返回 False。"""
        if not capture_id:
            return True                      # 老插件没有这个字段：不去重
        if capture_id in self._capture_set:
            return False
        self._capture_set.add(capture_id)
        self._capture_ids.append(capture_id)
        while len(self._capture_ids) > self.dedup_ids:
            old = self._capture_ids.popleft()
            self._capture_set.discard(old)
        return True


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

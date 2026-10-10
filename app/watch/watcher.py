# -*- coding: utf-8 -*-
"""watcher.py —— 后台线程：尾随插件日志 playerLog.txt，把事件交给上层。

职责边界（很重要）：
    * 线程里**只做** 文件 IO + JSON 解析 + 状态维护，绝不碰 tkinter
    * 事件通过 `on_events(events, live)` 回调交出去；默认在**工作线程**里被调用，
      上层负责把它投进 queue.Queue，再由主线程 root.after 消费
    * 每轮之间用 `Event.wait(interval)` 而不是 sleep —— stop() 能立刻打断
    * `live=False` 的那一批是首启的补读：**只记事实，不弹告警**，
      否则应用一打开就会把历史里的 join 事件炸成一堆通知
"""
from __future__ import annotations

import os
import threading

from app.config import (
    PLUGIN_MIN_VERSION, WATCH_POLL_INTERVAL, WATCH_THREAD_PRIORITY,
    get_logger, player_log_path,
)
from app.core.priority import restore_thread_priority, set_current_thread_priority
from app.watch.log_tail import LogTail
from app.watch.state import LogState


def _kb(n: int) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


class PlayerLogWatcher:
    """尾随插件日志的工作线程 + 一份线程安全的状态快照。"""

    def __init__(self, on_events=None, on_status=None, path=None,
                 poll_interval=None, state=None, log=None):
        self.path = player_log_path() if path is None else (path or "")
        self.poll_interval = float(
            WATCH_POLL_INTERVAL if poll_interval is None else poll_interval)
        self.on_events = on_events
        self.on_status = on_status
        self.log = log or get_logger("watch")
        self.state = state if state is not None else LogState()
        self.tail = LogTail(self.path)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._error = ""
        self._first_done = False
        self._emitted = 0

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        """起线程。已经在跑就返回 False。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="PlayerLogWatcher", daemon=True)
            self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> None:
        """请线程收工并等它退出。"""
        self._stop.set()
        self.join(timeout)

    def join(self, timeout: float = 3.0) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def poll_once(self) -> int:
        """同步跑一轮（不做首启补读），返回处理过的行数。给测试与 --watch-debug 用。"""
        return self._safe_round(first=False)

    def backfill(self, live: bool = False) -> int:
        """同步跑首启补读那一轮，返回处理过的行数。"""
        n = self._safe_round(first=True)
        self._first_done = True
        return n

    # ------------------------------------------------------------------ 线程体
    def _run(self) -> None:
        old = set_current_thread_priority(WATCH_THREAD_PRIORITY)
        try:
            self.log.info("开始尾随插件日志：%s", self.path or "(路径为空)")
            self._safe_round(first=True)
            self._first_done = True
            while not self._stop.is_set():
                self._safe_round(first=False)
                self._stop.wait(self.poll_interval)
        except Exception:                                # noqa: BLE001
            self.log.exception("尾随线程异常退出")
        finally:
            restore_thread_priority(old)
            self.log.info("已停止尾随插件日志（读到 %s）",
                          _kb(self.tail.read_bytes))

    def _safe_round(self, first: bool) -> int:
        """一轮 = 读一批行 + 解释 + 回调。任何异常都不许把线程带走。"""
        try:
            lines = (self.tail.start_at_tail() if first else self.tail.poll())
            self._error = ""
        except Exception as e:                           # noqa: BLE001
            self._error = f"读取日志失败：{type(e).__name__}: {e}"
            self.log.exception("读取日志失败")
            return 0
        if not lines:
            return 0
        try:
            with self._lock:
                events = self.state.feed_lines(lines)
        except Exception as e:                           # noqa: BLE001
            self._error = f"解析日志失败：{type(e).__name__}: {e}"
            self.log.exception("解析日志失败")
            return len(lines)
        if events:
            self._emitted += len(events)
            self._emit(events, live=not first)
        return len(lines)

    def _emit(self, events, live: bool) -> None:
        if self.on_events is None:
            return
        try:
            self.on_events(events, live)
        except Exception:                                # noqa: BLE001
            self.log.exception("事件回调出错")

    # ------------------------------------------------------------------ 状态
    def snapshot(self) -> dict:
        """给界面用的状态快照（线程安全；界面读的**只有**这一个入口）。"""
        with self._lock:
            st = self.state
            size = self.tail.size()
            directory = os.path.dirname(self.path) if self.path else ""
            return {
                "path": self.path,
                "dir": directory,
                "dir_exists": bool(directory) and os.path.isdir(directory),
                "exists": size >= 0,
                "size": max(size, 0),
                "offset": self.tail.offset,
                "read_bytes": self.tail.read_bytes,
                "skipped_lines": self.tail.skipped_lines,
                "running": self.running,
                "live": self._first_done,
                "error": self._error,
                "lines": st.lines,
                "bad_lines": st.bad_lines,
                "dup_lines": st.dup_lines,
                "emitted": self._emitted,
                "counts": dict(st.counts),
                "plugin": st.plugin_version,
                "plugin_outdated": st.plugin_outdated,
                "plugin_required": PLUGIN_MIN_VERSION,
                "game_pid": st.game_pid,
                "self_name": st.self_name,
                "self_ids": sorted(st.self_ids),
                "squad": st.squad_list(),
                "last_event": st.last_event,
                "last_event_at": st.last_event_at,
            }

    def status_text(self) -> str:
        """一句话人话状态，界面直接拿去显示。"""
        s = self.snapshot()
        if not s["path"]:
            return "没有拿到插件日志路径（LOCALAPPDATA 读不到）"
        if not s["dir_exists"]:
            return (f"找不到插件日志目录：{s['dir']}"
                    f"　—— 游戏里装 HD2Tracker 了吗？")
        if not s["exists"]:
            return "日志文件还没出现（插件没加载，或者还没进过任何一局）"
        if s["plugin_outdated"]:
            return (f"插件版本过旧（{s['plugin'] or '未知'}）：字段可能对不上，"
                    f"需要 {s['plugin_required']}")
        parts = [f"已跟踪 playerLog.txt（读到 {_kb(s['read_bytes'])}）"]
        if s["last_event"]:
            parts.append(f"最后事件 {s['last_event']} @ {s['last_event_at'] or '?'}")
        parts.append(f"当前队伍 {len(s['squad'])} 人")
        if s["bad_lines"]:
            parts.append(f"{s['bad_lines']} 行解析失败")
        if s["error"]:
            parts.append(s["error"])
        return "；".join(parts)

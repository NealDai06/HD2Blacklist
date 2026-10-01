# -*- coding: utf-8 -*-
"""scan_scheduler.py —— 调度中枢。

职责（不负责扫描时序，时序由 ScanSession / ChatScanner 自己管理）：
    * handle_hits              批量命中处理（首选接口）
    * handle_hit               单条命中处理（兼容接口，内部委托 handle_hits）
    * match_and_notify_batch   名字列表批量匹配（ScanSession 调用）
    * process_text             整段文本匹配（聊天框按需扫描调用）
    * 命中去重：HIT_DEDUP_WINDOW 秒内同一玩家只计数一次、只提示一次
    * 会话注册 / 注销、同一时刻只允许一个活跃会话
    * 保存证据截图、写日志
    * 通过 on_encounter 回调把结果推给 GUI（GUI 侧用 queue.Queue 转主线程）
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime

from app.config import (EVIDENCE_DIR, EVIDENCE_JPEG_QUALITY, EVIDENCE_MAX_FILES,
                    HIT_DEDUP_WINDOW, get_logger)

_SAFE_NAME_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")


class ScanScheduler:
    def __init__(self, db, matcher, notifier, ocr, capture,
                 region_config, on_encounter=None, evidence_enabled: bool = True):
        self.db = db
        self.matcher = matcher
        self.notifier = notifier
        self.ocr = ocr
        self.capture = capture
        self.region_config = region_config
        self.match_lock = threading.Lock()
        self.game_active = threading.Event()
        self._paused = threading.Event()
        self.on_encounter = on_encounter
        #: 「给用户看的动态」回调：on_event(text, level)。GUI 侧负责转到主线程。
        #: 与 log() 分开是因为日志里有大量技术细节，而这里只推用户看得懂的句子。
        self.on_event = None
        self.evidence_enabled = evidence_enabled

        self.logger = get_logger("scheduler")
        self._recent_hits = {}
        self._recent_lock = threading.Lock()
        self._hit_count_lock = threading.Lock()
        self.session_hit_count = 0
        #: 最近一批命中的统计（界面播报用）：
        #: {"total": 命中条数, "deduped": 被去重跳过, "recorded": 真正记录, "names": [...]}
        self._stats_lock = threading.Lock()
        self.last_hit_stats = {"total": 0, "deduped": 0, "recorded": 0, "names": []}

        self._active_session = None
        self._active_lock = threading.Lock()
        self._evidence_counter = 0
        self._evidence_lock = threading.Lock()
        self._shutdown = False

    # ------------------------------------------------------------ 游戏状态
    def set_game_active(self, active: bool) -> None:
        if active:
            self.game_active.set()
            self.report("游戏已启动，监视已激活")
        else:
            self.game_active.clear()
            self.stop_session()
            self.report("游戏已退出，监视已暂停")

    def set_paused(self, paused: bool) -> None:
        """用户手动暂停监控（不影响 game_active 状态）。"""
        if paused:
            self._paused.set()
            self.stop_session()
            self.report("监控已被用户暂停（菜单/HUD 不再扫描）")
        else:
            self._paused.clear()
            # 游戏没在跑的时候别喊"监控已恢复" —— 那会让人以为监控被打开了，
            # 其实只是取消了一个用户开关（实测反馈：终端一直跳"监控已恢复"）。
            if self.game_active.is_set():
                self.report("监控已恢复")
            else:
                self.report("已取消用户暂停（当前游戏未运行，不会扫描）")

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def is_active(self) -> bool:
        """监视是否该干活：游戏在跑且用户没暂停。"""
        return self.game_active.is_set() and not self._paused.is_set()

    # ------------------------------------------------------------ 会话管理
    def session_busy(self) -> bool:
        with self._active_lock:
            s = self._active_session
            return bool(s is not None and s.alive)

    def register_session(self, session) -> None:
        with self._active_lock:
            self._active_session = session

    def unregister_session(self, session) -> None:
        with self._active_lock:
            if self._active_session is session:
                self._active_session = None

    def start_session(self, region_key: str, source: str, params: dict):
        """启动一个新会话；若已有会话在跑，先终止它（同一时刻最多一个）。"""
        from app.scanning.scan_session import ScanSession       # 局部导入避免循环依赖

        if self._shutdown:
            return None
        old = None
        with self._active_lock:
            old = self._active_session
        if old is not None and old.alive:
            self.log(f"[Scheduler] 终止旧会话 source={old.source} → 启动 {source}")
            old.stop(wait=True, timeout=3.0)
        session = ScanSession(self, region_key, source, params)
        session.start()
        return session

    def stop_session(self, wait: bool = True) -> None:
        with self._active_lock:
            s = self._active_session
        if s is not None:
            s.stop(wait=wait)

    # ------------------------------------------------------------ 命中去重
    def _is_recently_hit(self, entry_id) -> bool:
        """同一玩家在 HIT_DEDUP_WINDOW 秒内是否已经计过数。

        去重窗口内**既不记入 encounters，也不弹提示** —— 防止连点扫描
        按钮或反复触发导致重复计数。
        """
        now = time.time()
        with self._recent_lock:
            last = self._recent_hits.get(entry_id, 0)
            if now - last < HIT_DEDUP_WINDOW:
                return True
            # 顺手清理过期记录，避免字典无限增长
            if len(self._recent_hits) > 256:
                self._recent_hits = {
                    k: v for k, v in self._recent_hits.items()
                    if now - v < HIT_DEDUP_WINDOW
                }
            return False

    def _mark_hit(self, entry_id) -> None:
        with self._recent_lock:
            self._recent_hits[entry_id] = time.time()

    def clear_dedup_cache(self) -> None:
        """清空去重缓存（手动调试 / 需要立刻重新计数时使用）。"""
        with self._recent_lock:
            self._recent_hits.clear()

    def dedup_cache_size(self) -> int:
        with self._recent_lock:
            return len(self._recent_hits)

    # ------------------------------------------------------------ 命中处理
    def _process_hits(self, hits, source, img, notify_event: bool = True) -> list:
        """命中处理流水线，返回 [(entry, score, updated), ...]。

        顺序严格为：
            1. 过滤去重（窗口内的直接跳过并写日志）
            2. 标记去重（**先全部过滤完再统一标记**，避免
               "一部分标记了、一部分没标记"导致后续批量行为不一致）
            3. 逐个写证据 + 原子更新 DB + 通知 GUI
            4. 批量弹提示：多个通知栏，只播一次音效

        ``notify_event=False``：调用方会自己播报完整结果（聊天框按需扫描就是
        这样），此时不再往界面动态流里塞重复的行。统计照样记录。
        """
        if not hits:
            self._set_hit_stats(0, 0, 0, [])
            return []

        # ---- 1. 过滤去重 ----
        passed = []
        deduped_names = []
        for item in hits:
            entry = item[0]
            score = item[1]
            name_seen = item[2] if len(item) > 2 else ""
            entry_id = entry.get("id")
            if entry_id is None:
                continue
            who = name_seen or entry.get("player_name") or "?"
            if self._is_recently_hit(entry_id):
                # 去重也要说清楚是谁：旧日志只有 entry_id，用户看不懂"为什么没反应"
                self.log(f"[Hit] 去重跳过 {who} entry_id={entry_id} source={source}")
                if notify_event:
                    self._emit(
                        f"命中 {who}，但 {HIT_DEDUP_WINDOW} 秒内刚提示过 → "
                        f"本次跳过（不重复计数、不重复弹窗）",
                        "dedup",
                    )
                deduped_names.append(str(who))
                continue
            passed.append((entry, score, name_seen))

        if not passed:
            self._set_hit_stats(len(hits), len(deduped_names), 0, deduped_names)
            return []

        # ---- 2. 标记去重 ----
        for entry, _score, _name in passed:
            self._mark_hit(entry["id"])

        # ---- 3. 逐个更新 DB + 通知 GUI ----
        updated_list = []
        for entry, score, name_seen in passed:
            try:
                evidence_path = self._save_evidence(img, entry, score)
                updated = self.db.record_encounter(
                    entry_id=entry["id"], score=score, source=source,
                    name_seen=name_seen, screenshot_path=evidence_path,
                )
            except Exception as e:                       # noqa: BLE001
                # 单条失败不能拖垮整批
                self.logger.warning("[Scheduler] 命中写入失败 id=%s: %s",
                                    entry.get("id"), e)
                continue

            with self._hit_count_lock:
                self.session_hit_count += 1

            self.log(
                f"[Hit] {entry.get('player_name')} "
                f"score={float(score or 0):.0f} source={source} "
                f"今日第{updated.get('today_count', 1)}次"
            )

            updated_list.append((entry, score, updated))

            if self.on_encounter:
                try:
                    self.on_encounter({
                        "entry_id": entry["id"],
                        "player_name": entry.get("player_name"),
                        "note": entry.get("note") or "",
                        "seen_at": updated.get("seen_at"),
                        "today_count": updated.get("today_count", 1),
                        "score": score,
                        "source": source,
                    })
                except Exception as e:                   # noqa: BLE001
                    self.logger.warning("[Scheduler] GUI 回调异常: %s", e)

        recorded_names = [
            str(e.get("player_name") or "?") for e, _s, _u in updated_list
        ]
        self._set_hit_stats(len(hits), len(deduped_names), len(updated_list),
                            recorded_names + deduped_names)

        if not updated_list:
            # 命中了但一条都没写进库（例如数据库被占用）—— 这同样要告诉用户，
            # 否则界面上就是"按了 F8 什么都没发生"。
            self.logger.warning("[Hit] 命中 %d 条但全部写入失败", len(hits))
            if notify_event:
                self._emit(f"命中 {len(hits)} 条，但全部写入失败（详见日志）",
                           "warn")
            return []

        # 一次扫描命中多个玩家时，把**整批名字**打在一行里。
        # 之前只有逐条日志，用户很难确认"到底命中了谁、是不是只播了一个"。
        names = "、".join(recorded_names)
        if len(updated_list) > 1:
            self.log(f"[Hit] 本批共命中 {len(updated_list)} 名黑名单玩家：{names}")
            if notify_event:
                self._emit(f"本批共命中 {len(updated_list)} 名黑名单玩家：{names}",
                           "hit")
        elif notify_event:
            self._emit(f"命中黑名单玩家 {names}（已弹提示 + 已记录）", "hit")

        # ---- 4. 合并通知：多个通知栏 + 一次音效 ----
        try:
            self.notifier.alert_batch(
                [(entry, score, source) for entry, score, _u in updated_list]
            )
        except Exception as e:                           # noqa: BLE001
            self.logger.warning("[Scheduler] 批量弹窗异常: %s", e)

        return updated_list

    def handle_hits(self, hits, source, img, notify_event: bool = True) -> int:
        """批量命中处理（ChatScanner / 列表匹配调用）。

        hits: [(entry, score, name_seen), ...]
        返回实际计数并提示的条数。

        ``notify_event=False`` 见 `_process_hits`：调用方自己播报结果。
        """
        return len(self._process_hits(hits, source, img,
                                      notify_event=notify_event))

    def handle_hit(self, entry, score, source, img, name_seen):
        """单条命中（兼容原接口）。

        返回数据库更新结果 dict；若该条被去重跳过或写入失败，返回 None。
        """
        results = self._process_hits([(entry, score, name_seen)], source, img)
        return results[0][2] if results else None

    def match_and_notify_batch(self, names, source, img) -> int:
        """名字列表批量匹配（ScanSession 调用），命中后一次性批量处理。"""
        if not names:
            return 0
        with self.match_lock:
            hits = self.matcher.check(names)
        if not hits:
            return 0
        return self.handle_hits(
            [(entry, score, entry.get("player_name") or "")
             for entry, score in hits],
            source, img,
        )

    def match_and_notify(self, names, source, img) -> int:
        """兼容原接口。"""
        return self.match_and_notify_batch(names, source, img)

    def process_text(self, text, source, img) -> int:
        """整段文本匹配（聊天框按需扫描调用），命中后一次性批量处理。"""
        if not text or not text.strip():
            return 0
        with self.match_lock:
            hits = self.matcher.match_text(text)
        if not hits:
            return 0
        return self.handle_hits(
            [(entry, score,
              matched_substr or (entry.get("player_name") or ""))
             for entry, score, matched_substr in hits],
            source, img,
        )

    def reload_blacklist(self) -> int:
        """黑名单变更后重建匹配索引。"""
        count = self.matcher.reload()
        self.log(f"[Scheduler] 匹配索引已刷新，共 {count} 条")
        return count

    def reset_session_hit_count(self) -> None:
        with self._hit_count_lock:
            self.session_hit_count = 0

    def get_session_hit_count(self) -> int:
        with self._hit_count_lock:
            return self.session_hit_count

    # ---------------------------------------------------------------- 证据
    def _save_evidence(self, img, entry, score):
        """保存命中截图到 data/evidence/，返回路径（失败返回 ""）。"""
        if not self.evidence_enabled or img is None:
            return ""
        try:
            os.makedirs(EVIDENCE_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            raw_name = entry.get("player_name") or "unknown"
            safe = _SAFE_NAME_RE.sub("_", str(raw_name))[:24] or "unknown"
            path = os.path.join(EVIDENCE_DIR, f"{ts}_{safe}_{int(score)}.jpg")
            img.convert("RGB").save(path, "JPEG",
                                    quality=EVIDENCE_JPEG_QUALITY)
            with self._evidence_lock:
                self._evidence_counter += 1
                need_prune = self._evidence_counter % 25 == 0
            if need_prune:
                self._prune_evidence()
            return path
        except Exception as e:                           # noqa: BLE001
            self.logger.warning("[Scheduler] 保存证据失败: %s", e)
            return ""

    @staticmethod
    def _prune_evidence() -> None:
        """证据目录超过上限时删除最旧的文件，避免磁盘无限增长。"""
        try:
            files = [os.path.join(EVIDENCE_DIR, f)
                     for f in os.listdir(EVIDENCE_DIR)
                     if f.lower().endswith(".jpg")]
            if len(files) <= EVIDENCE_MAX_FILES:
                return
            files.sort(key=lambda p: os.path.getmtime(p))
            for p in files[:len(files) - EVIDENCE_MAX_FILES]:
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception:                                # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 日志
    def log(self, msg: str, level: str = "info") -> None:
        getattr(self.logger, level, self.logger.info)(msg)

    def report(self, msg: str, level: str = "info") -> None:
        """写日志 **并** 把这条动态推给界面（`on_event`）。

        界面上那条「最近动态」就是靠它撑起来的：用户按下 F8、命中、被去重、
        会话结束、游戏启动/退出……都要看得见，而不是只躺在日志文件里。
        回调由 app 层接上（GUI 内部再转主线程），没接上时退化成纯日志。
        """
        try:
            self.log(msg, level)
        except TypeError:
            # log 有时会被换成只收一个参数的替身（测试里就是 lines.append，
            # 外部也可能这么包一层）。别让"播报"反过来把扫描线程搞崩。
            self.log(msg)
        self._emit(msg, level)

    def _emit(self, msg: str, level: str = "info") -> None:
        """只推界面动态流，不再写一遍日志。"""
        cb = self.on_event
        if cb is None:
            return
        try:
            cb(msg, level)
        except Exception as e:                           # noqa: BLE001
            self.logger.warning("[Scheduler] 事件回调异常: %s", e)

    def _set_hit_stats(self, total, deduped, recorded, names) -> None:
        with self._stats_lock:
            self.last_hit_stats = {"total": int(total), "deduped": int(deduped),
                                   "recorded": int(recorded),
                                   "names": list(names)}

    def get_hit_stats(self) -> dict:
        """最近一批命中的统计（ChatScanner 用它播报去重/记录结果）。"""
        with self._stats_lock:
            return dict(self.last_hit_stats)

    # ---------------------------------------------------------------- 关闭
    def shutdown(self) -> None:
        self._shutdown = True
        self.stop_session(wait=True)
        self.log("调度器已停止")

# -*- coding: utf-8 -*-
"""chat_scanner.py —— 聊天框**按需**扫描（替代原来的 ChatMonitor）。

设计原则：
    * **没有循环、没有定时器、没有哈希缓存** —— 只有被调用时才抓一张图、跑一次 OCR
    * 游戏运行时如果用户不点按钮，本模块 **完全不产生任何开销**
    * `_busy` 非阻塞锁保证连点按钮不会并发 OCR
    * 所有命中收集完成后 **一次性** 交给 `scheduler.handle_hits()`，
      从而做到「多个通知栏 + 只播一次音效」
    * 任何异常都被吞掉并写日志，绝不把异常抛回按钮回调

触发方式：
    * GUI 工具栏 [扫描聊天框] 按钮
    * 可选热键（chat_hotkey.py，默认关闭）
"""
from __future__ import annotations

import threading
import time

from app.config import (CHAT_SCAN_REGION_KEY, CHAT_SCAN_SOURCE,
                    OCR_CONFIDENCE_MIN, SCAN_THREAD_PRIORITY, get_logger)
from app.core.priority import low_priority


class ChatScanner:
    """按需触发一次聊天框扫描。无循环、无定时、无缓存。"""

    def __init__(self, scheduler, capture, ocr):
        self.scheduler = scheduler
        self.capture = capture
        self.ocr = ocr
        self.log = get_logger("chat_scan")
        self._busy = threading.Lock()
        #: 可选回调：**每次状态变化**都在调用线程里被调用（含 started）。
        #: 界面靠它播报"按了 F8 → 正在扫 → 结果如何"，按钮和热键走同一条路。
        self.on_result = None
        self.scan_count = 0
        self.last_result = None

    # ---------------------------------------------------------------- 状态
    def is_busy(self) -> bool:
        """当前是否有一次扫描正在进行。"""
        # 尝试获取锁来判断；拿到就立刻还回去
        if self._busy.acquire(blocking=False):
            self._busy.release()
            return False
        return True

    # ---------------------------------------------------------------- 扫描
    def scan_now(self, source: str = CHAT_SCAN_SOURCE) -> dict:
        """立即扫描一次聊天框。

        返回 {"status": ..., "hits": n, "text": str, "elapsed_ms": float}
        status: started / busy / empty / no_hit / ok / error

        `on_result` 会被调用**两次**：先 `started`（界面立刻有反应），
        再最终结果。ok 的结果里还会带 `source / names / recorded / deduped`，
        让界面能说清"命中了谁、有几条被去重跳过、有没有真的写进库"。
        """
        if not self._busy.acquire(blocking=False):
            self.scheduler.log("[ChatScanner] 上一次扫描尚未完成，跳过")
            return self._finish({"status": "busy", "hits": 0, "text": "",
                                 "source": source})
        try:
            # 先播报"已经开始扫"，抓图+OCR 要 0.5~1.2 秒，用户按下 F8 后
            # 这段时间界面必须有反应，否则就像没生效。
            self._notify({"status": "started", "hits": 0, "text": "",
                          "source": source, "elapsed_ms": 0.0})
            # 抓图 + OCR 会一次性吃掉一大块 CPU（实测可达 2 CPU秒以上），
            # 用低优先级跑，让游戏线程始终优先拿到 CPU —— 见 priority.py
            with low_priority(SCAN_THREAD_PRIORITY):
                return self._scan_locked(source)
        finally:
            self._busy.release()

    def _scan_locked(self, source: str) -> dict:
        t0 = time.time()
        try:
            # OCR 不可用时**必须明说**：以前这里会一路走到"聊天框为空"，
            # 用户看到的就是"按了 F8 没反应/扫描失效"（真实故障）。
            reason = self._ocr_unavailable_reason()
            if reason:
                self.scheduler.log(f"[ChatScanner] OCR 不可用：{reason}", "error")
                return self._finish({"status": "ocr_unavailable", "hits": 0,
                                     "text": "", "source": source,
                                     "error": reason})

            img = self.capture.grab(CHAT_SCAN_REGION_KEY)
            if img is None:
                self.scheduler.log("[ChatScanner] 聊天框截图失败")
                return self._finish({"status": "error", "hits": 0, "text": "",
                                     "source": source, "error": "聊天框截图失败"
                                     "（监视区域没框对？设置 → 监视区域）"})

            ocr_results = self.ocr.recognize_raw(img)
            text = " ".join(t for t, c in ocr_results
                            if c > OCR_CONFIDENCE_MIN and t)
            if not text.strip():
                self.scheduler.log("[ChatScanner] 聊天框为空")
                return self._finish({"status": "empty", "hits": 0, "text": "",
                                     "source": source,
                                     "elapsed_ms": (time.time() - t0) * 1000})

            hits = self.scheduler.matcher.match_text(text)
            elapsed = (time.time() - t0) * 1000
            self.scan_count += 1
            names = [str(e.get("player_name") or "?")
                     for e, _s, _m in hits]
            self.scheduler.log(
                f"[ChatScanner] 扫描完成 source={source} "
                f"匹配 {len(hits)} 条"
                f"{('：' + '、'.join(names)) if names else ''} "
                f"耗时 {elapsed:.0f}ms 文本={text[:120]!r}"
            )
            if not hits:
                return self._finish({"status": "no_hit", "hits": 0,
                                     "source": source, "names": [],
                                     "text": text, "elapsed_ms": elapsed})

            # 批量处理：多个通知栏 + 一次音效 + HIT_DEDUP_WINDOW 秒去重。
            # notify_event=False：本模块的 on_result 会把"命中了几条、几条被
            # 去重"一次说清楚，不必再往动态流里塞一行。
            self.scheduler.handle_hits(hits, source=source, img=img,
                                       notify_event=False)
            stats = self._hit_stats()
            return self._finish({"status": "ok", "hits": len(hits),
                                 "source": source, "names": names,
                                 "processed": stats["recorded"],
                                 "recorded": stats["recorded"],
                                 "deduped": stats["deduped"],
                                 "text": text, "elapsed_ms": elapsed})
        except Exception as e:                           # noqa: BLE001
            self.scheduler.log(f"[ChatScanner] 扫描异常: {e}")
            self.log.exception("聊天框扫描异常")
            return self._finish({"status": "error", "hits": 0, "text": "",
                                 "source": source, "error": str(e)})

    def _ocr_unavailable_reason(self) -> str:
        """OCR 不可用时返回原因（可用 / 测试替身没有这个接口时返回空串）。"""
        ocr = self.ocr
        if ocr is None:
            return "OCR 引擎未接入"
        reason_fn = getattr(ocr, "unavailable_reason", None)
        if isinstance(reason_fn, str):          # 测试替身可以直接给字符串
            return reason_fn
        if callable(reason_fn):
            try:
                return str(reason_fn() or "")
            except Exception as e:                       # noqa: BLE001
                return f"OCR 状态检查失败：{e}"
        available = getattr(ocr, "available", True)
        if callable(available):
            try:
                available = available()
            except Exception:                            # noqa: BLE001
                available = True
        return "" if available else "OCR 引擎不可用"

    def _hit_stats(self) -> dict:
        """取最近一批命中的统计（老 scheduler 没有这个接口时给零值）。"""
        try:
            stats = self.scheduler.get_hit_stats()
            return {"recorded": int(stats.get("recorded") or 0),
                    "deduped": int(stats.get("deduped") or 0)}
        except Exception:                                # noqa: BLE001
            return {"recorded": 0, "deduped": 0}

    # ---------------------------------------------------------------- 工具
    def _notify(self, result: dict) -> dict:
        """记录状态并回调（回调在**调用线程**里执行，界面侧必须自己转主线程）。"""
        result.setdefault("elapsed_ms", 0.0)
        self.last_result = result
        if self.on_result:
            try:
                self.on_result(result)
            except Exception as e:                       # noqa: BLE001
                self.log.warning("on_result 回调异常: %s", e)
        return result

    def _finish(self, result: dict) -> dict:
        return self._notify(result)

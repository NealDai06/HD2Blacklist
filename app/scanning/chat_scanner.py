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
        self.on_result = None        # 可选回调：扫描结束后在**调用线程**里被调用
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
        status: busy / empty / no_hit / ok / error
        """
        if not self._busy.acquire(blocking=False):
            self.scheduler.log("[ChatScanner] 上一次扫描尚未完成，跳过")
            return self._finish({"status": "busy", "hits": 0, "text": ""})
        try:
            # 抓图 + OCR 会一次性吃掉一大块 CPU（实测可达 2 CPU秒以上），
            # 用低优先级跑，让游戏线程始终优先拿到 CPU —— 见 priority.py
            with low_priority(SCAN_THREAD_PRIORITY):
                return self._scan_locked(source)
        finally:
            self._busy.release()

    def _scan_locked(self, source: str) -> dict:
        t0 = time.time()
        try:
            img = self.capture.grab(CHAT_SCAN_REGION_KEY)
            if img is None:
                self.scheduler.log("[ChatScanner] 聊天框截图失败")
                return self._finish({"status": "error", "hits": 0, "text": "",
                                     "error": "截图失败"})

            ocr_results = self.ocr.recognize_raw(img)
            text = " ".join(t for t, c in ocr_results
                            if c > OCR_CONFIDENCE_MIN and t)
            if not text.strip():
                self.scheduler.log("[ChatScanner] 聊天框为空")
                return self._finish({"status": "empty", "hits": 0, "text": ""})

            hits = self.scheduler.matcher.match_text(text)
            elapsed = (time.time() - t0) * 1000
            self.scan_count += 1
            self.scheduler.log(
                f"[ChatScanner] 扫描完成 source={source} "
                f"匹配 {len(hits)} 条 耗时 {elapsed:.0f}ms 文本={text[:120]!r}"
            )
            if not hits:
                return self._finish({"status": "no_hit", "hits": 0,
                                     "text": text, "elapsed_ms": elapsed})

            # 批量处理：多个通知栏 + 一次音效 + HIT_DEDUP_WINDOW 秒去重
            processed = self.scheduler.handle_hits(hits, source=source, img=img)
            return self._finish({"status": "ok", "hits": len(hits),
                                 "processed": processed, "text": text,
                                 "elapsed_ms": elapsed})
        except Exception as e:                           # noqa: BLE001
            self.scheduler.log(f"[ChatScanner] 扫描异常: {e}")
            self.log.exception("聊天框扫描异常")
            return self._finish({"status": "error", "hits": 0, "text": "",
                                 "error": str(e)})

    # ---------------------------------------------------------------- 工具
    def _finish(self, result: dict) -> dict:
        result.setdefault("elapsed_ms", 0.0)
        self.last_result = result
        if self.on_result:
            try:
                self.on_result(result)
            except Exception as e:                       # noqa: BLE001
                self.log.warning("on_result 回调异常: %s", e)
        return result

# -*- coding: utf-8 -*-
"""esc_trigger.py —— ESC 菜单触发扫描。

原理：
    * `GetAsyncKeyState(VK_ESCAPE)` 每 100ms 轮询一次（**不使用键盘钩子**）
    * 只在上升沿触发（避免长按连发），并做 1 秒去抖
    * 触发后：先停掉旧会话 → 等 0.6s 菜单动画 → 判断菜单是否真的打开了
      （区域灰度标准差 > 阈值）→ 菜单打开才启动 ScanSession
    * 菜单没打开（比如只是关个提示框）就什么都不做，符合"ESC 频繁触发必须过滤"

轮询在独立守护线程里进行；延迟判定放到子线程，保证 100ms 轮询节奏不被拖慢。
"""
from __future__ import annotations

import threading
import time

from config import (ESC_DEBOUNCE, ESC_MENU_DELAY, ESC_MENU_STD_THRESHOLD,
                    ESC_POLL_INTERVAL, ESC_SESSION, get_logger)

VK_ESCAPE = 0x1B


def _get_async_key_state(vk: int) -> int:
    import win32api
    return win32api.GetAsyncKeyState(vk)


class EscTrigger:
    """ESC 键 → 菜单玩家列表扫描。"""

    REGION_KEY = "menu_player_list"
    SOURCE = "esc_menu"

    def __init__(self, scheduler, capture, ocr, game_active=None,
                 region_key: str = REGION_KEY, params: dict = None,
                 active_check=None):
        self.scheduler = scheduler
        self.capture = capture
        self.ocr = ocr
        self.game_active = game_active if game_active is not None else \
            scheduler.game_active
        self.active_check = active_check or (lambda: self.game_active.is_set())
        self.region_key = region_key
        self.params = dict(params or ESC_SESSION)
        self.log = get_logger("esc")

        self._stop = threading.Event()
        self._thread = None
        self._last_press_ts = 0.0
        self._was_down = False
        self._pending = None
        self.trigger_count = 0
        self.skip_count = 0

    # ---------------------------------------------------------------- 控制
    def start(self):
        """启动轮询（阻塞，请放入守护线程）。"""
        if self._thread and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="EscTrigger")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------------- 主循环
    def _loop(self):
        self.log.info("ESC 触发监听启动（poll=%.0fms 去抖=%.1fs）",
                      ESC_POLL_INTERVAL * 1000, ESC_DEBOUNCE)
        while not self._stop.wait(ESC_POLL_INTERVAL):
            if not self.active_check():
                self._was_down = False
                continue
            try:
                down = bool(_get_async_key_state(VK_ESCAPE) & 0x8000)
            except Exception as e:                       # noqa: BLE001
                self.log.warning("GetAsyncKeyState 失败，ESC 触发停用: %s", e)
                return

            # 上升沿检测
            if down and not self._was_down:
                self._on_press()
            self._was_down = down

    def _on_press(self):
        now = time.time()
        if now - self._last_press_ts < ESC_DEBOUNCE:
            self.log.debug("ESC 去抖忽略")
            return
        self._last_press_ts = now
        self.trigger_count += 1
        self.log.info("[ESC] 检测到按键，%.2fs 后判定菜单状态", ESC_MENU_DELAY)

        # 先停掉可能还在跑的旧会话（同一时刻最多一个）
        self.scheduler.stop_session(wait=False)

        t = threading.Thread(target=self._delayed_check, daemon=True,
                             name="EscMenuCheck")
        t.start()

    def _delayed_check(self):
        if self._stop.wait(ESC_MENU_DELAY):
            return
        if not self.active_check():
            return
        try:
            img = self.capture.grab(self.region_key)
        except Exception as e:                           # noqa: BLE001
            self.log.warning("[ESC] 抓图失败: %s", e)
            return
        if img is None:
            return

        std = self.capture.region_std(img)
        if std <= ESC_MENU_STD_THRESHOLD:
            self.skip_count += 1
            self.log.info("[ESC] 菜单未打开（区域复杂度 %.1f ≤ %.1f），跳过扫描",
                          std, ESC_MENU_STD_THRESHOLD)
            return

        self.log.info("[ESC] 菜单已打开（区域复杂度 %.1f），启动扫描会话", std)
        self.scheduler.start_session(self.region_key, self.SOURCE, self.params)

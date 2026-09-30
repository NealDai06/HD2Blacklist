# -*- coding: utf-8 -*-
"""esc_trigger.py —— ESC 菜单触发扫描。

原理：
    * `GetAsyncKeyState(VK_ESCAPE)` 每 100ms 轮询一次（**不使用键盘钩子**）
    * 只在上升沿触发（避免长按连发），并做 1 秒去抖
    * 触发后：先停掉旧会话 → 等 0.6s 菜单动画 → 判断菜单是否真的打开了
      → 菜单打开才启动 ScanSession
    * 菜单没打开（比如只是关个提示框）就什么都不做，符合"ESC 频繁触发必须过滤"

关于「菜单是否打开」的判定（这里曾经有个把 ESC 扫描几乎废掉的 bug）：
    老实现只看「区域灰度标准差 > 20」，假设"菜单打开后画面更复杂"。
    拿真实截图一量就发现假设是错的 ——
        菜单**开着**的图：标准差 9.6~23.3、平均亮度 10~36
        菜单**没开**的图：标准差 93.0、平均亮度 140.3
    阈值 20 正好压在前者的中间，于是菜单明明开着也有一半概率被判成"没开"，
    连扫描都不启动，表现就是"ESC 菜单经常检测不出东西"。
    现在改成看**平均亮度**（暗色面板 = 菜单开着），并且拿不准时**宁可按开着处理**
    （多扫一次最多浪费 3 帧 OCR，漏扫则等于功能失效）；判定还要复查几次，
    躲开菜单淡入动画。

轮询在独立守护线程里进行；延迟判定放到子线程，保证 100ms 轮询节奏不被拖慢。
"""
from __future__ import annotations

import threading
import time

from config import (ESC_DEBOUNCE, ESC_MENU_CHECK_RETRY, ESC_MENU_DELAY,
                    ESC_MENU_MEAN_MAX, ESC_MENU_MEAN_MIN_BRIGHT,
                    ESC_MENU_RETRY_DELAY, ESC_MENU_STD_MIN,
                    ESC_MENU_STD_THRESHOLD, ESC_POLL_INTERVAL, ESC_SESSION,
                    get_logger)

VK_ESCAPE = 0x1B

STATE_OPEN = "open"
STATE_CLOSED = "closed"
STATE_UNKNOWN = "unknown"


def _get_async_key_state(vk: int) -> int:
    import win32api
    return win32api.GetAsyncKeyState(vk)


def judge_menu(mean: float, std: float) -> str:
    """根据区域亮度/对比度判断 ESC 菜单状态 → 'open' / 'closed' / 'unknown'。

    * 暗（平均亮度低）且有文字（标准差不为 0）→ 菜单面板已经铺开 → open
    * 明显亮且细节丰富 → 是游戏画面 → closed
    * 其余（半透明过渡、极暗纯色画面…）→ unknown，交给调用方复查 / 保守扫描
    """
    try:
        mean = float(mean)
        std = float(std)
    except (TypeError, ValueError):
        return STATE_UNKNOWN
    if mean <= ESC_MENU_MEAN_MAX:
        return STATE_OPEN if std >= ESC_MENU_STD_MIN else STATE_UNKNOWN
    if mean >= ESC_MENU_MEAN_MIN_BRIGHT and std >= ESC_MENU_STD_THRESHOLD:
        return STATE_CLOSED
    return STATE_UNKNOWN


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
        """等菜单动画结束后判定；拿不准就复查，最后一次仍拿不准则保守扫描。"""
        if self._stop.wait(ESC_MENU_DELAY):
            return
        attempts = max(1, int(ESC_MENU_CHECK_RETRY))
        for attempt in range(1, attempts + 1):
            if self._stop.is_set() or not self.active_check():
                return
            try:
                img = self.capture.grab(self.region_key)
            except Exception as e:                       # noqa: BLE001
                self.log.warning("[ESC] 抓图失败: %s", e)
                return
            if img is None:
                return

            mean, std = self._stats(img)
            state = judge_menu(mean, std)
            detail = (f"亮度 {mean:.1f} 复杂度 {std:.1f} "
                      f"(第 {attempt}/{attempts} 次判定)")

            if state == STATE_CLOSED:
                self.skip_count += 1
                self.log.info("[ESC] 菜单未打开（%s → 判定为游戏画面），跳过扫描",
                              detail)
                return
            if state == STATE_OPEN:
                self.log.info("[ESC] 菜单已打开（%s），启动扫描会话", detail)
                self.scheduler.start_session(self.region_key, self.SOURCE,
                                             self.params)
                return

            # unknown：可能是菜单淡入中，等一会儿再看
            self.log.debug("[ESC] 菜单状态待定（%s）", detail)
            if attempt < attempts and self._stop.wait(ESC_MENU_RETRY_DELAY):
                return

        # 复查完还是说不准 —— 宁可多扫一次（最多浪费几帧低优先级 OCR），
        # 也不能因为判不出来就整个漏掉这次 ESC。
        self.log.info("[ESC] 菜单状态无法确定，按已打开处理并启动扫描会话")
        self.scheduler.start_session(self.region_key, self.SOURCE, self.params)

    def _stats(self, img):
        """取区域 (平均亮度, 标准差)，兼容只有 region_std 的老 capture。"""
        fn = getattr(self.capture, "region_stats", None)
        if callable(fn):
            try:
                mean, std = fn(img)
                return float(mean), float(std)
            except Exception:                            # noqa: BLE001
                pass
        try:
            return float(self.capture.region_std(img)), 0.0
        except Exception:                                # noqa: BLE001
            return 255.0, 0.0

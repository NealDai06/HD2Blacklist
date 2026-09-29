# -*- coding: utf-8 -*-
"""scan_session.py —— 玩家列表扫描会话（有明确生命周期与终止条件）。

一个 ScanSession 只负责「按固定间隔扫描某个区域 → 提取玩家名 → 交给调度器匹配」。
它自身不含任何全局状态，终止条件有三个：

    1. 连续 N 次没有识别到有效结果          (max_consecutive_empty)
    2. 总时长超过上限                        (max_duration)
    3. 距离上一次「发现新名字」超过           (keep_alive_after_hit)
       —— 对应验收项"会话持续到滚动结束后 3 秒终止"；
          keep_alive_after_hit=0 表示"扫到名字就立刻收工"（冷启动用）

这样即使游戏处于静止画面，扫描也会自动停下来，符合"资源节省是第一原则"。
"""
from __future__ import annotations

import threading
import time

import numpy as np

from config import (OCR_CONFIDENCE_MIN, SCAN_THREAD_PRIORITY,
                    SESSION_DIFF_TOLERANCE, SESSION_MAX_STATIC_FRAMES,
                    SESSION_SKIP_UNCHANGED, get_logger)
from priority import low_priority

# 明显的 UI 文字，避免把菜单项当成玩家名
UI_BLACKLIST = {
    "setting", "settings", "options", "quit", "exit",
    "resume", "back", "return", "leave", "invite",
    "设置", "选项", "退出", "返回", "离开", "邀请",
    "装备", "战略", "地图", "任务", "等级", "经验",
    "continue", "cancel", "confirm", "select", "loadout",
    "stratagems", "stratagem", "mission", "social", "armory",
    "warbond", "acquisitions", "ship", "management",
}

_NAME_MIN_LEN = 2
_NAME_MAX_LEN = 30


def is_valid_player_name(text: str) -> bool:
    """判断 OCR 文本是否像一个玩家名。"""
    if not text:
        return False
    t = text.strip()
    if len(t) < _NAME_MIN_LEN or len(t) > _NAME_MAX_LEN:
        return False
    if t.isdigit():
        return False
    if t.lower() in UI_BLACKLIST:
        return False
    # 至少要包含一个字母/数字/汉字
    if not any(c.isalnum() or '\u4e00' <= c <= '\u9fff' for c in t):
        return False
    # 纯符号/纯标点比例过高的丢掉
    meaningful = sum(1 for c in t
                     if c.isalnum() or '\u4e00' <= c <= '\u9fff')
    if meaningful / len(t) < 0.4:
        return False
    return True


def evaluate_scan(ocr_results, min_conf: float = OCR_CONFIDENCE_MIN) -> list:
    """从 OCR 结果里筛出有效玩家名，保持原始顺序并去重。

    接受两种输入：
        [(text, conf), ...]              —— 每个元素当作独立一行
        [(text, conf, bbox), ...]        —— 会先把同一行的碎片拼回完整名字
    """
    out, seen = [], set()
    for item in group_into_lines(ocr_results or [], min_conf) or ():
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def group_into_lines(ocr_results, min_conf: float = OCR_CONFIDENCE_MIN,
                     y_overlap: float = 0.5, gap_ratio: float = 1.2) -> list:
    """把 OCR 碎片按「同一行」聚合并过滤成有效玩家名。

    规则：
        * 竖直方向重叠 >= 较小框高度的 y_overlap
        * 横向间距 <= gap_ratio × 文字高度（说明是同一个人名被切开）
    没有 bbox 的结果（例如 [(text, conf)]）会被各自当作独立一行处理。
    """
    frags = []
    no_box = []
    for item in ocr_results or []:
        try:
            text = item[0]
            conf = float(item[1])
            bbox = item[2] if len(item) > 2 else None
        except (IndexError, TypeError, ValueError):
            continue
        if conf <= min_conf:
            continue
        text = (text or "").strip()
        if not text:
            continue
        if not bbox:
            no_box.append(text)
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in bbox)
        except (TypeError, ValueError):
            no_box.append(text)
            continue
        h = max(1.0, y1 - y0)
        frags.append({"text": text, "x0": x0, "x1": x1, "y0": y0, "y1": y1,
                      "h": h, "yc": (y0 + y1) / 2.0})

    names = []
    for text in no_box:                     # 没有坐标的，各自成行
        if is_valid_player_name(text):
            names.append(text)

    frags.sort(key=lambda f: (f["yc"], f["x0"]))
    lines = []
    for frag in frags:
        placed = False
        for line in lines:
            ref = line[-1]
            overlap = min(ref["y1"], frag["y1"]) - max(ref["y0"], frag["y0"])
            if overlap <= 0:
                continue
            if overlap / min(ref["h"], frag["h"]) < y_overlap:
                continue
            gap = frag["x0"] - ref["x1"]
            if gap <= gap_ratio * max(ref["h"], frag["h"]):
                line.append(frag)
                placed = True
                break
        if not placed:
            lines.append([frag])

    for line in lines:
        line.sort(key=lambda f: f["x0"])
        # 直接拼接：matcher 归一化时会去掉所有非字母数字字符，
        # 所以 "SamplePlayer" + "01" 与 "SamplePlayer_01" 等价。
        merged = "".join(f["text"] for f in line).strip()
        if is_valid_player_name(merged):
            names.append(merged)
    return names


def evaluate_boxes(ocr_results, min_conf: float = OCR_CONFIDENCE_MIN) -> list:
    """group_into_lines 的别名（语义更明确：输入是带框的 OCR 结果）。"""
    return group_into_lines(ocr_results, min_conf)


class ScanSession:
    """一次扫描会话（在独立守护线程里运行）。"""

    def __init__(self, scheduler, region_key: str, source: str, params: dict):
        self.scheduler = scheduler
        self.region_key = region_key
        self.source = source
        self.interval = float(params["interval"])
        self.max_duration = float(params["max_duration"])
        self.max_consecutive_empty = int(params["max_consecutive_empty"])
        self.keep_alive_after_hit = float(params["keep_alive_after_hit"])
        # 画面连续静止多少帧就收工（0=不启用）。抓屏次数≈让游戏卡顿的次数，
        # 静止的菜单没必要反复抓。
        self.max_static_frames = int(params.get("max_static_frames",
                                                SESSION_MAX_STATIC_FRAMES) or 0)
        # 画面没变化就跳过 OCR（默认跟随 config，可按会话覆盖，方便测试）
        self.skip_unchanged = bool(params.get("skip_unchanged",
                                              SESSION_SKIP_UNCHANGED))
        self.logger = get_logger("session")

        self._stop = threading.Event()
        self._thread = None
        self.seen_names = set()
        self.hit_count = 0
        self.skipped_frames = 0        # 因画面没变而跳过 OCR 的帧数
        self._last_small = None        # 上一帧的缩略灰度图（用于差分）
        self.started_at = None
        self.finished_at = None

    # ---------------------------------------------------------------- 控制
    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.alive:
            return self._thread
        self.started_at = time.time()
        self._thread = threading.Thread(
            target=self._run, name=f"ScanSession-{self.source}", daemon=True
        )
        self._thread.start()
        return self._thread

    def stop(self, wait: bool = False, timeout: float = 2.0) -> None:
        self._stop.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=timeout)

    # ---------------------------------------------------------------- 主循环
    def _frame_changed(self, img) -> bool:
        """画面变了吗？（1/4 缩略图的平均像素差 —— 几十微秒，远便宜于 OCR）

        菜单/列表静止时，把同一张图反复送去 OCR 纯属浪费：既白烧 CPU，
        又会让游戏掉帧。这里先花几十微秒判断一下。
        """
        if not self.skip_unchanged:
            return True
        try:
            w = max(1, img.width // 4)
            h = max(1, img.height // 4)
            small = np.asarray(img.convert("L").resize((w, h)),
                               dtype=np.int16)
        except Exception:                                # noqa: BLE001
            return True                                  # 判断不了就当变了
        prev = self._last_small
        self._last_small = small
        if prev is None or prev.shape != small.shape:
            return True
        return float(np.abs(small - prev).mean()) > SESSION_DIFF_TOLERANCE

    def _scan_once(self):
        """抓图 + OCR + 有效性过滤。

        返回 (img, valid_names, skipped)：
            skipped=True 表示「画面没变化，本次没跑 OCR」，valid_names 为 None。
        """
        with low_priority(SCAN_THREAD_PRIORITY):
            img = self.scheduler.capture.grab(self.region_key)
            if img is None:
                raise RuntimeError(f"区域截图失败: {self.region_key}")
            if not self._frame_changed(img):
                return img, None, True
            ocr = self.scheduler.ocr
            # 真实引擎提供 recognize_lines（会把被 OCR 切开的名字拼回去）；
            # 测试替身只有 recognize_raw 时自动退回逐行过滤。
            lines_fn = getattr(ocr, "recognize_lines", None)
            if callable(lines_fn):
                valid_names = lines_fn(img)
            else:
                valid_names = evaluate_scan(ocr.recognize_raw(img))
        return img, valid_names, False

    def _run(self):
        start_time = time.time()
        empty_streak = 0
        static_streak = 0
        last_new_name_time = None
        reason = "unknown"
        self.scheduler.register_session(self)
        self.scheduler.log(
            f"[Session] 开始 source={self.source} region={self.region_key} "
            f"interval={self.interval}s max={self.max_duration}s"
        )

        try:
            while not self._stop.is_set():
                if time.time() - start_time > self.max_duration:
                    reason = "超时"
                    break
                if (last_new_name_time is not None
                        and (time.time() - last_new_name_time)
                        > self.keep_alive_after_hit):
                    reason = "发现新名字后静默"
                    break

                # 可被 stop() 立刻打断的等待
                if self._stop.wait(self.interval):
                    reason = "被中止"
                    break

                try:
                    img, valid_names, skipped = self._scan_once()
                except Exception as e:                   # noqa: BLE001
                    self.logger.warning("[Session] 扫描异常: %s", e)
                    empty_streak += 1
                    if empty_streak >= self.max_consecutive_empty:
                        reason = "连续异常"
                        break
                    continue

                if skipped:
                    # 画面没变化：不跑 OCR，也**不算**「连续无结果」——
                    # 否则静止菜单会在 1 秒内被误判成收工。
                    self.skipped_frames += 1
                    static_streak += 1
                    if (self.max_static_frames
                            and static_streak >= self.max_static_frames):
                        reason = "画面持续静止"
                        break
                    continue

                static_streak = 0

                if not valid_names:
                    empty_streak += 1
                    if empty_streak >= self.max_consecutive_empty:
                        reason = "连续无结果"
                        break
                    continue

                empty_streak = 0
                new_names = [n for n in valid_names if n not in self.seen_names]
                if new_names:
                    # 只有"发现新名字"才刷新存活计时器：
                    # 这样滚动结束后 keep_alive_after_hit 秒就会自动收工。
                    last_new_name_time = time.time()
                    self.seen_names.update(new_names)
                    try:
                        # 一次性批量处理：多个通知栏 + 只播一次音效 + 命中去重
                        self.hit_count += self.scheduler.match_and_notify_batch(
                            new_names, self.source, img
                        )
                    except Exception as e:               # noqa: BLE001
                        self.logger.warning("[Session] 匹配异常: %s", e)
        except Exception as e:                           # noqa: BLE001
            reason = f"未捕获异常({e})"
            self.logger.exception("[Session] 线程异常终止")
        finally:
            self.finished_at = time.time()
            self.scheduler.unregister_session(self)
            self.scheduler.log(
                f"[Session] 结束 source={self.source} 原因={reason} "
                f"耗时={self.finished_at - start_time:.1f}s "
                f"识别={len(self.seen_names)}个名字 命中={self.hit_count} "
                f"静止跳过={self.skipped_frames}帧"
            )

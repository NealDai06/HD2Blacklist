# -*- coding: utf-8 -*-
"""perf_probe.py —— 摸清「触发扫描」到底吃掉多少资源，以及优化有没有用。

用法：
    python tools/perf_probe.py           # 全套
    python tools/perf_probe.py --all     # 追加会话累计成本估算

输出内容：
    1) 本机 CPU 核心数、OCR 线程数、会话参数、当前进程优先级
    2) 实测 抓图 / 预处理 / 只做检测 / 检测+识别 的 墙钟 与 CPU 时间
    3) 用真实截图模拟「静止的菜单」：N 帧里到底真的跑了几次 OCR、共吃多少 CPU
    4) 真跑一遍会话线程，数「改前 / 改后」各抓了多少次屏
       —— 抓屏次数 ≈ 可能让游戏卡顿的次数，这才是掉帧的直接指标
"""
from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                        # noqa: BLE001
    pass

import priority                                          # noqa: E402
from config import (ESC_SESSION, OCR_INTRA_OP_THREADS,   # noqa: E402
                    SESSION_DIFF_TOLERANCE, SESSION_SKIP_UNCHANGED)
from ocr_engine import OCREngine                         # noqa: E402
from region_config import RegionConfig                   # noqa: E402
from scan_session import ScanSession                     # noqa: E402
from screen_capture import ScreenCapture                 # noqa: E402


def _line(title=""):
    if not title:
        print("-" * 66)
    else:
        print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def env_info():
    _line("环境")
    print(f"CPU 逻辑核心数        : {os.cpu_count()}")
    print(f"OCR 线程数(单次识别)  : {OCR_INTRA_OP_THREADS}")
    print(f"ESC 菜单会话参数      : {ESC_SESSION}")
    print(f"静止画面跳过 OCR      : {SESSION_SKIP_UNCHANGED}"
          f"（阈值 {SESSION_DIFF_TOLERANCE}）")
    print(f"当前进程优先级        : {priority.process_priority_name()}")


# --------------------------------------------------------------------------
def measure(region_key="chat_event", rounds=5):
    rc = RegionConfig()
    cap = ScreenCapture(rc)
    ocr = OCREngine()
    ok = ocr.warmup()
    time.sleep(0.3)
    _line("抓图与 OCR 成本（真实屏幕）")
    if not ok:
        print("OCR 不可用，只测抓图")

    t0 = time.perf_counter()
    for _ in range(rounds):
        cap.grab(region_key)
    wall_grab = (time.perf_counter() - t0) / rounds * 1000

    img = cap.grab(region_key)
    print(f"区域                  : {region_key} {img.size}")

    c0, c1 = time.process_time(), None
    arr = ocr.preprocess(img)
    c1 = time.process_time()
    print(f"仅抓图                : 墙钟 {wall_grab:.1f} ms/次")
    print(f"预处理(放大+灰度)     : CPU {(c1 - c0) * 1000:.1f} ms   → {arr.shape}")

    if not ok:
        cap.close()
        return

    # 只做检测（不识别文字） vs 检测+识别
    engine = ocr._ensure_engine()
    det_cpu = det_wall = None
    try:
        w0, c0 = time.perf_counter(), time.process_time()
        engine(arr, use_det=True, use_cls=False, use_rec=False)
        det_wall = (time.perf_counter() - w0) * 1000
        det_cpu = (time.process_time() - c0) * 1000
    except Exception as e:                               # noqa: BLE001
        print(f"（只做检测的计时跳过: {e}）")

    tot_w = tot_c = 0.0
    for _ in range(rounds):
        a, b = time.perf_counter(), time.process_time()
        ocr.recognize_raw(cap.grab(region_key))
        tot_w += time.perf_counter() - a
        tot_c += time.process_time() - b
    wall, cpu = tot_w / rounds * 1000, tot_c / rounds * 1000

    if det_cpu is not None:
        print(f"只做文字检测          : 墙钟 {det_wall:.0f} ms    CPU {det_cpu:.0f} ms")
    print(f"检测 + 识别（全流程） : 墙钟 {wall:.0f} ms    CPU {cpu:.0f} ms")
    print(f"→ 单次扫描约 {cpu / 1000:.2f} CPU秒，压在 {wall:.0f} ms 内，"
          f"瞬时占用 {cpu / wall:.1f} 个核")
    if det_cpu and cpu > det_cpu:
        print(f"→ 其中识别（按框逐条）占大头：{(cpu - det_cpu):.0f} ms "
              f"＝ {(cpu - det_cpu) / cpu * 100:.0f}%")
    print("→ 画面上文字越多越贵：这是菜单/名单比聊天框更耗 CPU 的原因")
    cap.close()


# --------------------------------------------------------------------------
class _StubScheduler:
    """给 ScanSession 用的最小替身（只喂同一张静止截图）。"""

    OCR_FAKE = ["ProbePlayer"]      # 假装一直识别到这个名字，会话就不会提前收工

    def __init__(self, img, ocr):
        self.img = img
        self.ocr = ocr
        self.grabs = 0

        class _Cap:
            def __init__(self, outer):
                self.outer = outer

            def grab(self, key):
                self.outer.grabs += 1
                return self.outer.img
        self.capture = _Cap(self)

    def log(self, *a, **k):
        pass

    def register_session(self, s):
        pass

    def unregister_session(self, s):
        pass

    def match_and_notify_batch(self, names, source, img):
        return 0


class _CountingOCR:
    """只数调用次数，不真跑 OCR（这样测的是抓屏次数，不受 OCR 快慢干扰）。"""

    def __init__(self, names=None):
        self.calls = 0
        self.names = names if names is not None else _StubScheduler.OCR_FAKE

    def recognize_lines(self, img):
        self.calls += 1
        return list(self.names)


def measure_session_captures(region_key="menu_player_list"):
    """真跑一遍会话线程，数一数总共抓了多少次屏。

    抓屏次数 ≈ 可能让游戏卡顿的次数，所以这才是「掉帧」的直接指标。
    """
    _line("一次菜单扫描会话：实际抓了多少次屏")
    rc = RegionConfig()
    cap = ScreenCapture(rc)
    img = cap.grab(region_key)
    cap.close()
    if img is None:
        print("截图失败，跳过")
        return
    cases = [
        ("改前：间隔 0.3s、静止不提前收工",
         {"interval": 0.3, "max_duration": 8.0, "max_consecutive_empty": 3,
          "keep_alive_after_hit": 3.0, "max_static_frames": 0}),
        ("改后：间隔 0.5s、静止 6 帧收工",
         {"interval": 0.5, "max_duration": 8.0, "max_consecutive_empty": 3,
          "keep_alive_after_hit": 3.0, "max_static_frames": 6}),
    ]
    for name, params in cases:
        ocr = _CountingOCR()
        sched = _StubScheduler(img, ocr)
        session = ScanSession(sched, region_key, "probe", params)
        t0 = time.time()
        session.start()
        session._thread.join(timeout=params["max_duration"] + 3)
        session.stop()
        print(f"{name}")
        print(f"    抓屏 {sched.grabs} 次  真的跑了 {ocr.calls} 次 OCR  "
              f"耗时 {time.time() - t0:.1f}s")
    print("\n→ 抓屏次数直接决定「游戏被卡几次」；OCR 次数决定 CPU 被吃掉多少")


def _session_estimate():
    _line("一次 ESC 菜单扫描会话的累计成本")
    iv = float(ESC_SESSION["interval"])
    mx = float(ESC_SESSION["max_duration"])
    n = int(mx / iv)
    print(f"每 {iv}s 扫一次，最长 {mx}s → 最多 {n} 次抓图 + OCR")
    print("每次抓屏都会让 DWM/GPU 同步一次，所以次数本身就是成本；"
          "\n开『静止跳过 OCR』后，静止菜单里只有画面变化的那几帧真的跑 OCR。")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env_info()
    measure()
    measure_static_session()
    measure_session_captures()
    if "--all" in argv:
        _session_estimate()
    print()
    return 0


def measure_static_session(region_key="menu_player_list", frames=10):
    """静止画面下跑 N 帧，看真的跑了几次 OCR、共吃多少 CPU。"""
    _line("静止画面重复扫描的浪费（真实截图 × N 帧）")
    rc = RegionConfig()
    cap = ScreenCapture(rc)
    real = OCREngine()
    if not real.warmup():
        print("OCR 不可用，跳过")
        cap.close()
        return
    img = cap.grab(region_key)
    cap.close()
    if img is None:
        print("截图失败，跳过")
        return
    counter = [0]

    class _RealCounting(_CountingOCR):
        def recognize_lines(self, im):
            counter[0] += 1
            return real.recognize_lines(im)

    sched = _StubScheduler(img, _RealCounting())
    session = ScanSession(sched, region_key, "probe", {
        "interval": 0.01, "max_duration": 5.0,
        "max_consecutive_empty": 999, "keep_alive_after_hit": 0,
    })
    c0 = time.process_time()
    t0 = time.perf_counter()
    for _ in range(frames):
        session._scan_once()
    cpu = (time.process_time() - c0) * 1000
    wall = (time.perf_counter() - t0) * 1000
    print(f"{frames} 帧同一张静止截图：真的跑了 {counter[0]} 次 OCR，"
          f"共 {cpu:.0f} ms CPU（墙钟 {wall:.0f} ms）")
    if SESSION_SKIP_UNCHANGED and counter[0] == 1:
        print("→ 跳过生效：只有第 1 帧真的识别，其余帧只做一次缩略图差分")
    elif not SESSION_SKIP_UNCHANGED:
        print(f"→ 未开启跳过：白跑了 {counter[0] - 1} 次 OCR")


if __name__ == "__main__":
    sys.exit(main())

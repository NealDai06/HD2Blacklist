# -*- coding: utf-8 -*-
"""priority.py —— 把本程序的 CPU 优先级压到游戏下面（保护游戏帧数）。

为什么需要：
    抓屏 + OCR 是「一次性吃掉一大块 CPU」的活（实测单次约 0.3~0.9 CPU 秒，
    压在 0.3~0.5 秒内跑完，等于瞬间占满 2 个核）。如果这些工作和游戏的
    渲染/逻辑线程同为 NORMAL 优先级，Windows 会公平地轮转 CPU，
    游戏就会出现卡顿 —— 表现就是 1% low 掉到 55 这种「最低帧」被拉低。

    把本进程降到 BELOW_NORMAL、把扫描线程再压到 LOWEST 之后，
    游戏线程永远优先拿到 CPU，我们只在**空闲时间片**里干活：
    扫描可能慢一点（0.5 → 0.8 秒），但游戏的帧率不再被拖。

机制（Windows 优先级是「进程基类 + 线程偏移」算出来的）：
    * `SetPriorityClass(GetCurrentProcess(), BELOW_NORMAL)` 会影响本进程
      **所有线程**（包括 onnxruntime 内部自己建的工作线程）——
      因为它们的基础优先级是拿进程基类现算的，所以一次调用就能全压低。
    * `SetThreadPriority(GetCurrentThread(), LOWEST)` 让扫描线程更靠后。

非 Windows / 调用失败都安静降级，绝不影响功能。
"""
from __future__ import annotations

import contextlib
import ctypes
import os

from app.config import get_logger

# ---- 进程优先级类 ----
IDLE_PRIORITY_CLASS = 0x00000040
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NORMAL_PRIORITY_CLASS = 0x00000020
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
HIGH_PRIORITY_CLASS = 0x00000080

# ---- 线程优先级（相对进程基类的偏移）----
THREAD_PRIORITY_IDLE = -15
THREAD_PRIORITY_LOWEST = -2
THREAD_PRIORITY_BELOW_NORMAL = -1
THREAD_PRIORITY_NORMAL = 0

_PROCESS_LEVELS = {
    "idle": IDLE_PRIORITY_CLASS,
    "below_normal": BELOW_NORMAL_PRIORITY_CLASS,
    "normal": NORMAL_PRIORITY_CLASS,
    "above_normal": ABOVE_NORMAL_PRIORITY_CLASS,
    "high": HIGH_PRIORITY_CLASS,
}

_THREAD_LEVELS = {
    "idle": THREAD_PRIORITY_IDLE,
    "lowest": THREAD_PRIORITY_LOWEST,
    "below_normal": THREAD_PRIORITY_BELOW_NORMAL,
    "normal": THREAD_PRIORITY_NORMAL,
}

_k32 = None
_load_tried = False


def _kernel32():
    global _k32, _load_tried
    if _load_tried:
        return _k32
    _load_tried = True
    if os.name != "nt":
        return None
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.GetCurrentThread.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k.SetPriorityClass.restype = ctypes.c_int
        k.GetPriorityClass.argtypes = [ctypes.c_void_p]
        k.GetPriorityClass.restype = ctypes.c_uint
        k.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k.SetThreadPriority.restype = ctypes.c_int
        k.GetThreadPriority.argtypes = [ctypes.c_void_p]
        k.GetThreadPriority.restype = ctypes.c_int
        _k32 = k
    except Exception:                                    # noqa: BLE001
        _k32 = None
    return _k32


# --------------------------------------------------------------------------
def set_process_priority(level: str = "below_normal") -> bool:
    """调整整个进程的优先级类。返回是否成功。"""
    k = _kernel32()
    if k is None:
        return False
    flags = _PROCESS_LEVELS.get(str(level).lower())
    if flags is None:
        get_logger("priority").warning("未知进程优先级: %s", level)
        return False
    try:
        return bool(k.SetPriorityClass(k.GetCurrentProcess(), flags))
    except Exception:                                    # noqa: BLE001
        return False


def get_process_priority() -> int:
    k = _kernel32()
    if k is None:
        return 0
    try:
        return int(k.GetPriorityClass(k.GetCurrentProcess()))
    except Exception:                                    # noqa: BLE001
        return 0


def process_priority_name() -> str:
    value = get_process_priority()
    for name, flags in _PROCESS_LEVELS.items():
        if flags == value:
            return name
    return f"0x{value:04X}"


def set_current_thread_priority(level: str = "lowest"):
    """调整当前线程优先级，返回**原来的值**（便于恢复），失败返回 None。"""
    k = _kernel32()
    if k is None:
        return None
    delta = _THREAD_LEVELS.get(str(level).lower())
    if delta is None:
        return None
    try:
        handle = k.GetCurrentThread()
        old = int(k.GetThreadPriority(handle))
        if not k.SetThreadPriority(handle, int(delta)):
            return None
        return old
    except Exception:                                    # noqa: BLE001
        return None


def restore_thread_priority(old) -> None:
    if old is None:
        return
    k = _kernel32()
    if k is None:
        return
    try:
        k.SetThreadPriority(k.GetCurrentThread(), int(old))
    except Exception:                                    # noqa: BLE001
        pass


@contextlib.contextmanager
def low_priority(level: str = "lowest"):
    """让 with 块里的工作以低优先级运行（结束后恢复原值）。"""
    old = set_current_thread_priority(level)
    try:
        yield old is not None
    finally:
        restore_thread_priority(old)


def apply_game_friendly(process_level: str = "below_normal",
                        log: bool = True) -> bool:
    """启动时调用：把进程整体压到游戏下面。"""
    ok = set_process_priority(process_level)
    if log:
        logger = get_logger("priority")
        if ok:
            logger.info("已把本进程优先级降到 %s（游戏优先拿 CPU；扫描线程用 %s）",
                        process_level, "lowest")
        else:
            logger.info("调整进程优先级失败或已跳过，保持系统默认")
    return ok

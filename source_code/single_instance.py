# -*- coding: utf-8 -*-
"""single_instance.py —— 单实例限制：同一时间只允许跑一个进程。

实现方式：命名内核互斥体（`CreateMutexW`）。为什么不用锁文件：

    * 进程被任务管理器结束 / 崩溃 / 断电后，内核对象由系统自动回收，
      不存在「残留锁文件导致再也打不开」这种经典问题；
    * 不写文件、不写注册表、不改任何游戏相关的东西；
    * 名字带 `Local\\` 前缀 —— 只在当前登录会话内互斥，多用户互不干扰。

第二个实例启动时：
    ① 尽力把已经在跑的那个窗口从托盘 / 后台叫到前台
       （用户双击 exe 的意图通常就是「把程序调出来」），成功就直接退出；
    ② 叫不出来（找不到窗口）才弹一句提示，说明程序已在运行、可能缩在托盘。

注意：只有**正常 GUI 启动**受限。`--check` / `--capture-debug` /
`--preview-notification` 这类命令行诊断不受限制（程序开着也能照跑，
方便边运行边排查）。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from config import WINDOW_TITLE, get_logger

#: 互斥体名字（Local\ = 当前登录会话内有效）
MUTEX_NAME = r"Local\hd2_blacklist_single_instance"

ERROR_ALREADY_EXISTS = 183

SW_SHOW = 5
SW_RESTORE = 9

_k32 = None
_u32 = None
_load_tried = False


def _libs():
    """惰性加载 kernel32 / user32，并**显式声明参数类型**。

    不声明的话 ctypes 默认按 c_int 处理返回值，64 位下句柄会被截断。
    """
    global _k32, _u32, _load_tried
    if _load_tried:
        return _k32, _u32
    _load_tried = True
    if os.name != "nt":
        return None, None
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                   wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        _k32 = k

        u = ctypes.WinDLL("user32", use_last_error=True)
        u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.FindWindowW.restype = wintypes.HWND
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.BringWindowToTop.argtypes = [wintypes.HWND]
        u.BringWindowToTop.restype = wintypes.BOOL
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        _u32 = u
    except Exception:                                    # noqa: BLE001
        _k32 = _u32 = None
    return _k32, _u32


# ==========================================================================
class SingleInstance:
    """持有就代表「我是唯一实例」；进程结束（含被强杀）由系统自动释放。"""

    def __init__(self, name: str = MUTEX_NAME):
        self.name = name
        self.handle = None
        self.log = get_logger("single")

    # ------------------------------------------------------------------
    def acquire(self) -> bool:
        """尝试成为唯一实例。True=可以继续启动，False=已经有实例在跑。"""
        if os.name != "nt":
            return True                      # 非 Windows：不做限制
        k, _ = _libs()
        if k is None:
            self.log.warning("无法加载 kernel32，跳过单实例检查")
            return True
        try:
            handle = k.CreateMutexW(None, False, self.name)
        except Exception as e:                           # noqa: BLE001
            self.log.warning("创建互斥体失败，跳过单实例检查: %s", e)
            return True
        if not handle:
            self.log.warning("创建互斥体返回空句柄，跳过单实例检查")
            return True
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            try:
                k.CloseHandle(handle)                    # 别人的，还回去
            except Exception:                            # noqa: BLE001
                pass
            self.log.info("已有实例在运行，本次不再启动")
            return False
        self.handle = handle
        self.log.debug("单实例互斥体已获取: %s", self.name)
        return True

    def release(self) -> None:
        if self.handle is not None:
            try:
                k, _ = _libs()
                if k is not None:
                    k.CloseHandle(self.handle)
            except Exception:                            # noqa: BLE001
                pass
            finally:
                self.handle = None

    @property
    def held(self) -> bool:
        return self.handle is not None


# ==========================================================================
def find_window(title: str = WINDOW_TITLE) -> int:
    """按窗口标题找主窗口（隐藏 / 最小化到托盘的窗口也能找到）。"""
    _, u = _libs()
    if u is None or not title:
        return 0
    try:
        return int(u.FindWindowW(None, title) or 0)
    except Exception:                                    # noqa: BLE001
        return 0


def bring_to_front(title: str = WINDOW_TITLE) -> bool:
    """把已在运行的窗口叫到前台（尽力而为，失败返回 False）。"""
    _, u = _libs()
    if u is None:
        return False
    hwnd = find_window(title)
    if not hwnd:
        return False
    try:
        u.ShowWindow(hwnd, SW_RESTORE if u.IsIconic(hwnd) else SW_SHOW)
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:                                    # noqa: BLE001
        return False


def _dialog(message: str = "") -> None:
    text = message or (
        f"{WINDOW_TITLE} 已经在运行了。\n\n"
        "它可能被最小化到了系统托盘（任务栏右下角的小图标），\n"
        "点一下托盘图标就能把窗口调出来，不需要重复打开。")
    try:
        import tkinter as tk
        import tkinter.messagebox as mb
        root = tk.Tk()
        root.withdraw()
        mb.showinfo("程序已在运行", text, parent=root)
        root.destroy()
    except Exception as e:                               # noqa: BLE001
        get_logger("single").warning("提示窗口弹出失败: %s", e)


def notify_existing_instance(title: str = WINDOW_TITLE,
                             show_dialog: bool = True) -> str:
    """第二个实例的处理：先叫窗口，叫不出来再弹提示。

    返回 "raised" / "dialog" / "none"。
    """
    log = get_logger("single")
    if bring_to_front(title):
        log.info("已有实例在运行：已把它的窗口叫到前台，本次启动直接退出")
        return "raised"
    log.info("已有实例在运行：没找到它的窗口，弹提示后退出")
    if show_dialog:
        _dialog()
        return "dialog"
    return "none"

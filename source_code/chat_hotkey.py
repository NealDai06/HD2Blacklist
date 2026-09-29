# -*- coding: utf-8 -*-
"""chat_hotkey.py —— 聊天框扫描快捷键（可自定义，默认关闭；**全局生效**）。

为什么从「按键轮询」改成「系统热键」：
    * 旧实现用 `GetAsyncKeyState` 轮询。轮到游戏（Helldivers 2 带反作弊、
      常常以前台全屏 / 高权限运行）时，低权限进程经常读不到按键状态，
      表现就是「只有本程序窗口在前台时按 F8 才有反应、游戏里按了没反应」。
    * 现在改成 `RegisterHotKey`：由系统把 `WM_HOTKEY` 投递到本进程的消息
      队列，**与前台窗口是谁无关**，所以游戏里也能触发。
    * 它不是键盘钩子、不注入任何进程、不读游戏内存 —— 仍然满足
      「不注入、不读内存、不改包」的硬性约束。
    * 注册失败（被别的程序占用、或系统保留键如 F12）时**自动退回轮询模式**，
      并把原因写进日志 / 状态文案，功能不会整个失效。

注意：RegisterHotKey 会被系统「独占」——注册成功后该按键不再下发给前台
程序（这正是它能在游戏里生效的原因）。所以请选一个游戏里不用的键。

组合键匹配规则（轮询模式下保留原语义）：
    - Ctrl / Alt 必须与绑定**完全一致**（绑了就必按，没绑就不能按）
    - Shift **只有绑定时才要求**，这样游戏里按着 Shift 跑动时
      依然能触发 F8 这类不带 Shift 的热键
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes

from config import (CHAT_SCAN_HOTKEY_DEBOUNCE, CHAT_SCAN_HOTKEY_SOURCE,
                    CHAT_SCAN_HOTKEY_VK, get_logger)

VK_CONTROL = 0x11
VK_MENU = 0x12          # Alt
VK_SHIFT = 0x10

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HWND_MESSAGE = -3

#: WM_HOTKEY 的 wParam —— 本进程内唯一即可
HOTKEY_ID = 0xB1C0

ERROR_HOTKEY_ALREADY_REGISTERED = 1409


# ==========================================================================
# 轮询模式用的按键状态（测试里会被替换掉）
# ==========================================================================
def _get_async_key_state(vk: int) -> int:
    """单独抽出来，方便测试替换。"""
    import win32api
    return win32api.GetAsyncKeyState(vk)


def _is_down(vk: int) -> bool:
    return bool(_get_async_key_state(vk) & 0x8000)


# ==========================================================================
# RegisterHotKey 相关的 ctypes 封装（全部单独抽出来，方便测试注入假实现）
# ==========================================================================
_USER32 = None
_KERNEL32 = None
_LOAD_TRIED = False


def _user32():
    """惰性加载 user32，并**显式声明参数类型**（默认 c_int 会截断 64 位句柄）。"""
    global _USER32, _LOAD_TRIED
    if _LOAD_TRIED:
        return _USER32
    _LOAD_TRIED = True
    if os.name != "nt":
        return None
    try:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u.CreateWindowExW.restype = wintypes.HWND
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.DestroyWindow.restype = wintypes.BOOL
        u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                     wintypes.UINT, wintypes.UINT]
        u.RegisterHotKey.restype = wintypes.BOOL
        u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        u.UnregisterHotKey.restype = wintypes.BOOL
        u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                  wintypes.UINT, wintypes.UINT]
        u.GetMessageW.restype = ctypes.c_int
        u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                         wintypes.WPARAM, wintypes.LPARAM]
        u.PostThreadMessageW.restype = wintypes.BOOL
        _USER32 = u
    except Exception:                                    # noqa: BLE001
        _USER32 = None
    return _USER32


def _kernel32():
    global _KERNEL32
    if _KERNEL32 is None and os.name == "nt":
        try:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.GetCurrentThreadId.restype = wintypes.DWORD
            _KERNEL32 = k
        except Exception:                                # noqa: BLE001
            _KERNEL32 = None
    return _KERNEL32


def register_supported() -> bool:
    """当前环境能不能用系统热键（非 Windows / 加载失败 → 用轮询）。"""
    return _user32() is not None


def _create_message_window():
    """创建一个隐藏的 message-only 窗口，只用来接收 WM_HOTKEY。"""
    u = _user32()
    if u is None:
        return None
    hwnd = u.CreateWindowExW(0, "STATIC", "hd2_hotkey_sink", 0, 0, 0, 0, 0,
                             wintypes.HWND(HWND_MESSAGE), None, None, None)
    return hwnd or None


def _destroy_window(hwnd) -> None:
    u = _user32()
    if u is not None and hwnd:
        u.DestroyWindow(hwnd)


def _register_hotkey(hwnd, hotkey_id: int, mods: int, vk: int) -> bool:
    u = _user32()
    if u is None:
        return False
    return bool(u.RegisterHotKey(hwnd, int(hotkey_id), int(mods), int(vk)))


def _unregister_hotkey(hwnd, hotkey_id: int) -> bool:
    u = _user32()
    if u is None:
        return False
    return bool(u.UnregisterHotKey(hwnd, int(hotkey_id)))


def _get_message(msg_ptr, hwnd=None) -> int:
    """取一条消息（hwnd=None → 取本线程所有消息，含 PostThreadMessage）。"""
    u = _user32()
    if u is None:
        return -1
    return int(u.GetMessageW(msg_ptr, hwnd, 0, 0))


def _post_thread_quit(thread_id: int) -> bool:
    u = _user32()
    if u is None or not thread_id:
        return False
    return bool(u.PostThreadMessageW(int(thread_id), WM_QUIT, 0, 0))


def _current_thread_id() -> int:
    k = _kernel32()
    return int(k.GetCurrentThreadId()) if k is not None else 0


def _last_error() -> int:
    return int(ctypes.get_last_error())


# ==========================================================================
class ChatScanHotkey:
    """按一次（组合）热键 → 扫描一次聊天框。

    优先注册成**系统全局热键**（游戏里也能用）；注册不了就退回按键轮询。
    两种模式都走同一个 `_on_press()`（去抖 + 独立线程扫描，不阻塞）。
    """

    POLL_INTERVAL = 0.1

    MODE_OFF = "off"
    MODE_REGISTER = "register"      # 系统全局热键
    MODE_POLLING = "polling"        # GetAsyncKeyState 轮询

    def __init__(self, chat_scanner, hotkey=None,
                 vk_code: int = CHAT_SCAN_HOTKEY_VK,
                 debounce: float = CHAT_SCAN_HOTKEY_DEBOUNCE,
                 active_check=None, use_register_hotkey: bool = True):
        """hotkey: {"vk":int, "ctrl":bool, "alt":bool, "shift":bool}
        不传则退回 vk_code（兼容旧调用）。"""
        self.chat_scanner = chat_scanner

        hk = dict(hotkey or {})
        self.vk = int(hk.get("vk", vk_code))
        self.ctrl = bool(hk.get("ctrl", False))
        self.alt = bool(hk.get("alt", False))
        self.shift = bool(hk.get("shift", False))

        self.debounce = float(debounce)
        self.active_check = active_check        # 可选：返回 False 时忽略按键
        self.use_register_hotkey = bool(use_register_hotkey)
        self.log = get_logger("chat_hotkey")

        self.mode = self.MODE_OFF
        self.last_error = ""
        self.ready = threading.Event()          # 注册结果已确定

        self._was_down = False
        self._last_press = 0.0
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread = None
        self._hwnd = None
        self._tid = 0
        self._registered = False
        self.trigger_count = 0

    # ---------------------------------------------------------------- 控制
    @property
    def display_name(self) -> str:
        from hotkey_config import combo_name
        return combo_name(self.vk, self.ctrl, self.alt, self.shift)

    def start(self):
        """启动热键线程。**被 stop() 取消过的实例不会复活**（防竞态）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            if self._stop.is_set():
                return None
            self.ready.clear()
            self.last_error = ""

            if self.use_register_hotkey and register_supported():
                target, self.mode = self._register_loop, self.MODE_REGISTER
            else:
                target, self.mode = self._loop, self.MODE_POLLING
                self.last_error = "当前环境不支持系统热键"
                self.ready.set()

            self._thread = threading.Thread(target=target, daemon=True,
                                            name="ChatScanHotkey")
            thread = self._thread
            thread.start()
            return thread

    def wait_ready(self, timeout: float = 1.5) -> bool:
        """等注册结果确定（正常是毫秒级）。"""
        return bool(self.ready.wait(timeout))

    def stop(self, wait: bool = False, timeout: float = 2.0) -> None:
        with self._lock:
            self._stop.set()
            tid = self._tid
            thread = self._thread
        if tid:
            try:
                _post_thread_quit(tid)                   # 唤醒 GetMessage
            except Exception:                            # noqa: BLE001
                pass
        if wait and thread is not None and thread.ident is not None:
            try:
                thread.join(timeout=timeout)
            except RuntimeError:                         # 线程还没真正起来
                pass

    @property
    def enabled(self) -> bool:
        return not self._stop.is_set()

    @property
    def registered(self) -> bool:
        """是否真的挂上了系统全局热键。"""
        return bool(self._registered)

    def status_text(self) -> str:
        """一句人能看懂的当前状态（给 GUI 状态栏 / 日志用）。"""
        if self.mode == self.MODE_REGISTER:
            return (f"扫描热键 {self.display_name} 已启用"
                    "（系统全局热键：游戏里也能用）")
        if self.mode == self.MODE_POLLING:
            why = f"：{self.last_error}" if self.last_error else ""
            return (f"扫描热键 {self.display_name} 已启用"
                    f"（按键轮询模式{why}）")
        return f"扫描热键 {self.display_name} 未启动"

    # ------------------------------------------------------------ 组合键判定
    def register_mods(self) -> int:
        """转成 RegisterHotKey 的 fsModifiers 位。"""
        mods = MOD_NOREPEAT
        if self.ctrl:
            mods |= MOD_CONTROL
        if self.alt:
            mods |= MOD_ALT
        if self.shift:
            mods |= MOD_SHIFT
        return mods

    def combo_down(self) -> bool:
        """当前是否正好按着绑定的组合键（仅轮询模式使用）。"""
        ctrl_down = _is_down(VK_CONTROL)
        alt_down = _is_down(VK_MENU)
        if ctrl_down != self.ctrl:
            return False
        if alt_down != self.alt:
            return False
        if self.shift and not _is_down(VK_SHIFT):
            return False
        return _is_down(self.vk)

    # -------------------------------------------------- 模式一：系统全局热键
    def _register_loop(self):
        self._tid = _current_thread_id()
        hwnd = None
        reason = ""
        try:
            hwnd = _create_message_window()
            if not hwnd:
                reason = "创建隐藏消息窗口失败"
            elif not _register_hotkey(hwnd, HOTKEY_ID, self.register_mods(),
                                      self.vk):
                err = _last_error()
                reason = ("该按键已被其它程序占用"
                          if err == ERROR_HOTKEY_ALREADY_REGISTERED
                          else f"系统拒绝注册（错误码 {err}）")
        except Exception as e:                           # noqa: BLE001
            reason = f"注册异常: {e}"

        if reason:
            if hwnd:
                try:
                    _destroy_window(hwnd)
                except Exception:                        # noqa: BLE001
                    pass
            self._hwnd = None
            self._tid = 0
            self.last_error = reason
            self.mode = self.MODE_POLLING
            self.ready.set()
            self.log.warning("系统热键 %s 不可用（%s）→ 退回按键轮询模式",
                             self.display_name, reason)
            self._loop()
            return

        self._hwnd = hwnd
        self._registered = True
        self.mode = self.MODE_REGISTER
        self.ready.set()
        self.log.info("聊天框扫描热键 %s 已注册为系统全局热键"
                      "（游戏内前台也有效；该键不再下发给其它程序）",
                      self.display_name)
        try:
            msg = wintypes.MSG()
            while not self._stop.is_set():
                r = _get_message(ctypes.byref(msg))
                if r in (0, -1):                         # WM_QUIT / 出错
                    break
                if msg.message == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID:
                    self._maybe_press()
        except Exception as e:                           # noqa: BLE001
            self.log.error("热键消息循环异常退出: %s", e)
        finally:
            try:
                _unregister_hotkey(hwnd, HOTKEY_ID)
            except Exception:                            # noqa: BLE001
                pass
            try:
                _destroy_window(hwnd)
            except Exception:                            # noqa: BLE001
                pass
            self._registered = False
            self._hwnd = None
            self._tid = 0
            self.log.info("聊天框扫描热键 %s 已注销", self.display_name)

    # ------------------------------------------------------ 模式二：按键轮询
    def _loop(self):
        self.log.info("聊天框扫描热键启动（%s 轮询模式 去抖=%.1fs 轮询=%.0fms）",
                      self.display_name, self.debounce,
                      self.POLL_INTERVAL * 1000)
        while not self._stop.wait(self.POLL_INTERVAL):
            if self.active_check is not None and not self.active_check():
                self._was_down = False
                continue
            try:
                down = self.combo_down()
            except Exception as e:                       # noqa: BLE001
                self.log.warning("GetAsyncKeyState 失败，热键停用: %s", e)
                return

            if down and not self._was_down:
                self._on_press()
            self._was_down = down

    # ---------------------------------------------------------------- 触发
    def _maybe_press(self):
        """全局热键路径：先过 active_check，再去抖。"""
        if self.active_check is not None:
            try:
                if not self.active_check():
                    return
            except Exception as e:                       # noqa: BLE001
                self.log.warning("active_check 异常，忽略本次按键: %s", e)
                return
        self._on_press()

    def _on_press(self):
        now = time.time()
        if now - self._last_press < self.debounce:
            return
        self._last_press = now
        self.trigger_count += 1
        self.log.info("[ChatScanHotkey] 检测到 %s，开始扫描聊天框",
                      self.display_name)
        threading.Thread(
            target=self.chat_scanner.scan_now,
            args=(CHAT_SCAN_HOTKEY_SOURCE,),
            daemon=True, name="ChatScanHotkeyScan",
        ).start()

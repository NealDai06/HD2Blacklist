# -*- coding: utf-8 -*-
"""notifier.py —— 无焦点 Overlay 提示。

硬性要求（不打断游戏）：
    窗口必须带 WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
    禁止用 tkinter.Toplevel + -topmost（那个会抢焦点、会进 Alt-Tab）

实现方式：纯 Win32 分层窗口（UpdateLayeredWindow）+ PIL 离屏渲染，
窗口由一个专用守护线程持有并泵消息，主线程只往队列里塞请求。

可自定义内容全部来自 NotificationConfig：
    文案模板 / 占位符 / 字段开关 / 图片 / 颜色 / 透明度 / 尺寸 / 位置 / 时长 / 音效
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import queue
import threading
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from config import (ASSETS_DIR, DEFAULT_BODY_FONT_SIZE, DEFAULT_ICON_PATH,
                    DEFAULT_TITLE_FONT_SIZE, FONT_SIZE_MAX, FONT_SIZE_MIN,
                    MAX_NOTIFY_STACK, NOTIFICATION_POSITIONS,
                    NOTIFY_STACK_BASE_Y, NOTIFY_STACK_GAP, get_logger)

# ==========================================================================
# Win32 绑定
# ==========================================================================
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008

WS_POPUP = 0x80000000
SW_SHOWNOACTIVATE = 4
SW_HIDE = 0
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, wt.HWND, wt.UINT,
                             wt.WPARAM, wt.LPARAM)

#: Overlay 的窗口类名 + **进程级常驻**的窗口过程。
#:
#: 为什么必须是模块级、只建一次（曾经的真 bug，会以原生崩溃形式出现）：
#:     `RegisterClassW` 注册的窗口类在进程内是**全局且永久**的，
#:     第二次注册会返回 1410（已注册）。而 `CreateWindowExW` 用的是
#:     **首次注册时**记下的那个 WndProc 指针 —— 如果回调是每个窗口实例
#:     各自创建的 Python 对象，一旦那个实例被 GC，
#:     Windows 仍会调用已释放的地址 → 进程直接死掉，
#:     退出码 `0xC000041D`（回调里抛异常）。
#:     所以：全局一份回调，永不释放。
OVERLAY_CLASS_NAME = "HD2BlacklistOverlayWnd"
_overlay_wndproc = None
_overlay_class_registered = False


def _overlay_wnd_proc(hwnd, msg, wparam, lparam):
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _ensure_overlay_class(hinst):
    """注册窗口类（进程内只做一次），返回进程级常驻的 WNDPROC。"""
    global _overlay_wndproc, _overlay_class_registered
    if _overlay_wndproc is None:
        _overlay_wndproc = WNDPROC(_overlay_wnd_proc)
    if not _overlay_class_registered:
        wc = WNDCLASSW()
        wc.lpfnWndProc = _overlay_wndproc
        wc.hInstance = hinst
        wc.lpszClassName = OVERLAY_CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):                     # 1410 = 已注册
                raise ctypes.WinError(err)
        _overlay_class_registered = True
    return _overlay_wndproc


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte),
                ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte),
                ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wt.DWORD * 3)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


LRESULT = ctypes.c_ssize_t

# --- 函数原型 -------------------------------------------------------------
# 64 位下必须显式声明 restype，否则句柄会被 ctypes 默认的 c_int 截断！
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT,
                                wt.UINT, wt.UINT]
user32.PeekMessageW.restype = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.SetProcessDPIAware.restype = wt.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wt.HWND

gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteDC.restype = wt.BOOL
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.CreateDIBSection.argtypes = [
    wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
    ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP

user32.UpdateLayeredWindow.argtypes = [
    wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
    wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.UpdateLayeredWindow.restype = wt.BOOL

# ==========================================================================
# 字体 / 图标
# ==========================================================================
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",     # 黑体
    r"C:\Windows\Fonts\simsun.ttc",     # 宋体
    r"C:\Windows\Fonts\arial.ttf",
)
_font_cache: dict = {}
_font_lock = threading.Lock()


def load_font(size: int):
    """取一个支持中文的字体，找不到就退回 PIL 内置位图字体。"""
    size = max(8, int(size))
    with _font_lock:
        if size in _font_cache:
            return _font_cache[size]
        font = None
        for path in _FONT_CANDIDATES:
            if os.path.exists(path):
                try:
                    font = ImageFont.truetype(path, size)
                    break
                except Exception:                        # noqa: BLE001
                    continue
        if font is None:
            font = ImageFont.load_default()
        _font_cache[size] = font
        return font


def ensure_default_icon(path: str = DEFAULT_ICON_PATH) -> str:
    """生成默认提示图（黄色警告三角 + 感叹号）。已存在则不覆盖。"""
    if os.path.exists(path):
        return path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    size = 128
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 10
    d.polygon([(size // 2, pad), (size - pad, size - pad), (pad, size - pad)],
              fill=(255, 196, 0, 255), outline=(80, 60, 0, 255))
    d.rectangle([size // 2 - 6, 42, size // 2 + 6, 84], fill=(40, 30, 0, 255))
    d.rectangle([size // 2 - 6, 94, size // 2 + 6, 106], fill=(40, 30, 0, 255))
    tmp = path + ".tmp"
    img.save(tmp, "PNG")
    os.replace(tmp, path)
    return path


_image_cache: dict = {}
_image_lock = threading.Lock()


def load_image_cached(path: str):
    """带 mtime 校验的图片缓存，避免每次弹窗都读盘。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _image_lock:
        hit = _image_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        img = Image.open(path).convert("RGBA")
    except Exception:                                    # noqa: BLE001
        return None
    with _image_lock:
        if len(_image_cache) > 16:
            _image_cache.clear()
        _image_cache[path] = (mtime, img)
    return img


# ==========================================================================
# 模板渲染
# ==========================================================================
class _SafeDict(dict):
    """未知占位符原样保留，避免一条坏模板导致崩溃。"""

    def __missing__(self, key):
        return "{" + key + "}"


def build_fields(entry, score, source) -> dict:
    entry = entry or {}
    name = entry.get("player_name") or entry.get("player_id") or "未知玩家"
    return {
        "player_name": str(name),
        "player_id": str(entry.get("player_id") or ""),
        "match_score": f"{float(score or 0):.0f}",
        "note": str(entry.get("note") or "无"),
        "tk_count": str(entry.get("tk_count") or 0),
        "source": str(source or ""),
        "time": datetime.now().strftime("%H:%M:%S"),
        "last_seen": str(entry.get("last_seen") or "首次"),
    }


def render_template(tpl: str, entry, score, source) -> str:
    """渲染模板，支持 {player_name} {match_score} {note} {tk_count}
    {source} {time} {last_seen}。"""
    if tpl is None:
        return ""
    try:
        return str(tpl).format_map(_SafeDict(build_fields(entry, score, source)))
    except Exception:                                    # noqa: BLE001
        return str(tpl)


#: show_fields 里的开关 → 它们控制的占位符
FIELD_PLACEHOLDERS = {
    "note": ("{note}",),
    "tk_count": ("{tk_count}",),
    "match_score": ("{match_score}",),
    "time": ("{time}",),
    "source": ("{source}",),
    "last_seen": ("{last_seen}",),
}


def filter_hidden_lines(body: str, show_fields: dict) -> str:
    """按 show_fields 丢掉整行都只由被关闭字段构成的行。"""
    if not show_fields:
        return body
    enabled = []
    for key, placeholders in FIELD_PLACEHOLDERS.items():
        if show_fields.get(key, True):
            enabled.extend(placeholders)
    out = []
    for line in str(body).split("\n"):
        holders = [p for p in FIELD_PLACEHOLDERS.values() for p in p
                   if p in line]
        if holders and not any(h in enabled for h in holders):
            continue
        out.append(line)
    return "\n".join(out).strip("\n")


def hex_to_rgb(value, default=(43, 43, 43)):
    try:
        v = str(value).lstrip("#")
        if len(v) == 3:
            v = "".join(c * 2 for c in v)
        return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:                                    # noqa: BLE001
        return default


def clamp_font_size(value, default: int) -> int:
    """把字号夹到合理区间；非法输入回退默认值。"""
    try:
        size = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, size))


# ==========================================================================
# 渲染 Overlay 画面
# ==========================================================================
def render_overlay(cfg: dict, entry, score, source,
                   width: int, height: int) -> Image.Image:
    """把通知渲染成一张 RGBA 图（背景 + 图片 + 标题 + 正文）。"""
    ap = cfg.get("appearance", {}) or {}
    bg = hex_to_rgb(ap.get("background_color"), (43, 43, 43))
    fg = hex_to_rgb(ap.get("text_color"), (255, 255, 255))
    title_color = hex_to_rgb(ap.get("title_color"), (255, 85, 85))
    opacity = float(ap.get("opacity", 0.85) or 0.0)
    opacity = min(1.0, max(0.0, opacity))
    radius = 10

    # 字号可在 GUI 里调（appearance.title_font_size / body_font_size）
    title_size = clamp_font_size(ap.get("title_font_size"),
                                 DEFAULT_TITLE_FONT_SIZE)
    body_size = clamp_font_size(ap.get("body_font_size"),
                                DEFAULT_BODY_FONT_SIZE)

    title = render_template(cfg.get("title_template", ""), entry, score, source)
    body_raw = cfg.get("body_template", "")
    body = render_template(filter_hidden_lines(body_raw,
                                               cfg.get("show_fields") or {}),
                           entry, score, source)

    # ---- 图片 ----
    mode = str(cfg.get("image_mode", "default")).lower()
    icon = None
    if mode == "default":
        icon = load_image_cached(ensure_default_icon())
    elif mode == "custom":
        p = cfg.get("image_path") or ""
        icon = load_image_cached(p) if p else None
        if icon is None and p:
            icon = load_image_cached(ensure_default_icon())
    if icon is not None:
        size = cfg.get("image_size") or [64, 64]
        try:
            iw, ih = max(8, int(size[0])), max(8, int(size[1]))
        except Exception:                                # noqa: BLE001
            iw, ih = 64, 64
        icon = icon.copy()
        icon.thumbnail((iw, ih), Image.LANCZOS)

    # ---- 文本换行 ----
    # 内边距 / 行高都跟着字号走，字号调大时整体一起放大，不会被裁掉
    pad = max(12, int(round(body_size * 0.9)))
    title_font = load_font(title_size)
    body_font = load_font(body_size)
    text_left = pad + (icon.width + 10 if icon else 0)
    text_width = max(40, width - text_left - pad)

    body_lines = []
    for raw_line in (body or "").split("\n"):
        if not raw_line:
            body_lines.append("")
            continue
        cur = ""
        for ch in raw_line:
            if body_font.getlength(cur + ch) <= text_width:
                cur += ch
            else:
                body_lines.append(cur)
                cur = ch
        body_lines.append(cur)

    title_h = title_size + 6
    line_h = body_size + 5
    needed = pad * 2 + title_h + line_h * len(body_lines)
    if icon is not None:
        needed = max(needed, pad * 2 + icon.height + 4)
    height = max(int(height), int(needed))
    width = max(120, int(width))

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, width - 1, height - 1], radius=radius,
                        fill=bg + (int(opacity * 255),),
                        outline=(0, 0, 0, int(min(255, opacity * 255))))

    y = pad
    if icon is not None:
        # 图标与标题行居中对齐
        img.alpha_composite(icon, (pad, y + max(0, (title_h - icon.height) // 2)))

    d.text((text_left, y), title, font=title_font, fill=title_color + (255,))
    y += title_h
    for line in body_lines:
        d.text((text_left, y), line, font=body_font, fill=fg + (255,))
        y += line_h
    return img


# ==========================================================================
# 分层窗口
# ==========================================================================
class _OverlaySurface:
    """一个分层窗口（通知堆叠里的一个槽位）。只在 Overlay 线程里使用。"""

    def __init__(self, index: int):
        self.index = index
        self.hwnd = None
        self.hide_at = 0.0
        self._wndproc_ref = None

    def create(self):
        hinst = kernel32.GetModuleHandleW(None)
        # 窗口过程是进程级常驻对象，绝不按实例创建（见模块顶部说明）
        self._wndproc_ref = _ensure_overlay_class(hinst)
        ex_style = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
                    | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
        hwnd = user32.CreateWindowExW(
            ex_style, OVERLAY_CLASS_NAME,
            f"HD2BlacklistOverlay{self.index}", WS_POPUP,
            0, 0, 10, 10, None, None, hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = hwnd

    def paint(self, image: Image.Image, x: int, y: int, duration: float):
        w, h = image.size
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        hbmp = None
        old = None
        try:
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h              # 负数 = 自上而下
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = BI_RGB

            bits = ctypes.c_void_p()
            hbmp = gdi32.CreateDIBSection(
                mem_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
                ctypes.byref(bits), None, 0)
            if not hbmp or not bits:
                raise ctypes.WinError(ctypes.get_last_error())
            old = gdi32.SelectObject(mem_dc, hbmp)

            # UpdateLayeredWindow 需要「预乘 alpha」的 BGRA
            import numpy as np
            arr = np.asarray(image.convert("RGBA"), dtype=np.uint16)
            a = arr[..., 3:4]
            rgb = (arr[..., :3] * a // 255).astype(np.uint8)
            bgra = np.concatenate(
                [rgb[..., 2:3], rgb[..., 1:2], rgb[..., 0:1],
                 arr[..., 3:4].astype(np.uint8)], axis=2)
            buf = bgra.tobytes()
            ctypes.memmove(bits, buf, len(buf))

            size = wt.SIZE(w, h)
            src = wt.POINT(0, 0)
            dst = wt.POINT(x, y)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            if not user32.UpdateLayeredWindow(
                    self.hwnd, screen_dc, ctypes.byref(dst),
                    ctypes.byref(size), mem_dc, ctypes.byref(src), 0,
                    ctypes.byref(blend), ULW_ALPHA):
                raise ctypes.WinError(ctypes.get_last_error())

            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            # 重新压到最顶层；SWP_NOACTIVATE 保证不抢焦点
            user32.SetWindowPos(self.hwnd, -1, x, y, w, h, 0x0010)
            self.hide_at = time.time() + max(0.2, duration)
        finally:
            if old:
                gdi32.SelectObject(mem_dc, old)
            if hbmp:
                gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)

    def hide(self):
        self.hide_at = 0.0
        if self.hwnd:
            try:
                user32.ShowWindow(self.hwnd, SW_HIDE)
            except Exception:                            # noqa: BLE001
                pass

    def destroy(self):
        if self.hwnd:
            try:
                user32.DestroyWindow(self.hwnd)
            except Exception:                            # noqa: BLE001
                pass
            self.hwnd = None


class _OverlayWindow:
    """一组常驻分层窗口（每个堆叠槽位一个），由专属线程持有。

    为什么必须是**一组**而不是一个（这曾经是个真 bug）：
        `UpdateLayeredWindow` 会把**整个窗口**重绘成新图，并把它移动到新位置。
        批量命中时若 N 个通知栏共用一个窗口，后画的会把先画的整个盖掉 ——
        用户最终只看得见**最后一条**；音效又只响一次，于是现象就是
        "一次命中多个黑名单玩家，却只播报了一个人，还说不清是谁"。
        每个槽位一个窗口之后，N 条就真的并排显示 N 栏。

    对外接口保持不变（show/hide/close/hwnd/available）。
    """

    def __init__(self, on_error=None):
        self.log = get_logger("notifier")
        self.available = False
        self._queue = queue.Queue(maxsize=max(4, MAX_NOTIFY_STACK * 2))
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._on_error = on_error
        self._surfaces = []           # [_OverlaySurface]，索引 = 堆叠槽位
        self._wndproc_ref = None      # 进程级常驻回调，见 _ensure_overlay_class

    # ------------------------------------------------------------ 生命周期
    def start(self, timeout: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="OverlayWindow")
        self._thread.start()
        self._ready.wait(timeout)
        return self.available

    @property
    def hwnd(self):
        """第一个槽位的窗口句柄（自检用）。"""
        return self._surfaces[0].hwnd if self._surfaces else None

    def _surface(self, slot: int):
        """取（必要时创建）第 slot 个槽位窗口；超出上限返回 None。"""
        slot = max(0, int(slot))
        if slot >= MAX_NOTIFY_STACK:
            return None
        while len(self._surfaces) <= slot:
            index = len(self._surfaces)
            surface = _OverlaySurface(index)
            surface.create()
            self._surfaces.append(surface)
            self.log.info("Overlay 窗口 #%d 已创建"
                          "（NOACTIVATE|TRANSPARENT|TOOLWINDOW|TOPMOST）", index)
        return self._surfaces[slot]

    def _run(self):
        try:
            self._surface(0)                     # 先建好第一个，供 --check 用
            self.available = True
        except Exception as e:                   # noqa: BLE001
            self.log.error("Overlay 窗口创建失败: %s", e)
            if self._on_error:
                self._on_error(e)
            self._ready.set()
            return
        self._ready.set()

        msg = wt.MSG()
        try:
            while not self._stop.is_set():
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == 0x0012:            # WM_QUIT
                        self._stop.set()
                        break
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                self._pump_requests()
                self._expire()
                time.sleep(0.02)
        finally:
            for surface in self._surfaces:
                surface.destroy()

    def _expire(self):
        """到点就把对应的槽位窗口收起来。"""
        now = time.time()
        for surface in self._surfaces:
            if surface.hide_at and now >= surface.hide_at:
                surface.hide()

    # ---------------------------------------------------------------- 请求
    def show(self, image: Image.Image, x: int, y: int, duration: float,
             slot: int = 0):
        """把一张图排到第 slot 个堆叠槽位显示。"""
        item = (int(slot), image, int(x), int(y), float(duration))
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # 队列满了丢最旧的，保证最新一次命中一定显示得出来
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except queue.Empty:
                pass

    def _pump_requests(self):
        while True:
            try:
                slot, image, x, y, duration = self._queue.get_nowait()
            except queue.Empty:
                return
            except Exception as e:                           # noqa: BLE001
                self.log.warning("Overlay 请求解析失败: %s", e)
                return
            try:
                surface = self._surface(slot)
                if surface is None:
                    self.log.debug("超过堆叠上限，跳过第 %d 个通知栏", slot + 1)
                    continue
                surface.paint(image, x, y, duration)
            except Exception as e:                           # noqa: BLE001
                self.log.warning("Overlay 绘制失败: %s", e)

    def hide(self):
        """收起所有通知栏。"""
        for surface in self._surfaces:
            surface.hide()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)


# ==========================================================================
# 音效
# ==========================================================================
class SoundPlayer:
    """beep / 自定义 wav / 静音。播放永远不阻塞调用线程。"""

    def __init__(self):
        self.log = get_logger("sound")
        self._pygame = None
        self._sound_cache = {}
        self._lock = threading.Lock()

    def play(self, sound_cfg: dict) -> None:
        if not sound_cfg or not sound_cfg.get("enabled", True):
            return
        mode = str(sound_cfg.get("mode", "beep")).lower()
        if mode == "none":
            return
        cfg = dict(sound_cfg)
        threading.Thread(target=self._play_sync, args=(cfg, mode),
                         daemon=True, name="SoundPlay").start()

    def _play_sync(self, cfg, mode):
        try:
            if mode == "wav":
                self._play_wav(cfg.get("path") or "")
            else:
                self._play_beep(int(cfg.get("beep_freq", 1200)),
                                int(cfg.get("beep_duration", 120)))
        except Exception as e:                           # noqa: BLE001
            self.log.debug("音效播放失败: %s", e)

    @staticmethod
    def _play_beep(freq: int, duration: int):
        import winsound
        freq = min(32767, max(37, int(freq)))
        duration = max(30, min(2000, int(duration)))
        winsound.Beep(freq, duration)

    def _play_wav(self, path: str):
        if not path or not os.path.exists(path):
            self.log.debug("wav 文件不存在: %s", path)
            return
        import pygame
        with self._lock:
            if self._pygame is None:
                try:
                    pygame.mixer.init()
                except Exception as e:                   # noqa: BLE001
                    self.log.warning("pygame.mixer 初始化失败: %s", e)
                    return
                self._pygame = pygame
            sound = self._sound_cache.get(path)
            if sound is None:
                try:
                    sound = pygame.mixer.Sound(path)
                except Exception as e:                   # noqa: BLE001
                    self.log.warning("加载 wav 失败 %s: %s", path, e)
                    return
                if len(self._sound_cache) > 8:
                    self._sound_cache.clear()
                self._sound_cache[path] = sound
            sound.play()

    def stop(self):
        with self._lock:
            if self._pygame is not None:
                try:
                    self._pygame.mixer.stop()
                except Exception:                        # noqa: BLE001
                    pass


# ==========================================================================
# Notifier
# ==========================================================================
class Notifier:
    """对外只暴露 alert(entry, score, source)。"""

    def __init__(self, notification_config, root=None):
        self.cfg = notification_config
        self.log = get_logger("notifier")
        self.sound = SoundPlayer()
        # 系统通知（winotify）只在 Overlay 不可用时作为兜底；可整体关闭。
        self.toast_enabled = True
        self._winotify = None
        self._overlay = _OverlayWindow(on_error=self._on_overlay_error)
        self.overlay_ok = self._overlay.start()
        if not self.overlay_ok:
            self.log.warning("Overlay 不可用，将只使用系统通知 / 音效")
        try:
            ensure_default_icon()
        except Exception as e:                           # noqa: BLE001
            self.log.warning("生成默认提示图失败: %s", e)

    def _on_overlay_error(self, exc):
        self.log.error("Overlay 初始化异常: %s", exc)

    # ---------------------------------------------------------------- 位置
    def _target_position(self, cfg: dict, w: int, h: int):
        ap = cfg.get("appearance", {}) or {}
        pos = str(ap.get("position", "top_right")).lower()
        if pos not in NOTIFICATION_POSITIONS:
            pos = "top_right"
        margin = 16

        left, top, sw, sh = self._screen_rect(ap.get("monitor"))

        # 顶部位置留出更大的上边距，给多栏堆叠留空间
        top_margin = NOTIFY_STACK_BASE_Y if pos.startswith("top") else margin

        if pos == "top_left":
            x, y = left + margin, top + top_margin
        elif pos == "top_center":
            x, y = left + (sw - w) // 2, top + top_margin
        elif pos == "top_right":
            x, y = left + sw - w - margin, top + top_margin
        elif pos == "bottom_left":
            x, y = left + margin, top + sh - h - margin
        elif pos == "bottom_center":
            x, y = left + (sw - w) // 2, top + sh - h - margin
        elif pos == "bottom_right":
            x, y = left + sw - w - margin, top + sh - h - margin
        else:                                            # center
            x, y = left + (sw - w) // 2, top + (sh - h) // 2
        return int(x), int(y)

    def _stacked_position(self, cfg, x, y, w, h, index):
        """计算堆叠后的位置；返回 None 表示放不下（应跳过绘制）。

        * top_* / center → 依次向下排
        * bottom_*       → 依次向上排
        * 做屏幕下沿 / 上沿保护，绝不把通知栏推到屏幕外
        """
        if index <= 0:
            return x, y
        ap = cfg.get("appearance", {}) or {}
        pos = str(ap.get("position", "top_right")).lower()
        step = h + NOTIFY_STACK_GAP
        left, top, sw, sh = self._screen_rect(ap.get("monitor"))

        if pos.startswith("bottom"):
            new_y = y - index * step
            if new_y < top + 4:                    # 顶到屏幕上沿 → 放不下
                return None
            return x, int(new_y)

        new_y = y + index * step
        if new_y + h > top + sh - 4:               # 顶到屏幕下沿
            new_y = top + sh - h - 4
            if new_y <= y:                         # 兜底也放不下
                return None
        return x, int(new_y)

    @staticmethod
    def _screen_rect(monitor=None):
        """返回 (left, top, width, height)。

        monitor 为 None/0 时使用整个虚拟桌面（可覆盖副屏）。
        """
        try:
            if monitor:
                import mss
                mons = mss.mss().monitors
                idx = int(monitor)
                if 0 < idx < len(mons):
                    m = mons[idx]
                    return m["left"], m["top"], m["width"], m["height"]
        except Exception:                                # noqa: BLE001
            pass
        try:
            user32.SetProcessDPIAware()
            return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
                    user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))
        except Exception:                                # noqa: BLE001
            return 0, 0, 1920, 1080

    # ---------------------------------------------------------------- 提示
    def alert(self, entry, score, source) -> bool:
        """单条提示（兼容原接口）。返回是否成功显示了 Overlay。"""
        return self._show_overlay(entry, score, source,
                                  play_sound=True, stack_index=0)

    def alert_batch(self, hits) -> int:
        """批量提示：**每个命中一个通知栏，但只播一次音效**。

        hits: [(entry, score, source), ...]
        返回实际弹出的通知栏数量。

        最多堆叠 MAX_NOTIFY_STACK 个。若命中数超过上限，**最后一个槽位会换成
        一条汇总栏**把剩下的玩家名都列出来 —— 否则用户根本不知道还有谁被命中了
        （之前的表现是"只播报了一个人，也不知道是哪一个"）。
        """
        if not hits:
            return 0

        # 音效只播一次（无论弹几个通知栏）
        self._play_sound_once()

        limit = max(1, int(MAX_NOTIFY_STACK))
        overflow = len(hits) > limit
        # 溢出时留一个槽位给汇总栏
        show_count = (limit - 1) if overflow else min(len(hits), limit)
        show_count = max(1, show_count) if hits else 0

        shown = 0
        for i, item in enumerate(hits[:show_count]):
            entry, score, source = item[0], item[1], item[2]
            if self._show_overlay(entry, score, source,
                                  play_sound=False, stack_index=i):
                shown += 1

        if overflow and shown:
            rest = hits[show_count:]
            if self._show_summary(rest, hits[0][2], stack_index=shown):
                shown += 1

        if len(hits) > limit:
            self.log.info("批量命中 %d 条，前 %d 个各弹一栏，其余汇总成 1 栏",
                          len(hits), show_count)
        if shown:
            self.log.info("批量提示：%d 个通知栏，1 次音效", shown)
        return shown

    def _show_summary(self, hits, source, stack_index: int) -> bool:
        """把「没抢到独立通知栏」的命中合并成一条汇总栏。"""
        if not hits:
            return False
        names = [str(h[0].get("player_name") or h[0].get("player_id") or "未知")
                 for h in hits]
        joined = "、".join(names)
        if len(joined) > 48:                       # 太长就截断，正文会自动换行
            joined = joined[:46] + "…"
        best = max((float(h[1] or 0) for h in hits), default=0.0)
        entry = {
            # 名字放在 player_name 而不是 note：note 可能被用户在
            # 通知设置里关掉（show_fields.note=False），player_name 永远会显示
            "player_name": f"等 {len(hits)} 名：{joined}",
            "player_id": "",
            "note": "同一次扫描命中的其余黑名单玩家",
            "tk_count": 0,
            "last_seen": "",
        }
        return self._show_overlay(entry, best, source,
                                  play_sound=False, stack_index=stack_index)

    def _play_sound_once(self):
        """播放一次提示音（独立线程，绝不阻塞调用方）。"""
        try:
            cfg = self.cfg.get()
            self.sound.play(cfg.get("sound") or {})
        except Exception as e:                           # noqa: BLE001
            self.log.debug("音效异常: %s", e)

    def _show_overlay(self, entry, score, source,
                      play_sound: bool = True, stack_index: int = 0) -> bool:
        """渲染并显示一个通知栏（不抢焦点）。

        play_sound=False 时只显示不发声 —— 批量提示靠它做到"多栏一次音效"。
        """
        cfg = self.cfg.get()
        shown = False
        try:
            ap = cfg.get("appearance", {}) or {}
            width = max(160, int(ap.get("width", 360)))
            height = max(60, int(ap.get("height", 100)))
            img = render_overlay(cfg, entry, score, source, width, height)
            x, y = self._target_position(cfg, img.width, img.height)
            pos = self._stacked_position(cfg, x, y, img.width, img.height,
                                         stack_index)
            if pos is None:
                self.log.debug("第 %d 个通知栏放不下，跳过弹出", stack_index + 1)
            else:
                x, y = pos
                duration = float((cfg.get("timing") or {}).get("duration", 4.0))
                if self._overlay.available:
                    self._overlay.show(img, x, y, duration,
                                       slot=stack_index)
                    shown = True
        except Exception as e:                           # noqa: BLE001
            self.log.warning("Overlay 提示失败: %s", e)

        if play_sound:
            self._play_sound_once()

        # Overlay 不可用时退回系统通知（不抢焦点，且不进全屏游戏画面）
        if not shown:
            self._toast(cfg, entry, score, source)
        return shown

    def preview(self, cfg: dict = None, entry=None, score=100.0,
                source="preview") -> bool:
        """用假数据或指定配置做一次即时预览（供 gui_notification 使用）。"""
        real_cfg = self.cfg.get()
        cfg = cfg if cfg is not None else real_cfg
        entry = entry or {"player_name": "示例玩家", "player_id": "7656119",
                          "note": "恶意TK / 测试条目", "tk_count": 3,
                          "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        ap = cfg.get("appearance", {}) or {}
        width = max(160, int(ap.get("width", 360)))
        height = max(60, int(ap.get("height", 100)))
        img = render_overlay(cfg, entry, score, source, width, height)
        x, y = self._target_position(cfg, img.width, img.height)
        duration = float((cfg.get("timing") or {}).get("duration", 4.0))
        if self._overlay.available:
            self._overlay.show(img, x, y, duration)
            return True
        self.log.warning("Overlay 不可用，无法预览")
        return False

    def render_preview_image(self, cfg: dict, entry=None, score=100.0,
                             source="preview"):
        """只渲染不显示，返回 PIL.Image（GUI 内嵌预览用）。"""
        entry = entry or {"player_name": "示例玩家", "player_id": "7656119",
                          "note": "恶意TK / 测试条目", "tk_count": 3,
                          "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        ap = cfg.get("appearance", {}) or {}
        width = max(160, int(ap.get("width", 360)))
        height = max(60, int(ap.get("height", 100)))
        return render_overlay(cfg, entry, score, source, width, height)

    def play_sound(self, sound_cfg: dict = None) -> None:
        cfg = self.cfg.get()
        self.sound.play(sound_cfg if sound_cfg is not None
                        else (cfg.get("sound") or {}))

    # ---------------------------------------------------------------- Toast
    def _toast(self, cfg, entry, score, source):
        if not self.toast_enabled:
            return
        try:
            from winotify import Notification
            title = render_template(cfg.get("title_template", ""),
                                    entry, score, source)
            body = render_template(cfg.get("body_template", ""),
                                   entry, score, source)
            if self._winotify is None:
                self._winotify = Notification(
                    app_id="HD2 黑名单", title=title, msg=body)
            else:
                self._winotify.title = title
                self._winotify.msg = body
            self._winotify.show()
        except Exception as e:                           # noqa: BLE001
            self.log.debug("系统通知不可用: %s", e)

    # ---------------------------------------------------------------- 关闭
    def close(self):
        try:
            self._overlay.close()
        except Exception:                                # noqa: BLE001
            pass
        try:
            self.sound.stop()
        except Exception:                                # noqa: BLE001
            pass

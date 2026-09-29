# -*- coding: utf-8 -*-
"""hotkey_config.py —— 聊天框扫描快捷键的**可自定义**配置。

- 用户设置保存在 `data/hotkey.json`
- 支持单个主键 + Ctrl / Alt / Shift 修饰键
- 提供 keysym <-> 虚拟键码（VK）互转，供 GUI 里的"按键捕获框"使用
- 线程安全；文件损坏/缺失时回退默认值（F8）
"""
from __future__ import annotations

import json
import os
import re
import threading

from config import (CHAT_SCAN_HOTKEY_VK, DEFAULT_HOTKEY, HOTKEY_PATH,
                    get_logger)

#: 一些不允许绑定的键（会和其它功能打架）
VK_ESCAPE = 0x1B
FORBIDDEN_VK = {
    VK_ESCAPE: "ESC 已被「菜单扫描」占用，请换一个键",
}

#: VK -> 显示名（主键）
_VK_NAMES = {}
for _i in range(1, 25):
    _VK_NAMES[0x70 + _i - 1] = f"F{_i}"
for _c in range(ord("A"), ord("Z") + 1):
    _VK_NAMES[_c] = chr(_c)
for _d in range(ord("0"), ord("9") + 1):
    _VK_NAMES[_d] = chr(_d)
for _i in range(10):
    _VK_NAMES[0x60 + _i] = f"小键盘{_i}"
_VK_NAMES.update({
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x20: "空格",
    0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓",
    0x2C: "PrintScreen", 0x2D: "Insert", 0x2E: "Delete",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
})

#: Tk keysym -> VK（按键捕获框用）
_KEYSYM_SPECIAL = {
    "BackSpace": 0x08, "Tab": 0x09, "Return": 0x0D, "KP_Enter": 0x0D,
    "Escape": 0x1B, "space": 0x20, "Prior": 0x21, "Next": 0x22,
    "End": 0x23, "Home": 0x24, "Left": 0x25, "Up": 0x26,
    "Right": 0x27, "Down": 0x28, "Print": 0x2C, "Insert": 0x2D,
    "Delete": 0x2E,
    "semicolon": 0xBA, "equal": 0xBB, "comma": 0xBC, "minus": 0xBD,
    "period": 0xBE, "slash": 0xBF, "grave": 0xC0, "bracketleft": 0xDB,
    "backslash": 0xDC, "bracketright": 0xDD, "apostrophe": 0xDE,
    "KP_0": 0x60, "KP_1": 0x61, "KP_2": 0x62, "KP_3": 0x63, "KP_4": 0x64,
    "KP_5": 0x65, "KP_6": 0x66, "KP_7": 0x67, "KP_8": 0x68, "KP_9": 0x69,
    "KP_Multiply": 0x6A, "KP_Add": 0x6B, "KP_Subtract": 0x6D,
    "KP_Decimal": 0x6E, "KP_Divide": 0x6F,
}

#: 这些 keysym 表示"只按了修饰键"，捕获时应更新复选框而不是主键
_MODIFIER_KEYSYMS = {
    "Control_L": "ctrl", "Control_R": "ctrl",
    "Alt_L": "alt", "Alt_R": "alt",
    "Shift_L": "shift", "Shift_R": "shift",
}

_FUNC_RE = re.compile(r"^F([1-9]|1[0-9]|2[0-4])$")


# ==========================================================================
def keysym_to_vk(keysym: str):
    """把 Tk 的 keysym 转成 Windows 虚拟键码；无法识别返回 None。"""
    if not keysym:
        return None
    if _FUNC_RE.match(keysym):
        return 0x70 + int(keysym[1:]) - 1
    if len(keysym) == 1:
        ch = keysym.upper()
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            return ord(ch)
    return _KEYSYM_SPECIAL.get(keysym)


def modifier_of_keysym(keysym: str):
    """若 keysym 是纯修饰键，返回 'ctrl'/'alt'/'shift'，否则 None。"""
    return _MODIFIER_KEYSYMS.get(keysym)


def vk_to_name(vk) -> str:
    """虚拟键码 -> 显示名。"""
    try:
        vk = int(vk)
    except (TypeError, ValueError):
        return "?"
    return _VK_NAMES.get(vk, f"VK 0x{vk:02X}")


def combo_name(vk, ctrl=False, alt=False, shift=False) -> str:
    """组合键显示名，例如 Ctrl+Shift+F8。"""
    parts = []
    if ctrl:
        parts.append("Ctrl")
    if alt:
        parts.append("Alt")
    if shift:
        parts.append("Shift")
    parts.append(vk_to_name(vk))
    return "+".join(parts)


# ==========================================================================
class HotkeyConfig:
    """聊天框扫描快捷键配置（线程安全，落盘 data/hotkey.json）。"""

    def __init__(self, path: str = HOTKEY_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.log = get_logger("hotkey")
        self.data = dict(DEFAULT_HOTKEY)
        self._load()

    # ------------------------------------------------------------------ IO
    def _load(self) -> None:
        if not os.path.exists(self.path):
            with self.lock:
                self.data = dict(DEFAULT_HOTKEY)
            return
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("hotkey.json 根节点必须是对象")
            with self.lock:
                self.data = self._sanitize(raw)
        except Exception as e:                           # noqa: BLE001
            self.log.warning("读取 hotkey.json 失败，回退默认: %s", e)
            with self.lock:
                self.data = dict(DEFAULT_HOTKEY)

    @staticmethod
    def _sanitize(raw: dict) -> dict:
        """清洗外部数据，保证 vk 合法且不是禁用键。"""
        data = dict(DEFAULT_HOTKEY)
        try:
            vk = int(raw.get("vk", DEFAULT_HOTKEY["vk"]))
        except (TypeError, ValueError):
            vk = DEFAULT_HOTKEY["vk"]
        if vk in FORBIDDEN_VK or not (0x08 <= vk <= 0xFE):
            vk = DEFAULT_HOTKEY["vk"]
        data["vk"] = vk
        data["enabled"] = bool(raw.get("enabled", DEFAULT_HOTKEY["enabled"]))
        data["ctrl"] = bool(raw.get("ctrl", False))
        data["alt"] = bool(raw.get("alt", False))
        data["shift"] = bool(raw.get("shift", False))
        data["name"] = combo_name(vk, data["ctrl"], data["alt"], data["shift"])
        return data

    def _save(self) -> None:
        with self.lock:
            payload = dict(self.data)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def reload(self) -> dict:
        self._load()
        return self.get()

    # --------------------------------------------------------------- 访问
    def get(self) -> dict:
        with self.lock:
            return dict(self.data)

    @property
    def enabled(self) -> bool:
        with self.lock:
            return bool(self.data.get("enabled"))

    @property
    def vk(self) -> int:
        with self.lock:
            return int(self.data.get("vk", CHAT_SCAN_HOTKEY_VK))

    @property
    def display(self) -> str:
        with self.lock:
            return combo_name(self.data.get("vk"), self.data.get("ctrl"),
                              self.data.get("alt"), self.data.get("shift"))

    def binding(self) -> dict:
        """返回给 ChatScanHotkey 用的绑定。"""
        with self.lock:
            return {"vk": self.data.get("vk"),
                    "ctrl": self.data.get("ctrl"),
                    "alt": self.data.get("alt"),
                    "shift": self.data.get("shift")}

    # --------------------------------------------------------------- 修改
    def set_enabled(self, enabled: bool) -> dict:
        with self.lock:
            self.data["enabled"] = bool(enabled)
        self._save()
        return self.get()

    def set_binding(self, vk, ctrl=False, alt=False, shift=False) -> dict:
        """设置主键 + 修饰键。非法/被禁用的键会抛 ValueError。"""
        try:
            vk = int(vk)
        except (TypeError, ValueError):
            raise ValueError("按键无效")
        if vk in FORBIDDEN_VK:
            raise ValueError(FORBIDDEN_VK[vk])
        if not (0x08 <= vk <= 0xFE):
            raise ValueError(f"不支持的按键: {vk}")
        with self.lock:
            self.data["vk"] = vk
            self.data["ctrl"] = bool(ctrl)
            self.data["alt"] = bool(alt)
            self.data["shift"] = bool(shift)
            self.data["name"] = combo_name(vk, ctrl, alt, shift)
        self._save()
        return self.get()

    def reset(self) -> dict:
        """恢复默认（F8 / 关闭），并删除用户文件。"""
        with self.lock:
            self.data = dict(DEFAULT_HOTKEY)
            if os.path.exists(self.path):
                try:
                    os.remove(self.path)
                except OSError as e:
                    self.log.warning("删除 hotkey.json 失败: %s", e)
        return self.get()


def ensure_hotkey_file() -> None:
    """首次运行时生成 data/hotkey.json 示例（不覆盖已有文件）。"""
    if os.path.exists(HOTKEY_PATH):
        return
    os.makedirs(os.path.dirname(HOTKEY_PATH) or ".", exist_ok=True)
    payload = dict(DEFAULT_HOTKEY)
    payload["_comment"] = ("enabled=true 后按 vk 指定的键即可扫描聊天框；"
                           "一般用 GUI 的「自定义快捷键…」来改，不必手改本文件。")
    tmp = HOTKEY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HOTKEY_PATH)

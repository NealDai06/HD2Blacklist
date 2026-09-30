# -*- coding: utf-8 -*-
"""hotkey_probe.py —— 真机验证「聊天框扫描全局热键」。

模拟一次真实按键（SendInput），确认 RegisterHotKey 路径真的能收到 WM_HOTKEY。

    python tools/hotkey_probe.py            # 默认测 F8
    python tools/hotkey_probe.py Ctrl+F8    # 也可以测组合键

注意：运行期间会临时独占该按键（几秒），结束后自动注销。
"""
from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:                                        # 控制台默认 GBK，装不下 ✅ 之类
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                           # noqa: BLE001
    pass

from app.scanning import chat_hotkey as hk  # noqa: E402
from app.settings.hotkey_config import keysym_to_vk                      # noqa: E402


class _Probe:
    """假 scanner：只记录被调用了几次。"""

    def __init__(self):
        self.calls = []

    def scan_now(self, source=None):
        self.calls.append(source)
        return {"status": "ok", "hits": 0}


def _parse(text: str):
    ctrl = alt = shift = False
    vk = None
    for part in text.split("+"):
        token = part.strip()
        low = token.lower()
        if low in ("ctrl", "control"):
            ctrl = True
        elif low == "alt":
            alt = True
        elif low == "shift":
            shift = True
        else:
            vk = keysym_to_vk(token)
    if vk is None:
        raise SystemExit(f"无法识别的按键: {text}")
    return {"vk": vk, "ctrl": ctrl, "alt": alt, "shift": shift}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    binding = _parse(argv[0]) if argv else {"vk": 0x77, "ctrl": False,
                                            "alt": False, "shift": False}
    scanner = _Probe()
    hotkey = hk.ChatScanHotkey(scanner, hotkey=binding)
    hotkey.start()
    ready = hotkey.wait_ready(timeout=3.0)
    print(f"注册准备完成: ready={ready} mode={hotkey.mode} "
          f"registered={hotkey.registered}")
    print(f"状态: {hotkey.status_text()}")
    if hotkey.last_error:
        print(f"回退原因: {hotkey.last_error}")
    if hotkey.mode != hk.ChatScanHotkey.MODE_REGISTER:
        print("=> 未能注册为系统全局热键，将退回轮询模式（游戏内可能无效）")
        hotkey.stop(wait=True)
        return 2

    # 用真实按键事件验证 WM_HOTKEY 是否送达
    import win32api
    import win32con
    mods = []
    if binding["ctrl"]:
        mods.append(win32con.VK_CONTROL)
    if binding["alt"]:
        mods.append(win32con.VK_MENU)
    if binding["shift"]:
        mods.append(win32con.VK_SHIFT)

    print("模拟按键…")
    for vk in mods:
        win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(binding["vk"], 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(binding["vk"], 0, win32con.KEYEVENTF_KEYUP, 0)
    for vk in reversed(mods):
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)

    deadline = time.time() + 3.0
    while time.time() < deadline and hotkey.trigger_count == 0:
        time.sleep(0.05)

    ok = hotkey.trigger_count == 1 and len(scanner.calls) == 1
    print(f"触发次数={hotkey.trigger_count} 扫描调用={len(scanner.calls)}")
    hotkey.stop(wait=True)
    print("已注销。")
    if ok:
        print("=> 全局热键工作正常（游戏内前台也能触发）✅")
        return 0
    print("=> 注册成功但没收到 WM_HOTKEY，请把这段输出发给开发者 ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())

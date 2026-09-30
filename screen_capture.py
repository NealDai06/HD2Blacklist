# -*- coding: utf-8 -*-
"""screen_capture.py —— 屏幕区域截图（mss）。

- mss 实例 **按线程** 缓存（mss 实例不是线程安全的）
- grab(region_key) 从 RegionConfig 取坐标，只截小区域
- 结果统一转成 PIL.Image（RGB）
"""
from __future__ import annotations

import threading

import mss
from PIL import Image

from config import get_logger


class ScreenCapture:
    """线程安全的小区域截图器。"""

    def __init__(self, region_config):
        self.region_config = region_config
        self.log = get_logger("capture")
        self._local = threading.local()
        self._screen_lock = threading.Lock()
        self._virtual = None          # 虚拟桌面矩形（所有显示器并集）
        self._closed = False

    # ---------------------------------------------------------------- 内部
    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            sct = mss.mss()
            self._local.sct = sct
        return sct

    def virtual_screen(self) -> dict:
        """返回虚拟桌面 {left, top, width, height}（多显示器时为并集）。"""
        with self._screen_lock:
            if self._virtual is None:
                try:
                    m = self._sct().monitors[0]
                    self._virtual = {"left": m["left"], "top": m["top"],
                                     "width": m["width"], "height": m["height"]}
                except Exception as e:                       # noqa: BLE001
                    self.log.warning("获取虚拟桌面失败: %s", e)
                    self._virtual = {"left": 0, "top": 0,
                                     "width": 1920, "height": 1080}
            return dict(self._virtual)

    def clamp_region(self, region: dict) -> dict:
        """把区域裁剪到虚拟桌面内，避免 mss 抛异常。"""
        vs = self.virtual_screen()
        left = max(int(region["left"]), vs["left"])
        top = max(int(region["top"]), vs["top"])
        right = min(int(region["left"]) + int(region["width"]),
                    vs["left"] + vs["width"])
        bottom = min(int(region["top"]) + int(region["height"]),
                     vs["top"] + vs["height"])
        width = max(1, right - left)
        height = max(1, bottom - top)
        return {"left": left, "top": top, "width": width, "height": height}

    # ---------------------------------------------------------------- 抓取
    def grab_region(self, region: dict):
        """按显式矩形抓取，返回 PIL.Image（RGB）。失败返回 None。"""
        if self._closed:
            return None
        region = self.clamp_region(region)
        try:
            shot = self._sct().grab(region)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as e:                               # noqa: BLE001
            self.log.warning("截图失败 %s: %s", region, e)
            return None

    def grab(self, region_key: str):
        """按区域键抓取（坐标来自 RegionConfig），返回 PIL.Image 或 None。"""
        try:
            region = self.region_config.get(region_key)
        except KeyError as e:
            self.log.warning("未知区域: %s", e)
            return None
        return self.grab_region(region)

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def region_stats(img) -> tuple:
        """返回 (平均亮度, 灰度标准差)；算不出来时返回 (255.0, 0.0)。

        用于判断 ESC 菜单是否已经打开：菜单打开时这块区域是「暗色面板 + 少量亮字」
        （平均亮度低、但标准差不为 0），菜单没打开时是游戏画面（平均亮度高）。
        只算一次 numpy，几十微秒。
        """
        if img is None:
            return 255.0, 0.0
        try:
            import numpy as np
            arr = np.asarray(img.convert("L"), dtype=np.float32)
            return float(arr.mean()), float(arr.std())
        except Exception:                                    # noqa: BLE001
            return 255.0, 0.0

    @staticmethod
    def region_std(img) -> float:
        """区域灰度标准差（保留旧接口）。"""
        return ScreenCapture.region_stats(img)[1]

    def close(self) -> None:
        self._closed = True
        sct = getattr(self._local, "sct", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:                                # noqa: BLE001
                pass
            self._local.sct = None

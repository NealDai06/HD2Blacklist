# -*- coding: utf-8 -*-
"""region_config.py —— 监视区域配置（默认值 + 用户自定义）。

- 用户未自定义时使用 config.DEFAULT_REGIONS
- 用户自定义保存在 data/user_config.json 的 "regions" 字段
- 所有监视区域坐标都可由用户在 GUI 中校准
"""
from __future__ import annotations

import json
import os
import threading

from config import DEFAULT_REGIONS, REGION_META, REGION_MIN_SIZE, USER_CONFIG_PATH

_REGION_FIELDS = ("left", "top", "width", "height")


def _normalize_region(region_dict) -> dict:
    """校验并规范化区域字典，返回 {left, top, width, height} 全 int。"""
    if not isinstance(region_dict, dict):
        raise ValueError("区域必须是一个对象")
    out = {}
    for f in _REGION_FIELDS:
        if f not in region_dict:
            raise ValueError(f"缺少字段: {f}")
        try:
            out[f] = int(region_dict[f])
        except (TypeError, ValueError):
            raise ValueError(f"字段 {f} 必须是整数")
    if out["width"] < REGION_MIN_SIZE or out["height"] < REGION_MIN_SIZE:
        raise ValueError(f"宽高必须 >= {REGION_MIN_SIZE}")
    return out


class RegionConfig:
    """区域配置读写（线程安全）。"""

    def __init__(self, path: str = USER_CONFIG_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.user_regions: dict = {}
        self._load()

    # ------------------------------------------------------------------ IO
    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                regions = data.get("regions", {}) if isinstance(data, dict) else {}
                clean = {}
                for key, value in regions.items():
                    if key not in DEFAULT_REGIONS:
                        continue
                    try:
                        clean[key] = _normalize_region(value)
                    except ValueError as e:
                        print(f"[RegionConfig] 忽略非法区域 {key}: {e}")
                with self.lock:
                    self.user_regions = clean
                return
            except Exception as e:                      # noqa: BLE001
                print(f"[RegionConfig] 读取失败: {e}")
        with self.lock:
            self.user_regions = {}

    def _save(self) -> None:
        with self.lock:
            payload = {"regions": dict(self.user_regions)}
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def reload(self) -> None:
        """从磁盘重新读取（手动编辑 user_config.json 后生效）。"""
        self._load()

    # --------------------------------------------------------------- 查询
    def get(self, key: str) -> dict:
        with self.lock:
            if key in self.user_regions:
                return dict(self.user_regions[key])
            if key in DEFAULT_REGIONS:
                return dict(DEFAULT_REGIONS[key])
        raise KeyError(f"未知区域: {key}")

    def get_all(self) -> dict:
        return {k: self.get(k) for k in DEFAULT_REGIONS}

    def is_customized(self, key: str) -> bool:
        with self.lock:
            return key in self.user_regions

    def source_of(self, key: str) -> str:
        """返回 'user' 或 'default'，供 GUI 显示状态。"""
        return "user" if self.is_customized(key) else "default"

    # --------------------------------------------------------------- 修改
    def set(self, key: str, region_dict) -> dict:
        if key not in DEFAULT_REGIONS:
            raise KeyError(f"未知区域: {key}")
        region = _normalize_region(region_dict)
        with self.lock:
            self.user_regions[key] = region
        self._save()
        return region

    def set_rect(self, key: str, left: int, top: int, width: int, height: int) -> dict:
        """从框选结果（可能是反向拖拽）写入区域，自动归一化。"""
        left, top, width, height = int(left), int(top), int(width), int(height)
        if width < 0:
            left += width
            width = -width
        if height < 0:
            top += height
            height = -height
        return self.set(key, {"left": left, "top": top,
                              "width": width, "height": height})

    def reset(self, key: str) -> None:
        with self.lock:
            self.user_regions.pop(key, None)
        self._save()

    def reset_all(self) -> None:
        with self.lock:
            self.user_regions = {}
        self._save()

    # --------------------------------------------------------------- 工具
    @staticmethod
    def meta(key: str) -> dict:
        return dict(REGION_META.get(key, {"label": key, "desc": ""}))

    @staticmethod
    def label(key: str) -> str:
        return REGION_META.get(key, {}).get("label", key)

    @staticmethod
    def keys():
        return list(DEFAULT_REGIONS.keys())

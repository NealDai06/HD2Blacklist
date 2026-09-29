# -*- coding: utf-8 -*-
"""notification_config.py —— 提示（Overlay）配置：文案 / 图片 / 外观 / 音效。

- 深合并：用户 json 只需写想改的字段
- 文件不存在 / 解析失败 → 回退默认，不崩溃
- 修改后无需重启：get() 内部按 mtime 自动重载
"""
from __future__ import annotations

import json
import os
import threading

from config import DEFAULT_NOTIFICATION, NOTIFICATION_PATH


def _deep_copy(obj):
    return json.loads(json.dumps(obj))


class NotificationConfig:
    """通知配置读写（线程安全，支持热重载）。"""

    def __init__(self, path: str = NOTIFICATION_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict = {}
        self._last_mtime = None
        self._load(force=True)

    # ------------------------------------------------------------------ IO
    def _load(self, force: bool = False) -> None:
        if os.path.exists(self.path):
            try:
                mtime = os.path.getmtime(self.path)
                if not force and mtime == self._last_mtime:
                    return
                with open(self.path, "r", encoding="utf-8-sig") as f:
                    user = json.load(f)
                if not isinstance(user, dict):
                    raise ValueError("notification.json 根节点必须是对象")
                merged = self._deep_merge(_deep_copy(DEFAULT_NOTIFICATION), user)
                with self.lock:
                    self.data = merged
                    self._last_mtime = mtime
                return
            except Exception as e:                      # noqa: BLE001
                print(f"[NotificationConfig] 读取失败，回退默认: {e}")
                try:
                    self._last_mtime = os.path.getmtime(self.path)
                except OSError:
                    pass
        with self.lock:
            self.data = _deep_copy(DEFAULT_NOTIFICATION)

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        for k, v in override.items():
            if k.startswith("_"):                       # 允许 _comment 之类的注记
                base[k] = v
            elif isinstance(v, dict) and isinstance(base.get(k), dict):
                NotificationConfig._deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    def maybe_reload(self) -> None:
        self._load(force=False)

    def reload(self) -> None:
        self._load(force=True)

    # --------------------------------------------------------------- 访问
    def get(self) -> dict:
        self.maybe_reload()
        with self.lock:
            return _deep_copy(self.data)

    def get_value(self, *keys, default=None):
        """按路径取值：cfg.get_value('appearance', 'opacity', default=0.85)"""
        node = self.get()
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def save(self, new_data: dict) -> dict:
        """深合并写入磁盘，并刷新内存。"""
        with self.lock:
            self.data = self._deep_merge(_deep_copy(DEFAULT_NOTIFICATION), new_data)
            payload = _deep_copy(self.data)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        try:
            self._last_mtime = os.path.getmtime(self.path)
        except OSError:
            self._last_mtime = None
        return payload

    def reset(self) -> dict:
        """恢复默认：删除用户文件并重置内存。"""
        with self.lock:
            self.data = _deep_copy(DEFAULT_NOTIFICATION)
            if os.path.exists(self.path):
                try:
                    os.remove(self.path)
                except OSError as e:
                    print(f"[NotificationConfig] 删除配置失败: {e}")
            self._last_mtime = None
            return _deep_copy(self.data)


def ensure_notification_file() -> None:
    """首次启动时生成 notification.json 示例（不覆盖已有文件）。"""
    if os.path.exists(NOTIFICATION_PATH):
        return
    os.makedirs(os.path.dirname(NOTIFICATION_PATH) or ".", exist_ok=True)
    payload = _deep_copy(DEFAULT_NOTIFICATION)
    payload["_comment"] = "只需保留想修改的字段，其余自动使用默认值；删除本文件即恢复默认。"
    tmp = NOTIFICATION_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, NOTIFICATION_PATH)

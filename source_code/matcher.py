# -*- coding: utf-8 -*-
"""matcher.py —— 黑名单匹配器。

两条通路：
    check(names)      —— 对「名字列表」做匹配（HUD / ESC 菜单玩家列表）
    match_text(text)  —— 对「一整段聊天文本」做匹配（聊天框监控）

策略：先精确（规范化后完全相等 → 100 分），再模糊（rapidfuzz.ratio ≥ 阈值）。
"""
from __future__ import annotations

import re
import threading

try:                                    # 首选 rapidfuzz（快且与规范一致）
    from rapidfuzz import fuzz as _fuzz

    def _ratio(a: str, b: str) -> float:
        return float(_fuzz.ratio(a, b))

    BACKEND = "rapidfuzz"
except ImportError:                     # 降级：标准库 difflib，保证可运行
    from difflib import SequenceMatcher

    def _ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100.0

    BACKEND = "difflib"

from config import MATCH_THRESHOLD

# 分词分隔符：空白 + 中英文标点
_SPLIT_RE = re.compile(r"[\s,，。.、!！?？:：;；|/\\\-_\[\]【】()（）\"'“”‘’*#>]+")

# 归一化时剔除的字符：所有非字母数字（含下划线）字符，保留 Unicode 文字（中文/日文/韩文/西里尔等）。
# 下划线被当作无意义字符，这样 "Player_X" 与聊天框里的 "PlayerX" 能精确命中。
_NORM_RE = re.compile(r"[\W_]", re.UNICODE)


class Matcher:
    """线程安全的黑名单匹配器。"""

    def __init__(self, db, threshold: int = MATCH_THRESHOLD):
        self.db = db
        self.threshold = int(threshold)
        self.lock = threading.RLock()
        self.exact: dict = {}          # normalize(name) -> entry
        self.norm_list: list = []      # [(normalize(name), entry), ...]
        self.reload()

    # ------------------------------------------------------------ 归一化
    @staticmethod
    def normalize(name: str) -> str:
        if not name:
            return ""
        name = name.lower().strip()
        return _NORM_RE.sub("", name)

    # -------------------------------------------------------------- 索引
    def reload(self) -> int:
        """从数据库重建索引（黑名单增删改后调用）。返回索引条数。"""
        exact, norm_list = {}, []
        for entry in self.db.get_all():
            n = self.normalize(entry.get("player_name") or "")
            if not n:
                # 只有 player_id 的条目，用 ID 兜底参与匹配
                n = self.normalize(entry.get("player_id") or "")
                if not n or n == "-":
                    continue
            exact[n] = entry
            norm_list.append((n, entry))
        with self.lock:
            self.exact = exact
            self.norm_list = norm_list
        return len(norm_list)

    def _snapshot(self):
        with self.lock:
            return self.exact, list(self.norm_list)

    # -------------------------------------------------------------- 匹配
    def check(self, names):
        """对名字列表做匹配。

        返回 [(entry, score), ...]，同一 entry 只返回一次，按输入顺序。

        注意：OCR 经常把一个名字拆成多行（例如 "SamplePlayer_01" 被识别成
        "SamplePlayer" + "01"），所以这里除了逐个名字，还会尝试 **相邻 2/3 项的
        拼接**，与 match_text 的思路一致。
        """
        if not names:
            return []
        exact, norm_list = self._snapshot()
        hits, seen_ids = [], set()
        normed = []
        for cand in self._expand(names):
            n = self.normalize(cand)
            if len(n) >= 2:
                normed.append(n)

        # 第一轮：精确匹配。必须整体先跑完 —— 否则"较短的候选先被模糊命中"
        # 会抢走本该属于"较长的候选精确命中"的玩家。
        for n in normed:
            entry = exact.get(n)
            if entry is not None and entry["id"] not in seen_ids:
                hits.append((entry, 100.0))
                seen_ids.add(entry["id"])

        # 第二轮：模糊匹配
        for n in normed:
            if n in exact:
                continue
            for norm_name, entry in norm_list:
                if entry["id"] in seen_ids:
                    continue
                score = _ratio(n, norm_name)
                if score >= self.threshold:
                    hits.append((entry, float(score)))
                    seen_ids.add(entry["id"])
                    break
        return hits

    @staticmethod
    def _expand(names):
        """单项 + 相邻 2 项拼接 + 相邻 3 项拼接（保持顺序，短的优先）。"""
        items = [n for n in names if n]
        cands = list(items)
        for i in range(len(items) - 1):
            cands.append(items[i] + items[i + 1])
        for i in range(len(items) - 2):
            cands.append(items[i] + items[i + 1] + items[i + 2])
        return cands

    def match_text(self, text):
        """从一段长文本里匹配黑名单。

        返回 [(entry, score, matched_substring), ...]

        步骤：
          1. 整段规范化后做「包含」检查 —— 覆盖多词玩家名被标点切散的情况
          2. 分词 + 相邻 2/3 词组合 → 精确 / 模糊匹配
        """
        if not text or not text.strip():
            return []

        exact, norm_list = self._snapshot()
        hits, seen_ids = [], set()

        # ---- 1. 整段包含检查（最短优先，避免长名抢先） ----
        whole = self.normalize(text)
        if len(whole) >= 2:
            for norm_name, entry in sorted(norm_list, key=lambda x: -len(x[0])):
                if entry["id"] in seen_ids:
                    continue
                if len(norm_name) >= 2 and norm_name in whole:
                    hits.append((entry, 100.0, entry.get("player_name") or norm_name))
                    seen_ids.add(entry["id"])

        # ---- 2. 分词 + 组合 ----
        tokens = [t for t in _SPLIT_RE.split(text) if t]
        candidates = list(tokens)
        for i in range(len(tokens) - 1):
            candidates.append(tokens[i] + tokens[i + 1])
        for i in range(len(tokens) - 2):
            candidates.append(tokens[i] + tokens[i + 1] + tokens[i + 2])
        # 也尝试用空格连接（还原 "John Doe" 这类名字的原始形态）
        for i in range(len(tokens) - 1):
            candidates.append(tokens[i] + " " + tokens[i + 1])
        for i in range(len(tokens) - 2):
            candidates.append(tokens[i] + " " + tokens[i + 1] + " " + tokens[i + 2])

        normed = []                          # [(归一化串, 原文子串), ...]
        for cand in candidates:
            n = self.normalize(cand)
            if len(n) >= 2:
                normed.append((n, cand))

        # 2a. 精确（整体先跑完，保证"精确"永远优先于"模糊"）
        for n, raw in normed:
            entry = exact.get(n)
            if entry is not None and entry["id"] not in seen_ids:
                hits.append((entry, 100.0, raw))
                seen_ids.add(entry["id"])

        # 2b. 模糊
        for n, raw in normed:
            if n in exact:
                continue
            for norm_name, entry in norm_list:
                if entry["id"] in seen_ids:
                    continue
                score = _ratio(n, norm_name)
                if score >= self.threshold:
                    hits.append((entry, float(score), raw))
                    seen_ids.add(entry["id"])
                    break

        return hits

    def match_one(self, name: str):
        """单个名字匹配，返回 (entry, score) 或 None。"""
        hits = self.check([name])
        return hits[0] if hits else None

# -*- coding: utf-8 -*-
"""matcher.py —— 黑名单匹配器。

两条通路，**PeerID 永远优先**：

    check_peer(peer_id)  —— PeerID 精确命中（第一判据）
    check(names)         —— 名字匹配（兼容层：给没有 PeerID 的老条目）
    match_text(text)     —— 对一整段文本做名字匹配（离线自检 / 手工排查用）

为什么以 PeerID 为主：它是 PlayFab 的 `title_player_account`，跨局稳定，
改名字也不影响；靠名字则要么认错人（别人取了一样的名字），要么漏人
（他改了个名字你就认不出了）。

名字匹配保留四层，从严到宽，先跑完严的再跑宽的（否则短候选的模糊命中会抢走
本该属于长候选的精确命中）：

    1. 精确        —— 去掉所有非字母数字后完全相等
    2. 易混字符    —— 0/O、1/l/I、5/S… 折叠后完全相等（给 OCR 时代认错的名字兜底）
    3. 模糊        —— rapidfuzz.ratio ≥ 阈值
    4. 符号层      —— 名字**整条就是符号**（例如玩家名就是一个 `?`）时，
                      只按「整体相等」匹配，避免一个问号在每句话里误报

**有 PeerID 的条目只走精确层，不进名字索引。** 这是有意的：名字层的价值在于
"老条目没有 ID，只能靠名字"；一旦有 ID，再靠名字匹配只会把冒充同名的人
也算进来。代价是"他换了个账号"这种极端情况认不出来 —— 那时候你手动补 ID。
"""
from __future__ import annotations

import re
import threading
import unicodedata

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

from app.config import (MATCH_FUZZY_CONFUSABLE, MATCH_SYMBOL_NAMES,
                        MATCH_THRESHOLD, get_logger)
from app.core.database import normalize_peer_id

# 分词分隔符：空白 + 中英文标点
_SPLIT_RE = re.compile(r"[\s,，。.、!！?？:：;；|/\\\-_\[\]【】()（）\"'“”‘’*#>]+")

# 归一化时剔除的字符：所有非字母数字（含下划线）字符，保留 Unicode 文字（中文/日文/韩文/西里尔等）。
# 下划线被当作无意义字符，这样 "Player_X" 与聊天框里的 "PlayerX" 能精确命中。
_NORM_RE = re.compile(r"[\W_]", re.UNICODE)

#: 纯符号片段（用于把 `?`、`★` 这类名字从文本里挑出来）
_LEAD_PUNCT_RE = re.compile(r"^[^\w\s]+", re.UNICODE)
_TRAIL_PUNCT_RE = re.compile(r"[^\w\s]+$", re.UNICODE)

#: OCR 易混字符折叠表。只收「字形上真的像」的那几组，
#: 折叠过度（比如把 u/y 都当成 v）会制造大量假命中，得不偿失。
_CONFUSE_TABLE = str.maketrans({
    "0": "o",
    "1": "l",
    "i": "l",
    "5": "s",
    "8": "b",
    "2": "z",
    "4": "a",
})


def fold(text: str) -> str:
    """NFKC + 去空白 + 大小写折叠。

    NFKC 会把全角 `？` 变成半角 `?`、全角字母数字变成半角 ——
    于是「黑名单里存的是 `？`、OCR 给的是 `?`」这种情况也能对上。
    """
    if not text:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text)).casefold())


def symbol_key(name: str) -> str:
    """纯符号名字的键；返回 "" 表示这个名字不是「纯符号名」。

    纯符号名 = 折叠后非空、且**一个字母/数字/汉字都没有**（例如 `?`、`★☆`）。
    """
    if not name:
        return ""
    key = fold(name)
    if not key:
        return ""
    if any(ch.isalnum() for ch in key):
        return ""
    return key


def confuse(text: str) -> str:
    """把 OCR 易混字符折叠到同一个代表字符（用于"容错精确"比较）。"""
    return str(text or "").translate(_CONFUSE_TABLE)


def symbol_keys_in(text: str) -> set:
    """从一段文本里抽出所有「纯符号片段」候选。

    覆盖这些真实形态（黑名单里存的是 `?` 时都能命中）：
        "?"            → 整条就是符号
        "? : hello"    → 空格切出来的 atom 就是符号
        "?加入了游戏"   → atom 的前导符号串
        "为什么?"       → atom 的尾随符号串

    注意要求**整段符号串相等**：聊天里打 "???" 或 "?!" 不会命中 `?`，
    这是刻意的，否则一句带问号的聊天就会误报。
    """
    out = set()
    if not text:
        return out
    whole = symbol_key(text)
    if whole:
        out.add(whole)
    for atom in re.split(r"\s+", str(text)):
        if not atom:
            continue
        key = symbol_key(atom)
        if key:
            out.add(key)
        m = _LEAD_PUNCT_RE.match(atom)
        if m:
            k = fold(m.group(0))
            if k:
                out.add(k)
        m = _TRAIL_PUNCT_RE.search(atom)
        if m:
            k = fold(m.group(0))
            if k:
                out.add(k)
    return out


class Matcher:
    """线程安全的黑名单匹配器。"""

    def __init__(self, db, threshold: int = MATCH_THRESHOLD):
        self.db = db
        self.threshold = int(threshold)
        self.lock = threading.RLock()
        self.logger = get_logger("matcher")
        self.peers: dict = {}          # peer_id -> entry（第一判据，精确命中）
        self.exact: dict = {}          # normalize(name) -> entry（老条目）
        self.norm_list: list = []      # [(normalize(name), entry), ...]
        self.symbols: dict = {}        # symbol_key(name) -> entry（纯符号名字）
        self.folded: dict = {}         # confuse(normalize(name)) -> [entry, ...]
        self.reload()

    # ------------------------------------------------------------ 归一化
    @staticmethod
    def normalize(name: str) -> str:
        if not name:
            return ""
        name = fold(name)
        return _NORM_RE.sub("", name)

    # -------------------------------------------------------------- 索引
    def reload(self) -> int:
        """从数据库重建索引（黑名单增删改后调用）。返回索引条数。

        **有 PeerID 的条目只进 `peers`**，不进名字索引 —— 理由见模块开头的
        说明（有 ID 就别再靠名字，否则冒充同名的人也会被算进来）。

        没有 PeerID 的条目（老名单、或者你手动只填了名字）走名字索引：
        「纯符号名字」（名字就是一个 `?`）不能被「去掉所有非字母数字」的
        归一化处理 —— 那样会得到空串，条目会被直接丢掉，于是这类名字
        永远匹配不上。它们单独进 `symbols`，按符号层匹配。

        真的没法索引的条目（名字为空 —— 正常添加时 database.add() 会拦下来，
        只有手改过的库才会有）会写 WARNING，不再静默跳过：静默跳过的后果
        就是"明明在名单里却不命中"，用户完全无从判断。
        """
        peers, exact, norm_list, symbols, folded = {}, {}, [], {}, {}
        skipped = []
        for entry in self.db.get_all():
            name = (entry.get("player_name") or "").strip()

            pid = normalize_peer_id(entry.get("peer_id"))
            if pid:
                peers.setdefault(pid, entry)
                continue

            n = self.normalize(name)
            if n:
                exact.setdefault(n, entry)
                norm_list.append((n, entry))
                folded.setdefault(confuse(n), []).append(entry)

            sk = symbol_key(name) if MATCH_SYMBOL_NAMES else ""
            if sk:
                symbols.setdefault(sk, entry)

            if not n and not sk:
                skipped.append(entry)

        if skipped:
            self.logger.warning(
                "[Matcher] 有 %d 条黑名单没有可用的名字、也没有 PeerID，"
                "无法参与匹配（id=%s）",
                len(skipped),
                "、".join(str(e.get("id")) for e in skipped[:5]),
            )

        with self.lock:
            self.peers = peers
            self.exact = exact
            self.norm_list = norm_list
            self.symbols = symbols
            self.folded = folded
        return len(peers) + len(norm_list) + len(symbols)

    def stats(self) -> dict:
        """索引构成：几条靠 PeerID、几条靠名字。界面拿它做提示。"""
        with self.lock:
            return {"peers": len(self.peers),
                    "names": len(self.norm_list) + len(self.symbols)}

    def _snapshot(self):
        with self.lock:
            return (self.exact, list(self.norm_list), dict(self.symbols),
                    {k: list(v) for k, v in self.folded.items()})

    def name_allowlist(self) -> set:
        """返回「黑名单里确实存在的名字」集合（折叠后）。

        给"这个名字像不像玩家名"这类判断做白名单：像 `?` 这种名字，
        只要黑名单里真的有，就得放行。
        """
        with self.lock:
            out = set(self.exact)
            out.update(self.symbols)
            return out

    def symbol_names(self) -> set:
        """返回所有「纯符号名字」的键。"""
        with self.lock:
            return set(self.symbols)

    # -------------------------------------------------------------- 匹配
    def check_peer(self, peer_id: str):
        """按 PeerID 精确匹配。命中返回 ``(entry, 100.0)``，否则 None。

        这是**第一判据**：跨局稳定、不怕改名、也不可能被冒充。
        """
        pid = normalize_peer_id(peer_id)
        if not pid:
            return None
        with self.lock:
            entry = self.peers.get(pid)
        return (entry, 100.0) if entry is not None else None

    @staticmethod
    def _expand(names):
        """单项 + 相邻 2 项拼接 + 相邻 3 项拼接（保持顺序，短的优先）。

        OCR 经常把一个名字拆成多行（例如 "SamplePlayer_01" 被识别成
        "SamplePlayer" + "01"），所以要试相邻拼接。
        """
        items = [n for n in names if n]
        cands = list(items)
        for i in range(len(items) - 1):
            cands.append(items[i] + items[i + 1])
        for i in range(len(items) - 2):
            cands.append(items[i] + items[i + 1] + items[i + 2])
        return cands

    def check(self, names):
        """对名字列表做匹配。

        返回 [(entry, score), ...]，同一 entry 只返回一次，按输入顺序。
        """
        if not names:
            return []
        exact, norm_list, symbols, folded = self._snapshot()
        hits, seen_ids = [], set()

        def add(entry, score):
            if entry["id"] in seen_ids:
                return False
            hits.append((entry, float(score)))
            seen_ids.add(entry["id"])
            return True

        candidates = self._expand(names)
        normed = []
        for cand in candidates:
            n = self.normalize(cand)
            if len(n) >= 2:
                normed.append(n)

        # 第一轮：精确匹配。必须整体先跑完 —— 否则"较短的候选先被模糊命中"
        # 会抢走本该属于"较长的候选精确命中"的玩家。
        for n in normed:
            entry = exact.get(n)
            if entry is not None:
                add(entry, 100.0)

        # 第二轮：OCR 易混字符的"精确"（Player_Ol → Player_01 这类）
        if MATCH_FUZZY_CONFUSABLE:
            for n in normed:
                if n in exact:
                    continue
                bucket = folded.get(confuse(n)) or []
                if len(bucket) == 1:             # 只有唯一解才敢认
                    add(bucket[0], 100.0)

        # 第三轮：模糊匹配
        for n in normed:
            if n in exact:
                continue
            for norm_name, entry in norm_list:
                if entry["id"] in seen_ids:
                    continue
                score = _ratio(n, norm_name)
                if score >= self.threshold:
                    add(entry, score)
                    break

        # 第四轮：纯符号名字（整条相等）
        if symbols:
            for cand in candidates:
                sk = symbol_key(cand)
                if sk and sk in symbols:
                    add(symbols[sk], 100.0)
        return hits

    def match_text(self, text):
        """从一段长文本里匹配黑名单。

        返回 [(entry, score, matched_substring), ...]

        步骤：
          1. 整段规范化后做「包含」检查 —— 覆盖多词玩家名被标点切散的情况
          2. 分词 + 相邻 2/3 词组合 → 精确 / 易混精确 / 模糊
          3. 纯符号名字：只在「整段或某个片段就是符号」时才认
        """
        if not text or not text.strip():
            return []

        exact, norm_list, symbols, folded = self._snapshot()
        hits, seen_ids = [], set()

        def add(entry, score, raw):
            if entry["id"] in seen_ids:
                return False
            hits.append((entry, float(score), raw))
            seen_ids.add(entry["id"])
            return True

        # ---- 1. 整段包含检查（最长优先，避免长名被短名抢先） ----
        whole = self.normalize(text)
        if len(whole) >= 2:
            for norm_name, entry in sorted(norm_list, key=lambda x: -len(x[0])):
                if len(norm_name) >= 2 and norm_name in whole:
                    add(entry, 100.0, entry.get("player_name") or norm_name)

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
            if entry is not None:
                add(entry, 100.0, raw)

        # 2b. 易混字符的"精确"
        if MATCH_FUZZY_CONFUSABLE:
            for n, raw in normed:
                if n in exact:
                    continue
                bucket = folded.get(confuse(n)) or []
                if len(bucket) == 1:
                    add(bucket[0], 100.0, raw)

        # 2c. 模糊
        for n, raw in normed:
            if n in exact:
                continue
            for norm_name, entry in norm_list:
                if entry["id"] in seen_ids:
                    continue
                score = _ratio(n, norm_name)
                if score >= self.threshold:
                    add(entry, score, raw)
                    break

        # ---- 3. 纯符号名字 ----
        if symbols:
            for sk in symbol_keys_in(text):
                entry = symbols.get(sk)
                if entry is not None:
                    add(entry, 100.0, sk)

        return hits

    def match_one(self, name: str):
        """单个名字匹配，返回 (entry, score) 或 None。"""
        hits = self.check([name])
        return hits[0] if hits else None

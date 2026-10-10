# -*- coding: utf-8 -*-
"""database.py —— 黑名单 SQLite 存储（WAL 模式 + 线程安全）。

表结构：
    blacklist(id, player_name, note, peer_id, prev_names, created_at)
    seen_players(peer_id, name_first, name_last, first_seen, last_seen,
                 seen_count, last_game_pid)
    ignored(peer_id, name, added_at)

设计约定（v2 起）：
    * **身份就是 PeerID**：插件给的 `peer_id_hex` 是 PlayFab 的
      `title_player_account`，跨局稳定、改名字也不变。所以
        · 有 PeerID 的条目：`peer_id` 唯一 —— 同名不同人是两个人，各留一条；
          同一个人改了名，只更新名字，**旧名字记进 `prev_names`（曾用名）**；
        · 没有 PeerID 的条目：退回按名字识别，名字唯一（两条都没 ID 的同名记录
          分不清谁是谁），而且**不会告警** —— 想让它生效就去「最近遇到」里
          [加入名单]，或右键 [补全 PeerID]。
    * **`prev_names`（曾用名）只是给人看的历史**：存成 JSON 数组（名字里可能有
      逗号/分号，不能用分隔符硬切），不参与匹配、不参与告警。
    * **`peer_id` 由程序填写**：加入名单时从「最近遇到」自动带入，用户在界面上
      根本看不到这一栏的输入框。v1.1.2 那个真实故障（把名字填进了 ID 栏、名称栏
      留空，于是怎么都不命中）在结构上不可能再发生。
    * **名单只回答一个问题**：这个人要不要提醒。抓屏时代的 `encounters`
      命中流水账连同证据截图一起下线。
    * 老库（含 player_id/tk_count/encounter_count/last_seen）首次打开时
      **自动升级**：名称栏为空的条目用 player_id 补上名字，逐人统计字段去掉，
      补上 `peer_id` / `prev_names` 两列，并把"名字唯一"换成"PeerID 唯一"。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

from app.config import DB_PATH, RECENT_DAYS, RECENT_KEEP, get_logger

_DT_FMT = "%Y-%m-%d %H:%M:%S"

#: 「曾用名」最多留这么多个（只留最近改的几次，别让一列塞满）
PREV_NAME_LIMIT = 8
#: 曾用名在界面上/手填时的分隔方式
PREV_NAME_SEP = " / "


def parse_prev_names(value) -> list:
    """把「曾用名」解析成列表。

    正常情况是 JSON 数组（程序写的）；但允许用户手改数据库、或者导入一份
    别人用记事本编辑过的文件，所以遇到普通字符串就按 `/ , ;` 和换行切。
    """
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    return [s.strip() for s in re.split(r"[/,;\n]", text) if s.strip()]


def format_prev_names(value, sep: str = PREV_NAME_SEP) -> str:
    """把「曾用名」列的值格式化成给人看的一行。"""
    return sep.join(parse_prev_names(value))


def dump_prev_names(items) -> str:
    """把一串名字存成列值（空列表存空串，别存成 "[]"）。"""
    if isinstance(items, str):
        items = parse_prev_names(items)
    clean = []
    for item in items or []:
        name = str(item).strip()
        if name and name not in clean:
            clean.append(name)
    return json.dumps(clean[-PREV_NAME_LIMIT:], ensure_ascii=False) if clean else ""


def push_prev_name(value, old_name) -> str:
    """把一个旧名字追加进「曾用名」（去重、只留最近 PREV_NAME_LIMIT 个）。"""
    items = parse_prev_names(value)
    old = str(old_name or "").strip()
    if not old or old in items:
        return dump_prev_names(items)
    items.append(old)
    return dump_prev_names(items[-PREV_NAME_LIMIT:])


def normalize_peer_id(value) -> str:
    """把 PeerID 统一成 16 位大写十六进制；不像 PeerID 就返回空串。

    插件写出来的是 `format('%08X%08X', hi, lo)` —— 16 个十六进制字符。
    从别处复制粘贴来的可能带空格、小写、`0x` 前缀，这里一并洗干净，
    免得出现"看着一模一样却匹配不上"这种事。
    """
    text = str(value or "").strip().replace(" ", "").replace("-", "")
    if text[:2].lower() == "0x":
        text = text[2:]
    if not text or len(text) > 16:
        return ""
    try:
        int(text, 16)
    except ValueError:
        return ""
    return text.upper().zfill(16)


#: 名单的字段（+ 自增 id）。CSV / JSON 导入导出按这个顺序
LIST_FIELDS = ("player_name", "note", "peer_id", "prev_names", "created_at")

#: 系统管理的列：能读、能显示，但用户不能直接改（必须走 set_peer_id）
SYSTEM_FIELDS = ("peer_id",)

#: 老库里出现过、现在一律不要的字段
_LEGACY_COLUMNS = ("player_id", "tk_count", "encounter_count", "last_seen")

# 允许的排序字段白名单（防 SQL 注入）
_SORT_COLUMNS = {
    "id": "id",
    "player_name": "player_name",
    "note": "note",
    "peer_id": "peer_id",
    "created_at": "created_at",
}

_BLACKLIST_DDL = """
CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_name TEXT NOT NULL,
    note TEXT DEFAULT '',
    peer_id TEXT DEFAULT '',
    prev_names TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

#: 「最近遇到」——由插件日志喂进来，记住碰到的**每一个人**（不只名单里的）。
#: 你自己由 watcher 过滤掉，永远不进这张表。
_SEEN_PLAYERS_DDL = """
CREATE TABLE IF NOT EXISTS seen_players (
    peer_id       TEXT PRIMARY KEY,
    name_first    TEXT DEFAULT '',
    name_last     TEXT DEFAULT '',
    first_seen    DATETIME,
    last_seen     DATETIME,
    seen_count    INTEGER DEFAULT 1,
    last_game_pid INTEGER
);
"""

#: 「已忽略」——点过[忽略此人]就不再有他的提醒，也不再出现在「最近遇到」里。
#: 单独一张表而不是 seen_players 的一个布尔列：忽略的可能是一个**还没见过**
#: 的人（从别处得知的 id），而且取消忽略之后不该凭空长出一条"遇到过"的记录。
_IGNORED_DDL = """
CREATE TABLE IF NOT EXISTS ignored (
    peer_id  TEXT PRIMARY KEY,
    name     TEXT DEFAULT '',
    added_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

_INDEXES_DDL = """
CREATE INDEX IF NOT EXISTS idx_seen_players_last_seen ON seen_players(last_seen);
CREATE INDEX IF NOT EXISTS idx_seen_players_game ON seen_players(last_game_pid);
-- **身份 = PeerID**（v2 起）：
--   · 有 PeerID 的条目：peer_id 唯一 —— 同名不同人是两个人，各留一条；
--     同一个人改了名字也还是他，靠这条索引认出来（只需更新名字）。
--   · 没有 PeerID 的条目：退回按名字识别，名字仍然唯一 ——
--     否则两条都没 ID 的「Wendy」根本分不清谁是谁。
-- 用**部分索引**（WHERE）而不是普通唯一索引：SQLite 里空串也算一个值，
-- 直接建唯一索引会让第二条「没有 ID 的条目」插不进去。
CREATE UNIQUE INDEX IF NOT EXISTS idx_blacklist_peer
    ON blacklist(peer_id) WHERE peer_id <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_blacklist_name_legacy
    ON blacklist(player_name) WHERE IFNULL(peer_id,'') = '';
"""

_SCHEMA = (_BLACKLIST_DDL + _SEEN_PLAYERS_DDL + _IGNORED_DDL + _INDEXES_DDL)

_SELECT_LIST = ("SELECT id, player_name, note, peer_id, prev_names, created_at "
                "FROM blacklist")


def _now() -> str:
    return datetime.now().strftime(_DT_FMT)


def _clean_dt(value):
    """清洗导入来的时间字段：空串 / NULL 一律当作 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class BlacklistDB:
    """线程安全的黑名单数据库封装。"""

    def __init__(self, db_path: str = DB_PATH, timeout: float = 10.0):
        self.db_path = db_path
        self.log = get_logger("database")
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None,
            timeout=timeout,
        )
        self.lock = threading.RLock()
        self._closed = False
        self._init_connection()
        self._create_tables()

    # ------------------------------------------------------------------ 初始化
    def _init_connection(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")

    def _create_tables(self) -> None:
        with self.lock:
            # 先升级老库（内部会建新表），再统一补齐 seen_players / 索引
            self._migrate_legacy_schema()
            self._migrate_add_columns()
            self._migrate_blacklist_keys()
            self._drop_legacy_encounters()
            self.conn.executescript(_SCHEMA)

    def _migrate_blacklist_keys(self) -> None:
        """把「名字唯一」换成「PeerID 唯一（没有 ID 的才退回名字）」。

        v1 的规矩是 `blacklist(player_name)` 唯一 —— 后果是"同名不同人加不进来"，
        而且身份是名字（改个名就认不出来了）。现在 **PeerID 才是身份**：
          · 有 PeerID 的条目：peer_id 唯一，同名不同人各留一条；
          · 没有 PeerID 的条目：名字仍然唯一（两条没 ID 的同名条目分不清谁是谁）。

        老库上那条 `idx_blacklist_name` 必须删掉，否则新规矩生效不了。
        """
        with self.lock:
            try:
                old = self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='idx_blacklist_name'").fetchone()
                if old is None:
                    return
                self.conn.execute("DROP INDEX IF EXISTS idx_blacklist_name")
                self.log.info(
                    "[数据库] 已把「名字唯一」换成「PeerID 唯一」"
                    "（同名不同人可以各留一条，同一个人改名只更新名字）")
            except sqlite3.Error as e:                   # noqa: BLE001
                self.log.warning("[数据库] 更换名单索引失败: %s", e)

    def _migrate_add_columns(self) -> None:
        """给 v1.x 的库补上 v2 新增的列（`peer_id` / `prev_names`）。

        老条目一律留空：凭空给老条目编一个 PeerID 是不可能的（硬猜只会把别人的
        ID 挂到你的名单上），曾用名更是没处可查。
        用 `ALTER TABLE ADD COLUMN` 而不是重建表 —— 名单里全是用户手输的数据，
        能不搬就不搬。
        """
        wanted = (("peer_id", "TEXT DEFAULT ''"),
                  ("prev_names", "TEXT DEFAULT ''"))
        try:
            cols = {row[1] for row in
                    self.conn.execute("PRAGMA table_info(blacklist)")}
        except sqlite3.Error:
            return
        if not cols:
            return
        with self.lock:
            for name, ddl in wanted:
                if name in cols:
                    continue
                try:
                    self.conn.execute(
                        f"ALTER TABLE blacklist ADD COLUMN {name} {ddl}")
                except sqlite3.Error as e:               # noqa: BLE001
                    self.log.warning("[数据库] 补 %s 列失败: %s", name, e)
                    continue
                self.log.info("[数据库] 已给 blacklist 补上 %s 列（老条目留空）",
                              name)

    def _drop_legacy_encounters(self) -> None:
        """删掉抓屏时代的 `encounters` 表（命中流水账 + 证据截图索引）。

        v2 不截图，也没有"命中"这个动作了：告警由日志事件直接触发，
        这张表既没有生产者也没有读者。老库里的它连同历史行一起清掉。
        """
        with self.lock:
            try:
                exists = self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='encounters'").fetchone()
                if exists is None:
                    return
                n = self.conn.execute(
                    "SELECT COUNT(*) FROM encounters").fetchone()[0]
                self.conn.execute("DROP TABLE encounters")
                self.log.warning(
                    "[数据库] 已删除抓屏时代的 encounters 表（原有 %d 条命中记录）",
                    int(n or 0))
            except sqlite3.Error as e:                   # noqa: BLE001
                self.log.warning("[数据库] 删除 encounters 表失败: %s", e)

    def _migrate_legacy_schema(self) -> int:
        """老库升级：删掉 player_id 与三个统计字段，保留名字与备注。

        为什么用"建新表再搬"而不是 `ALTER TABLE DROP COLUMN`：老库的
        `UNIQUE(player_id, player_name)` 约束去不掉，而 SQLite 在
        DROP COLUMN 时不允许动唯一索引覆盖的列。直接重建最省事。
        """
        try:
            cols = {row[1] for row in
                    self.conn.execute("PRAGMA table_info(blacklist)")}
        except sqlite3.Error:
            return 0
        if not cols or not (cols & set(_LEGACY_COLUMNS)):
            return 0                     # 新库 / 空库，不需要升级

        with self.lock:
            rows = self.conn.execute(
                "SELECT id, IFNULL(player_id,''), IFNULL(player_name,''), "
                "IFNULL(note,''), created_at FROM blacklist ORDER BY id ASC"
            ).fetchall()
            kept, dropped, seen = 0, 0, set()
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute("ALTER TABLE blacklist RENAME TO blacklist_legacy")
                self.conn.execute(_BLACKLIST_DDL)
                for _id, pid, pname, note, created in rows:
                    # 名称栏为空的老条目用 player_id 补名字 —— 那多半就是
                    # 用户填错栏的名字（`？` 这种），不能丢。
                    name = (pname or "").strip() or (pid or "").strip()
                    if not name or name == "-" or name in seen:
                        dropped += 1
                        continue
                    seen.add(name)
                    self.conn.execute(
                        "INSERT INTO blacklist (player_name, note, created_at) "
                        "VALUES (?, ?, ?)",
                        (name, note, created or _now()),
                    )
                    kept += 1
                self.conn.execute("DROP TABLE blacklist_legacy")
                self.conn.execute("COMMIT")
            except Exception:                            # noqa: BLE001
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            self.log.warning(
                "[数据库] 已升级到「名字 + PeerID」结构（去掉玩家ID栏与逐人统计）："
                "保留 %d 条，丢弃 %d 条（无名/重复）",
                kept, dropped,
            )
            return kept

    # ------------------------------------------------------------------ CRUD
    def add(self, player_name: str, note: str = "",
            created_at: str = None, peer_id: str = "",
            prev_names=None) -> int:
        """新增一条黑名单，返回自增 id（命中已有的人时返回的是**那一条**的 id）。

        **身份是 PeerID**：
          · 带了 PeerID，名单里已经有同一个 PeerID → 那是同一个人（他改过名字
            或者你把名字写错了）→ **只更新名字**（旧名字记进曾用名），绝不新增
            第二条；备注按"没填就不动"处理，免得空备注把你写过的覆盖掉；
          · 带了 PeerID，但同名的那条是**另一个 ID**（或者根本没有 ID）→ 各留一条：
            同名不同人本来就该都能记下来；
          · 没带 PeerID → 只能按名字识别：已经有一条"也没 ID"的同名条目就抛
            ValueError（两条都没 ID 的同名记录分不清谁是谁），
            但如果同名那条**有** PeerID，说明是另一个人 → 照样新增。

        `prev_names`（曾用名）可以给列表，也可以给 `"老王 / 老王2"` 这种字符串；
        走改名那条路时，被替下来的旧名字会**自动追加**进去。
        ⚠ 传空值 = "没指定"（不是"清空"）：新增/加入名单时把别人的改名历史抹掉
        没有任何道理；真要清空得走 `update(prev_names="")`。
        """
        name = (player_name or "").strip()
        if not name:
            raise ValueError("玩家名称不能为空")
        pid = normalize_peer_id(peer_id)
        prev = dump_prev_names(prev_names) if prev_names else None
        with self.lock:
            if pid:
                row = self.conn.execute(
                    "SELECT id FROM blacklist WHERE peer_id = ?",
                    (pid,)).fetchone()
                if row is not None:
                    self._rename_locked(row[0], name, note, prev)
                    return int(row[0])
                try:
                    cur = self.conn.execute(
                        "INSERT INTO blacklist "
                        "(player_name, note, peer_id, prev_names, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (name, note or "", pid, prev or "", created_at or _now()))
                    return int(cur.lastrowid)
                except sqlite3.IntegrityError:
                    raise ValueError(f"名单里已经有「{name}」了")
            dup = self.conn.execute(
                "SELECT player_name FROM blacklist "
                "WHERE player_name = ? COLLATE NOCASE "
                "AND IFNULL(peer_id,'') = ''", (name,)).fetchone()
            if dup is not None:
                raise ValueError(
                    f"名单里已经有一条没填 PeerID 的「{dup[0]}」了"
                    "（没有 PeerID 的条目只能按名字区分）")
            try:
                cur = self.conn.execute(
                    "INSERT INTO blacklist "
                    "(player_name, note, peer_id, prev_names, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, note or "", "", prev or "", created_at or _now()))
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                raise ValueError(f"名单里已经有「{name}」了")

    def _rename_locked(self, entry_id: int, new_name: str, note: str = "",
                       prev_names=None) -> None:
        """同一个人改了名字：只更新名字 + 把旧名字记进曾用名（调用方已持锁）。

        备注按"没填就不动"处理：从「最近遇到」点[加入名单]时备注框是空的，
        那时不该把用户之前写过的备注清掉。`prev_names` 传了就以它为准
        （用户在编辑框里改过的曾用名不能被自动追加覆盖掉），没传就自动追加。
        """
        with self.lock:
            row = self.conn.execute(
                "SELECT player_name, IFNULL(prev_names,'') FROM blacklist "
                "WHERE id = ?", (int(entry_id),)).fetchone()
            if row is None:
                return
            old_name, old_prev = row
            sets, params = ["player_name = ?"], [new_name]
            if (note or "").strip():
                sets.append("note = ?")
                params.append(note.strip())
            if prev_names is not None:
                sets.append("prev_names = ?")
                params.append(prev_names)
            elif (old_name or "").strip() != (new_name or "").strip():
                # 名字真的变了（不只是大小写/空格差异）→ 旧名字进曾用名
                sets.append("prev_names = ?")
                params.append(push_prev_name(old_prev, old_name))
            params.append(int(entry_id))
            try:
                self.conn.execute(
                    f"UPDATE blacklist SET {', '.join(sets)} WHERE id = ?",
                    params)
            except sqlite3.IntegrityError:
                raise ValueError(
                    f"名单里已经有一条没填 PeerID 的「{new_name}」了，改不了")

    def set_peer_id(self, entry_id: int, peer_id: str) -> bool:
        """给已有条目补上 / 清掉 PeerID（系统操作，不走用户的编辑框）。

        传空串 = 清掉（清掉之后这条就退回按名字识别，也不会再告警）。
        目标 PeerID 已经挂在**别的条目**上时抛 ValueError：那说明这两条其实是
        同一个人，该由人来决定留哪条、删哪条，程序不替你合并。
        """
        pid = normalize_peer_id(peer_id)
        with self.lock:
            if pid:
                dup = self.conn.execute(
                    "SELECT id, player_name FROM blacklist WHERE peer_id = ?",
                    (pid,)).fetchone()
                if dup is not None and int(dup[0]) != int(entry_id):
                    raise ValueError(
                        f"这个 PeerID 已经挂在「{dup[1]}」(# {dup[0]}) 上了")
            cur = self.conn.execute(
                "UPDATE blacklist SET peer_id = ? WHERE id = ?",
                (pid, int(entry_id)))
            return cur.rowcount > 0

    def find_by_peer_id(self, peer_id: str):
        """按 PeerID 查条目（匹配器要的是 O(1)，这里是单条查询的入口）。"""
        pid = normalize_peer_id(peer_id)
        if not pid:
            return None
        with self.lock:
            cur = self.conn.execute(f"{_SELECT_LIST} WHERE peer_id = ?", (pid,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        return dict(zip(cols, row)) if row else None

    def rename_by_peer_id(self, peer_id, new_name) -> str:
        """按 PeerID 把名单里的名字改成日志里看到的最新名字。

        返回**旧名字**；没这条 / 名字为空 / 名字没变都返回空串（调用方据此判断
        "到底改了没有"）。这是全自动的：他改名的当下我们就能从名册里读到新名字，
        与其等用户自己发现名单里写着旧名字，不如直接跟着改。

        只按 peer_id 找行，所以不可能撞上"无 ID 条目按名字唯一"那条约束
        （带 PeerID 的行不在那个部分索引里）。旧名字会记进**曾用名**。
        """
        pid = normalize_peer_id(peer_id)
        new = (new_name or "").strip()
        if not pid or not new:
            return ""
        with self.lock:
            row = self.conn.execute(
                "SELECT id, player_name, IFNULL(prev_names,'') FROM blacklist "
                "WHERE peer_id = ?", (pid,)).fetchone()
            if row is None:
                return ""
            entry_id, old, prev = row
            old = (old or "").strip()
            if old == new:
                return ""
            self.conn.execute(
                "UPDATE blacklist SET player_name = ?, prev_names = ? "
                "WHERE id = ?",
                (new, push_prev_name(prev, old), int(entry_id)))
        return old

    def blacklist_peers(self) -> dict:
        """{peer_id: 条目字典} —— 给匹配器做 PeerID 精确命中。"""
        with self.lock:
            cur = self.conn.execute(
                f"{_SELECT_LIST} WHERE IFNULL(peer_id,'') <> ''")
            cols = [d[0] for d in cur.description]
            return {row[cols.index("peer_id")]: dict(zip(cols, row))
                    for row in cur.fetchall()}

    def update(self, entry_id: int, player_name=None, note=None,
               prev_names=None) -> bool:
        """按字段更新（None 表示不改）。返回是否有行被修改。

        改名字时旧名字会自动进**曾用名**（除非调用方自己传了 prev_names ——
        用户在编辑框里改过的曾用名以他为准）。
        """
        sets, params = [], []
        if player_name is not None:
            name = str(player_name).strip()
            if not name:
                raise ValueError("玩家名称不能为空")
            sets.append("player_name = ?")
            params.append(name)
        if note is not None:
            sets.append("note = ?")
            params.append(str(note))
        if prev_names is not None:
            sets.append("prev_names = ?")
            params.append(dump_prev_names(prev_names))
        if not sets:
            return False
        with self.lock:
            try:
                if player_name is not None and prev_names is None:
                    old = self.conn.execute(
                        "SELECT player_name, IFNULL(prev_names,'') "
                        "FROM blacklist WHERE id = ?",
                        (int(entry_id),)).fetchone()
                    if old is not None and (old[0] or "").strip() != (
                            str(player_name).strip()):
                        sets.append("prev_names = ?")
                        params.append(push_prev_name(old[1], old[0]))
                params.append(int(entry_id))
                cur = self.conn.execute(
                    f"UPDATE blacklist SET {', '.join(sets)} WHERE id = ?", params
                )
                return cur.rowcount > 0
            except sqlite3.IntegrityError:
                raise ValueError("更新失败：名单里已经有同名的条目")

    def delete(self, entry_id: int) -> bool:
        """删除黑名单条目。

        v1 里这里还会级联删掉该条目的命中记录；v2 没有命中流水账了，
        「最近遇到」是**独立的事实**（这人确实跟你同队过），不能因为你把他
        从名单里划掉就把他从"遇到过"里抹掉。
        """
        with self.lock:
            cur = self.conn.execute("DELETE FROM blacklist WHERE id = ?",
                                    (int(entry_id),))
            return cur.rowcount > 0

    def delete_many(self, entry_ids) -> int:
        n = 0
        for eid in entry_ids:
            if self.delete(eid):
                n += 1
        return n

    def get(self, entry_id: int):
        with self.lock:
            cur = self.conn.execute(f"{_SELECT_LIST} WHERE id = ?",
                                    (int(entry_id),))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        return dict(zip(cols, row)) if row else None

    def _order_clause(self, order_by: str, desc: bool) -> str:
        col = _SORT_COLUMNS.get(order_by, "created_at")
        direction = "DESC" if desc else "ASC"
        return f"ORDER BY {col} {direction}, id DESC"

    def get_all(self, order_by: str = "created_at", desc: bool = True) -> list:
        """返回全部黑名单（默认按添加时间倒序，新加的排前面）。"""
        order = self._order_clause(order_by, desc)
        with self.lock:
            cur = self.conn.execute(f"{_SELECT_LIST} {order}")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def search(self, keyword: str, order_by: str = "created_at",
               desc: bool = True) -> list:
        """按 名称 / 备注 模糊查询（大小写不敏感）。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return self.get_all(order_by=order_by, desc=desc)
        order = self._order_clause(order_by, desc)
        like = f"%{keyword}%"
        with self.lock:
            cur = self.conn.execute(
                f"""{_SELECT_LIST}
                    WHERE player_name LIKE ? COLLATE NOCASE
                       OR IFNULL(note,'') LIKE ? COLLATE NOCASE
                    {order}""",
                (like, like),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------ 导入导出
    #: 导入导出使用的字段（与 GUI 的 CSV 列顺序保持一致）
    IMPORT_EXPORT_FIELDS = LIST_FIELDS

    def export_all(self) -> list:
        """导出全部黑名单条目，用于备份 / 迁移 / 多机同步。

        `peer_id` 与 `prev_names` 一起导出 —— 换台机器导回来，PeerID 精确匹配
        照样有效，曾用名也不会丢。
        """
        with self.lock:
            cur = self.conn.execute(
                "SELECT player_name, note, peer_id, prev_names, created_at "
                "FROM blacklist ORDER BY id ASC"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def import_entries(self, entries, strategy: str = "skip") -> dict:
        """从外部数据导入黑名单。

        **身份键：有 PeerID 就用 PeerID，没有才退回名字**（大小写不敏感）。
        这条规矩同时决定了两种"看起来像冲突、其实不是"的情况：

          · **同名、不同 PeerID** → 是两个人 → **各留一条**（都记下来）；
          · **同 PeerID、不同名字** → 是同一个人改了名 → **把库里的名字更新掉**
            （不论选哪种策略都会更新名字，因为"谁"是确定的）。

        只有 **同一个身份键** 撞上时，才轮到策略决定备注/添加时间怎么办：

            'skip'        —— 什么都不改（默认）
            'update_note' —— 用文件里的备注覆盖
            'overwrite'   —— 备注与添加时间都用文件里的值

        名字为空的行直接跳过。整个导入在 **一个事务** 内完成，任何异常整体回滚。

        返回 {"inserted": n, "updated": n, "skipped": n, "renamed": n}
        （`renamed` 是"被 PeerID 认出来、只改了名字"的行数，它是前面的子集）
        """
        if strategy not in ("skip", "update_note", "overwrite"):
            raise ValueError(f"未知的导入策略: {strategy}")

        inserted = skipped = updated = renamed = 0
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                for e in entries or []:
                    if not isinstance(e, dict):
                        skipped += 1
                        continue

                    name = str(e.get("player_name") or "").strip()
                    if not name:
                        skipped += 1          # 没有名字的行无法识别，跳过
                        continue

                    note = str(e.get("note") or "")
                    created = _clean_dt(e.get("created_at"))
                    pid = normalize_peer_id(e.get("peer_id"))
                    file_prev = parse_prev_names(e.get("prev_names"))

                    if pid:
                        existing = self.conn.execute(
                            "SELECT id, player_name, IFNULL(prev_names,'') "
                            "FROM blacklist WHERE peer_id = ?", (pid,)).fetchone()
                    else:
                        existing = self.conn.execute(
                            "SELECT id, player_name, IFNULL(prev_names,'') "
                            "FROM blacklist "
                            "WHERE player_name = ? COLLATE NOCASE "
                            "AND IFNULL(peer_id,'') = ''", (name,)).fetchone()

                    if existing is None:
                        # 新身份：直接加一条（同名但那一条是**别人**，照样加）
                        self.conn.execute(
                            "INSERT INTO blacklist "
                            "(player_name, note, peer_id, prev_names, "
                            " created_at) VALUES (?, ?, ?, ?, ?)",
                            (name, note, pid, dump_prev_names(file_prev),
                             created or _now()))
                        inserted += 1
                        continue

                    # 同一个人：名字跟着文件走（他改名了 / 你以前写错了）
                    row_id, old_name, db_prev = existing
                    renamed_here = old_name != name
                    merged_prev = parse_prev_names(db_prev) + file_prev
                    if renamed_here:
                        merged_prev.append(old_name)
                        renamed += 1
                    if renamed_here or file_prev:
                        self.conn.execute(
                            "UPDATE blacklist SET player_name = ?, prev_names = ? "
                            "WHERE id = ?",
                            (name, dump_prev_names(merged_prev), row_id))

                    if strategy == "skip":
                        skipped += 1
                    elif strategy == "update_note":
                        self.conn.execute(
                            "UPDATE blacklist SET note = ? WHERE id = ?",
                            (note, row_id))
                        updated += 1
                    else:                     # overwrite
                        self.conn.execute(
                            "UPDATE blacklist SET note = ?, created_at = ? "
                            "WHERE id = ?",
                            (note, created or _now(), row_id))
                        updated += 1
                self.conn.execute("COMMIT")
            except Exception:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

        return {"inserted": inserted, "updated": updated, "skipped": skipped,
                "renamed": renamed}

    # ------------------------------------------------------- 最近遇到
    def record_seen(self, peer_id, name="", game_pid=None, seen_at=None) -> dict:
        """记一次「遇到了这个人」，按 `peer_id` upsert。

        `seen_count` 是**遇到过几局**，不是写了几行日志：同一局里
        join / update / 离队又回来 都只算一次，只有 `game_pid` 变了才 +1。
        `name_first` 永远保留第一次看到的名字，`name_last` 跟着最新的一行走。
        """
        pid = normalize_peer_id(peer_id)
        if not pid:
            return {}
        when = seen_at or _now()
        clean = (name or "").strip()
        with self.lock:
            for attempt in range(3):
                try:
                    self.conn.execute("BEGIN IMMEDIATE")
                    row = self.conn.execute(
                        "SELECT name_first, seen_count, last_game_pid "
                        "FROM seen_players WHERE peer_id = ?", (pid,)).fetchone()
                    if row is None:
                        self.conn.execute(
                            """INSERT INTO seen_players
                               (peer_id, name_first, name_last, first_seen,
                                last_seen, seen_count, last_game_pid)
                               VALUES (?, ?, ?, ?, ?, 1, ?)""",
                            (pid, clean, clean, when, when, game_pid))
                        seen_count = 1
                    else:
                        name_first, count, last_pid = row
                        seen_count = int(count or 0)
                        if (game_pid is not None and last_pid is not None
                                and int(game_pid) != int(last_pid)):
                            seen_count += 1
                        self.conn.execute(
                            """UPDATE seen_players
                                  SET name_first = ?, name_last = ?,
                                      last_seen = ?, seen_count = ?,
                                      last_game_pid = ?
                                WHERE peer_id = ?""",
                            (name_first or clean, clean or name_first, when,
                             seen_count, game_pid, pid))
                    self.conn.execute("COMMIT")
                    return {"peer_id": pid, "name": clean,
                            "seen_count": seen_count, "last_seen": when}
                except sqlite3.OperationalError:
                    # 数据库忙：回滚后短暂重试
                    try:
                        self.conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    if attempt == 2:
                        raise
                    threading.Event().wait(0.05 * (attempt + 1))
                except Exception:
                    try:
                        self.conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
        raise RuntimeError("record_seen 失败")

    def get_seen(self, peer_id: str):
        """按 PeerID 取一条「最近遇到」。"""
        pid = normalize_peer_id(peer_id)
        if not pid:
            return None
        with self.lock:
            cur = self.conn.execute(
                "SELECT peer_id, name_first, name_last, first_seen, last_seen, "
                "seen_count, last_game_pid FROM seen_players WHERE peer_id = ?",
                (pid,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        return dict(zip(cols, row)) if row else None

    def get_recent_seen(self, limit: int = RECENT_KEEP,
                        days: int = RECENT_DAYS) -> list:
        """最近遇到的人，按最后遇到时间倒序。`days <= 0` = 不限时间。"""
        sql = ("SELECT peer_id, name_first, name_last, first_seen, last_seen, "
               "seen_count, last_game_pid FROM seen_players")
        params: list = []
        if days and int(days) > 0:
            sql += " WHERE last_seen >= ?"
            params.append(
                (datetime.now() - timedelta(days=int(days))).strftime(_DT_FMT))
        sql += " ORDER BY last_seen DESC, peer_id ASC LIMIT ?"
        params.append(int(limit))
        with self.lock:
            cur = self.conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def count_seen(self, days: int = 0) -> int:
        """「最近遇到」的人数。`days > 0` 时只数最近这些天。"""
        with self.lock:
            if days and int(days) > 0:
                row = self.conn.execute(
                    "SELECT COUNT(*) FROM seen_players WHERE last_seen >= ?",
                    ((datetime.now() - timedelta(days=int(days)))
                     .strftime(_DT_FMT),)).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT COUNT(*) FROM seen_players").fetchone()
        return int(row[0]) if row else 0

    def forget_seen(self, peer_id: str) -> bool:
        """把一个人从「最近遇到」里划掉（不进名单，但也不想再看见他）。"""
        pid = normalize_peer_id(peer_id)
        if not pid:
            return False
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM seen_players WHERE peer_id = ?", (pid,))
            return cur.rowcount > 0

    def clear_seen(self) -> None:
        """清空「最近遇到」（不动名单）。"""
        with self.lock:
            self.conn.execute("DELETE FROM seen_players")

    # ---------------------------------------------------------------- 忽略
    def is_ignored(self, peer_id: str) -> bool:
        """这个人是不是被忽略着。**告警链路每次都会问它**，所以走主键索引。"""
        pid = normalize_peer_id(peer_id)
        if not pid:
            return False
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM ignored WHERE peer_id = ?", (pid,)).fetchone()
        return row is not None

    def ignore_peer(self, peer_id, name="") -> bool:
        """忽略一个人：以后不再提醒，也不再进「最近遇到」。

        顺手把他已有的「最近遇到」记录删掉 —— 否则界面上会出现
        「忽略了他、他还赖在列表里」这种自相矛盾。
        """
        pid = normalize_peer_id(peer_id)
        if not pid:
            return False
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO ignored (peer_id, name, added_at) "
                "VALUES (?, ?, ?)", (pid, (name or "").strip(), _now()))
            self.conn.execute("DELETE FROM seen_players WHERE peer_id = ?",
                              (pid,))
        return True

    def unignore_peer(self, peer_id) -> bool:
        """取消忽略。**不会**凭空补一条「最近遇到」——下次真遇到自然会有。"""
        pid = normalize_peer_id(peer_id)
        if not pid:
            return False
        with self.lock:
            cur = self.conn.execute("DELETE FROM ignored WHERE peer_id = ?",
                                    (pid,))
        return cur.rowcount > 0

    def ignored_peers(self) -> set:
        """忽略名单里所有 peer_id（给"要不要提醒"的判断用）。"""
        with self.lock:
            return {r[0] for r in self.conn.execute("SELECT peer_id FROM ignored")}

    def get_ignored(self) -> list:
        """忽略名单（给人看的，带名字与添加时间）。"""
        with self.lock:
            cur = self.conn.execute(
                "SELECT peer_id, name, added_at FROM ignored "
                "ORDER BY added_at DESC, peer_id ASC")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def clear_ignored(self) -> None:
        """清空忽略名单（以后这些人会重新出现、也会重新提醒）。"""
        with self.lock:
            self.conn.execute("DELETE FROM ignored")

    # ------------------------------------------------------------- 统计
    def get_count(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) FROM blacklist").fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------- 维护
    def clear_blacklist(self) -> None:
        """只清名单，保住「最近遇到」。"""
        with self.lock:
            self.conn.execute("DELETE FROM blacklist")

    def clear_all(self) -> None:
        """清空名单与「最近遇到」（配置、日志与忽略名单都不动）。"""
        with self.lock:
            self.conn.execute("DELETE FROM blacklist")
            self.conn.execute("DELETE FROM seen_players")

    def vacuum(self) -> None:
        with self.lock:
            self.conn.execute("VACUUM")

    def close(self) -> None:
        with self.lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

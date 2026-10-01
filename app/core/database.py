# -*- coding: utf-8 -*-
"""database.py —— 黑名单 SQLite 存储（WAL 模式 + 原子命中记录）。

表结构：
    blacklist(id, player_name, note, created_at)
    encounters(id, blacklist_id, name_seen, match_score, source,
               screenshot_path, seen_at)

设计约定（v1.1.2 起）：
    * **名单只按名字识别**：玩家ID 一栏已经删掉。真实案例：添加时把名字填进
      了 ID 栏、名称栏留空，结果匹配怎么都不命中 —— 少一栏，就少一类这种错。
    * **不再有逐人统计**：tk_count / encounter_count / last_seen 全部删除。
      命中的事实记在 `encounters` 表里（一次命中一行，含匹配度 / 来源 /
      证据截图 / 时间），界面上的「本局命中 / 今日命中」由它现算，
      不依赖人均计数器。名单本身只回答一个问题：**这个名字要不要提醒**。
    * 老库（含 player_id/tk_count/encounter_count/last_seen）首次打开时
      **自动升级**：名称栏为空的条目用 player_id 补上名字，其余统计字段连同
      历史命中记录一起清空（用户明确要求"历史统计一并清空"）。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime

from app.config import DB_PATH, get_logger

_DT_FMT = "%Y-%m-%d %H:%M:%S"

#: 名单的字段就是这三个（+ 自增 id）
LIST_FIELDS = ("player_name", "note", "created_at")

#: 老库里出现过、现在一律不要的字段
_LEGACY_COLUMNS = ("player_id", "tk_count", "encounter_count", "last_seen")

# 允许的排序字段白名单（防 SQL 注入）
_SORT_COLUMNS = {
    "id": "id",
    "player_name": "player_name",
    "note": "note",
    "created_at": "created_at",
}

_BLACKLIST_DDL = """
CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_name TEXT NOT NULL,
    note TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

_ENCOUNTERS_DDL = """
CREATE TABLE IF NOT EXISTS encounters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blacklist_id INTEGER,
    name_seen TEXT,
    match_score REAL,
    source TEXT,
    screenshot_path TEXT,
    seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

_INDEXES_DDL = """
CREATE INDEX IF NOT EXISTS idx_encounters_seen_at ON encounters(seen_at);
CREATE INDEX IF NOT EXISTS idx_encounters_blacklist_id ON encounters(blacklist_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_blacklist_name ON blacklist(player_name);
"""

_SCHEMA = _BLACKLIST_DDL + _ENCOUNTERS_DDL + _INDEXES_DDL

_SELECT_LIST = "SELECT id, player_name, note, created_at FROM blacklist"


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
            # 先升级老库（内部会建新表），再统一补齐 encounters / 索引
            self._migrate_legacy_schema()
            self.conn.executescript(_SCHEMA)

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
                    # 用户填错栏的名字（`？`、`PlayerX` 这种），不能丢。
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
                # 「历史统计一并清空」：命中的历史记录也不再保留
                self.conn.execute("DELETE FROM encounters")
                self.conn.execute("COMMIT")
            except Exception:                            # noqa: BLE001
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            self.log.warning(
                "[数据库] 已升级到新结构（名单只按名字，去掉玩家ID与统计字段）："
                "保留 %d 条，丢弃 %d 条（无名/重复），历史命中记录已清空",
                kept, dropped,
            )
            return kept

    # ------------------------------------------------------------------ CRUD
    def add(self, player_name: str, note: str = "",
            created_at: str = None) -> int:
        """新增一条黑名单，返回自增 id。名字重复抛 ValueError。"""
        name = (player_name or "").strip()
        if not name:
            raise ValueError("玩家名称不能为空")
        with self.lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO blacklist (player_name, note, created_at) "
                    "VALUES (?, ?, ?)",
                    (name, note or "", created_at or _now()),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                raise ValueError(f"名单里已经有「{name}」了")

    def update(self, entry_id: int, player_name=None, note=None) -> bool:
        """按字段更新（None 表示不改）。返回是否有行被修改。"""
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
        if not sets:
            return False
        params.append(int(entry_id))
        with self.lock:
            try:
                cur = self.conn.execute(
                    f"UPDATE blacklist SET {', '.join(sets)} WHERE id = ?", params
                )
                return cur.rowcount > 0
            except sqlite3.IntegrityError:
                raise ValueError("更新失败：名单里已经有同名的条目")

    def delete(self, entry_id: int) -> bool:
        """删除黑名单条目及其命中记录。"""
        with self.lock:
            cur = self.conn.execute("DELETE FROM blacklist WHERE id = ?",
                                    (int(entry_id),))
            self.conn.execute("DELETE FROM encounters WHERE blacklist_id = ?",
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
        """导出全部黑名单条目，用于备份 / 迁移 / 多机同步。"""
        with self.lock:
            cur = self.conn.execute(
                "SELECT player_name, note, created_at FROM blacklist "
                "ORDER BY id ASC"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def import_entries(self, entries, strategy: str = "skip") -> dict:
        """从外部数据导入黑名单。

        strategy:
            'skip'        —— 已存在同名则跳过（默认）
            'update_note' —— 只更新备注
            'overwrite'   —— 连添加时间一起用文件里的值覆盖

        唯一键为 **名称**；名字为空的行直接跳过。
        整个导入在 **一个事务** 内完成，任何异常都会整体回滚。

        返回 {"inserted": n, "skipped": n, "updated": n}
        """
        if strategy not in ("skip", "update_note", "overwrite"):
            raise ValueError(f"未知的导入策略: {strategy}")

        inserted = skipped = updated = 0
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

                    existing = self.conn.execute(
                        "SELECT id FROM blacklist "
                        "WHERE player_name = ? COLLATE NOCASE", (name,),
                    ).fetchone()

                    if existing:
                        row_id = existing[0]
                        if strategy == "skip":
                            skipped += 1
                        elif strategy == "update_note":
                            self.conn.execute(
                                "UPDATE blacklist SET note = ? WHERE id = ?",
                                (note, row_id),
                            )
                            updated += 1
                        else:                 # overwrite
                            self.conn.execute(
                                "UPDATE blacklist SET note = ?, created_at = ? "
                                "WHERE id = ?",
                                (note, created or _now(), row_id),
                            )
                            updated += 1
                    else:
                        self.conn.execute(
                            "INSERT INTO blacklist "
                            "(player_name, note, created_at) VALUES (?, ?, ?)",
                            (name, note, created or _now()),
                        )
                        inserted += 1
                self.conn.execute("COMMIT")
            except Exception:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

        return {"inserted": inserted, "skipped": skipped, "updated": updated}

    # ------------------------------------------------------- 命中记录
    def record_encounter(self, entry_id, score, source,
                         name_seen, screenshot_path):
        """记一次命中：往 `encounters` 插一行，返回这条记录的关键信息。

        名单表本身**不再累加任何计数**（人均统计字段已删），所以这里只做
        "写事实"：谁、什么时候、匹配度多少、从哪触发的、证据图在哪。
        界面上的「今日命中」是现算的。

        返回 ``{"encounter_id":…, "seen_at":…, "today_count":…}``；
        `today_count` 是该玩家今天第几次被命中。
        """
        with self.lock:
            for attempt in range(3):
                try:
                    self.conn.execute("BEGIN IMMEDIATE")
                    now = _now()
                    exists = self.conn.execute(
                        "SELECT 1 FROM blacklist WHERE id = ?",
                        (int(entry_id),),
                    ).fetchone()
                    if exists is None:
                        raise ValueError(f"黑名单条目不存在: {entry_id}")
                    cur = self.conn.execute(
                        """INSERT INTO encounters
                           (blacklist_id, name_seen, match_score, source,
                            screenshot_path, seen_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (int(entry_id), name_seen,
                         float(score) if score is not None else 0.0,
                         source, screenshot_path, now),
                    )
                    encounter_id = int(cur.lastrowid)
                    row = self.conn.execute(
                        "SELECT COUNT(*) FROM encounters "
                        "WHERE blacklist_id = ? AND DATE(seen_at) = ?",
                        (int(entry_id), now[:10]),
                    ).fetchone()
                    self.conn.execute("COMMIT")
                    return {"encounter_id": encounter_id, "seen_at": now,
                            "today_count": int(row[0]) if row else 1}
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
        raise RuntimeError("record_encounter 失败")

    # ------------------------------------------------------------- 统计
    def get_today_encounter_count(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM encounters WHERE DATE(seen_at) = ?",
                (today,),
            ).fetchone()
        return row[0] if row else 0

    def get_total_encounter_count(self) -> int:
        """命中的总次数（= encounters 表的行数）。"""
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) FROM encounters").fetchone()
        return row[0] if row else 0

    def get_count(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) FROM blacklist").fetchone()
        return row[0] if row else 0

    def get_encounters(self, blacklist_id: int, limit: int = 100) -> list:
        with self.lock:
            cur = self.conn.execute(
                """SELECT id, blacklist_id, name_seen, match_score, source,
                          screenshot_path, seen_at
                   FROM encounters WHERE blacklist_id = ?
                   ORDER BY seen_at DESC, id DESC LIMIT ?""",
                (int(blacklist_id), int(limit)),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_recent_encounters(self, limit: int = 50) -> list:
        with self.lock:
            cur = self.conn.execute(
                """SELECT e.id, e.blacklist_id, e.name_seen, e.match_score,
                          e.source, e.screenshot_path, e.seen_at,
                          b.player_name
                   FROM encounters e
                   LEFT JOIN blacklist b ON b.id = e.blacklist_id
                   ORDER BY e.seen_at DESC, e.id DESC LIMIT ?""",
                (int(limit),),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------- 维护
    def clear_all(self) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM encounters")
            self.conn.execute("DELETE FROM blacklist")

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

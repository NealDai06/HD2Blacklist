# -*- coding: utf-8 -*-
"""database.py —— 黑名单 SQLite 存储（WAL 模式 + 原子命中更新）。

表结构见 README / 开发规范：
    blacklist(id, player_id, player_name, note, tk_count, encounter_count,
              evidence_path, created_at, last_seen)
    encounters(id, blacklist_id, name_seen, match_score, source,
               screenshot_path, seen_at)

字段语义：
    tk_count        —— 用户手动录入的 TK 次数
    encounter_count —— 系统自动累积的命中次数
    last_seen       —— 最近一次命中的时间（自动更新）
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime

from config import DB_PATH

_DT_FMT = "%Y-%m-%d %H:%M:%S"

# 允许的排序字段白名单（防 SQL 注入）
_SORT_COLUMNS = {
    "id": "id",
    "player_id": "player_id",
    "player_name": "player_name",
    "note": "note",
    "tk_count": "tk_count",
    "encounter_count": "encounter_count",
    "created_at": "created_at",
    "last_seen": "last_seen",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id TEXT NOT NULL,
    player_name TEXT,
    note TEXT,
    tk_count INTEGER DEFAULT 0,
    encounter_count INTEGER DEFAULT 0,
    evidence_path TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen DATETIME,
    UNIQUE(player_id, player_name)
);

CREATE TABLE IF NOT EXISTS encounters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blacklist_id INTEGER,
    name_seen TEXT,
    match_score REAL,
    source TEXT,
    screenshot_path TEXT,
    seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_encounters_seen_at ON encounters(seen_at);
CREATE INDEX IF NOT EXISTS idx_encounters_blacklist_id ON encounters(blacklist_id);
CREATE INDEX IF NOT EXISTS idx_blacklist_last_seen ON blacklist(last_seen);
CREATE INDEX IF NOT EXISTS idx_blacklist_name ON blacklist(player_name);
"""


def _now() -> str:
    return datetime.now().strftime(_DT_FMT)


def _safe_int(value, default: int = 0) -> int:
    """把导入来的值安全地转成非负整数。"""
    if value is None or value == "":
        return default
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


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
            self.conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ CRUD
    def add(self, player_id: str, player_name: str = "", note: str = "",
            tk_count: int = 0, evidence_path: str = "") -> int:
        """新增一条黑名单，返回自增 id。重复 (player_id, player_name) 抛 ValueError。"""
        player_id = (player_id or "").strip()
        player_name = (player_name or "").strip()
        if not player_id and not player_name:
            raise ValueError("玩家ID 与 玩家名称 至少填写一项")
        # player_id 为 NOT NULL，允许只用名称录入
        if not player_id:
            player_id = "-"
        try:
            tk = max(0, int(tk_count))
        except (TypeError, ValueError):
            tk = 0
        with self.lock:
            try:
                cur = self.conn.execute(
                    """INSERT INTO blacklist
                       (player_id, player_name, note, tk_count, evidence_path,
                        encounter_count, created_at)
                       VALUES (?, ?, ?, ?, ?, 0, ?)""",
                    (player_id, player_name, note or "", tk,
                     evidence_path or "", _now()),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                raise ValueError(
                    f"已存在相同记录（玩家ID={player_id}，名称={player_name}）"
                )

    def update(self, entry_id: int, player_id=None, player_name=None,
               note=None, tk_count=None, evidence_path=None) -> bool:
        """按字段更新（None 表示不改）。返回是否有行被修改。"""
        sets, params = [], []
        if player_id is not None:
            sets.append("player_id = ?")
            params.append(str(player_id).strip() or "-")
        if player_name is not None:
            sets.append("player_name = ?")
            params.append(str(player_name).strip())
        if note is not None:
            sets.append("note = ?")
            params.append(str(note))
        if tk_count is not None:
            try:
                tk = max(0, int(tk_count))
            except (TypeError, ValueError):
                tk = 0
            sets.append("tk_count = ?")
            params.append(tk)
        if evidence_path is not None:
            sets.append("evidence_path = ?")
            params.append(str(evidence_path))
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
                raise ValueError("更新失败：已存在相同（玩家ID, 名称）的记录")

    def delete(self, entry_id: int) -> bool:
        """删除黑名单及其命中记录。"""
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
            cur = self.conn.execute(
                """SELECT id, player_id, player_name, note, tk_count,
                          encounter_count, evidence_path, created_at, last_seen
                   FROM blacklist WHERE id = ?""",
                (int(entry_id),),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        return dict(zip(cols, row)) if row else None

    def get_all(self, order_by: str = "last_seen", desc: bool = True) -> list:
        """返回全部黑名单。

        排序：默认 last_seen DESC，且 NULL（从未遇见）排在最后。
        """
        col = _SORT_COLUMNS.get(order_by, "last_seen")
        direction = "DESC" if desc else "ASC"
        if col == "last_seen":
            order = (f"ORDER BY (last_seen IS NULL) ASC, last_seen {direction}, "
                     f"id DESC")
        elif col in ("created_at",):
            order = f"ORDER BY {col} {direction}, id DESC"
        else:
            order = f"ORDER BY {col} {direction}, id DESC"
        with self.lock:
            cur = self.conn.execute(
                f"""SELECT id, player_id, player_name, note, tk_count,
                           encounter_count, evidence_path, created_at, last_seen
                    FROM blacklist {order}"""
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def search(self, keyword: str, order_by: str = "last_seen",
               desc: bool = True) -> list:
        """按 玩家ID / 名称 / 备注 模糊查询（大小写不敏感）。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return self.get_all(order_by=order_by, desc=desc)
        col = _SORT_COLUMNS.get(order_by, "last_seen")
        direction = "DESC" if desc else "ASC"
        if col == "last_seen":
            order = (f"ORDER BY (last_seen IS NULL) ASC, last_seen {direction}, "
                     f"id DESC")
        else:
            order = f"ORDER BY {col} {direction}, id DESC"
        like = f"%{keyword}%"
        with self.lock:
            cur = self.conn.execute(
                f"""SELECT id, player_id, player_name, note, tk_count,
                           encounter_count, evidence_path, created_at, last_seen
                    FROM blacklist
                    WHERE player_id LIKE ? COLLATE NOCASE
                       OR IFNULL(player_name,'') LIKE ? COLLATE NOCASE
                       OR IFNULL(note,'') LIKE ? COLLATE NOCASE
                    {order}""",
                (like, like, like),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------ 导入导出
    #: 导入导出使用的字段（与 GUI 的 CSV 列顺序保持一致）
    IMPORT_EXPORT_FIELDS = ("player_id", "player_name", "note", "tk_count",
                            "encounter_count", "created_at", "last_seen")

    def export_all(self) -> list:
        """导出全部黑名单条目（含全部字段），用于备份 / 迁移 / 多机同步。"""
        with self.lock:
            cur = self.conn.execute(
                """SELECT player_id, player_name, note, tk_count,
                          encounter_count, created_at, last_seen
                   FROM blacklist
                   ORDER BY id ASC"""
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def import_entries(self, entries, strategy: str = "skip") -> dict:
        """从外部数据导入黑名单。

        strategy:
            'skip'        —— 已存在则跳过（默认）
            'update_note' —— 保留 encounter_count / last_seen，只更新 note / tk_count
            'overwrite'   —— 连 encounter_count / last_seen 一起用文件里的值覆盖

        唯一键为 (player_id, player_name)；player_id 为空的行直接跳过。
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

                    pid = str(e.get("player_id") or "").strip()
                    pname = str(e.get("player_name") or "").strip()
                    if not pid:
                        skipped += 1          # 空 player_id 一律跳过
                        continue

                    note = str(e.get("note") or "")
                    tk = _safe_int(e.get("tk_count"))
                    enc = _safe_int(e.get("encounter_count"))
                    created = _clean_dt(e.get("created_at"))
                    last_seen = _clean_dt(e.get("last_seen"))

                    existing = self.conn.execute(
                        """SELECT id FROM blacklist
                           WHERE player_id = ? AND IFNULL(player_name, '') = ?""",
                        (pid, pname),
                    ).fetchone()

                    if existing:
                        row_id = existing[0]
                        if strategy == "skip":
                            skipped += 1
                        elif strategy == "update_note":
                            self.conn.execute(
                                """UPDATE blacklist
                                   SET note = ?, tk_count = ?
                                   WHERE id = ?""",
                                (note, tk, row_id),
                            )
                            updated += 1
                        else:                 # overwrite
                            self.conn.execute(
                                """UPDATE blacklist
                                   SET note = ?, tk_count = ?,
                                       encounter_count = ?, last_seen = ?
                                   WHERE id = ?""",
                                (note, tk, enc, last_seen, row_id),
                            )
                            updated += 1
                    else:
                        self.conn.execute(
                            """INSERT INTO blacklist
                               (player_id, player_name, note, tk_count,
                                encounter_count, created_at, last_seen)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (pid, pname, note, tk, enc,
                             created or _now(), last_seen),
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

    # ------------------------------------------------------- 命中自动更新
    def record_encounter(self, entry_id, score, source,
                         name_seen, screenshot_path):
        """原子更新：次数+1、更新时间、插入 encounters、读回最新值。

        使用 BEGIN IMMEDIATE + WAL，保证并发场景下次数不丢。
        """
        with self.lock:
            for attempt in range(3):
                try:
                    self.conn.execute("BEGIN IMMEDIATE")
                    now = _now()
                    cur = self.conn.execute(
                        """UPDATE blacklist
                           SET encounter_count = encounter_count + 1,
                               last_seen = ?
                           WHERE id = ?""",
                        (now, int(entry_id)),
                    )
                    if cur.rowcount == 0:
                        raise ValueError(f"黑名单条目不存在: {entry_id}")
                    self.conn.execute(
                        """INSERT INTO encounters
                           (blacklist_id, name_seen, match_score, source,
                            screenshot_path, seen_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (int(entry_id), name_seen,
                         float(score) if score is not None else 0.0,
                         source, screenshot_path, now),
                    )
                    row = self.conn.execute(
                        """SELECT encounter_count, last_seen
                           FROM blacklist WHERE id = ?""",
                        (int(entry_id),),
                    ).fetchone()
                    self.conn.execute("COMMIT")
                    if row is None:
                        raise ValueError(f"黑名单条目不存在: {entry_id}")
                    return {"encounter_count": row[0], "last_seen": row[1]}
                except sqlite3.OperationalError as e:
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

    def reset_encounter_count(self, entry_id: int) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE blacklist SET encounter_count = 0 WHERE id = ?",
                (int(entry_id),),
            )

    def reset_last_seen(self, entry_id: int) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE blacklist SET last_seen = NULL WHERE id = ?",
                (int(entry_id),),
            )

    def reset_all_counters(self) -> int:
        """把所有条目的 encounter_count / last_seen 清零，返回影响行数。"""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE blacklist SET encounter_count = 0, last_seen = NULL"
            )
            return cur.rowcount

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
                          b.player_name, b.player_id
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

# -*- coding: utf-8 -*-
"""legacy_schema.py —— 旧数据库结构（v1.1.1 及更早）的备份 / 恢复 / 降级工具。

背景
----
v1.1.2 把名单表简化成 `(id, player_name, note, created_at)`，删掉了
`player_id` / `tk_count` / `encounter_count` / `evidence_path` / `last_seen`。
程序**首次打开旧库时会自动升级**（见 app/core/database.py 的
`_migrate_legacy_schema`），升级会清空 encounters 历史。

所以旧结构需要用这个工具留一份底：

    # 1. 把所有「旧结构」的 blacklist.db 扫出来备份（只复制，不动原文件）
    python tools/legacy_schema.py --backup

    # 2. 看看备份里有什么
    python tools/legacy_schema.py --list

    # 3. 恢复某一份回去（目标会被先改名成 xxx.before_restore_<时间戳>）
    python tools/legacy_schema.py --restore 测试版_PlayerX \
        --to "..\\发布包\\HD2Blacklist\\data\\blacklist.db"

    # 4. 把已经被升级过的新结构库变回旧结构（player_id/统计字段填默认值）
    python tools/legacy_schema.py --downgrade data\\blacklist.db

`--backup` 还会生成 `empty_old_schema.db`（空库，旧结构）与 `schema_old.sql`
（旧结构的建表语句），即使一个旧库都不剩，结构本身也还在。

工具只用标准库（sqlite3 / shutil），不 import 项目里的任何模块 ——
以后代码再改，这个工具照样能跑。
"""
from __future__ import annotations

import argparse
import glob
import io
import os
import shutil
import sqlite3
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOURCE = os.path.dirname(_HERE)                 # ...\source_code
_WORKSPACE = os.path.dirname(_SOURCE)            # ...\Helldiver_black
DEFAULT_BACKUP_DIR = os.path.join(_WORKSPACE, "_packaged_data_backup",
                                  "legacy_schema_backup")

#: v1.1.1 及更早的表结构（与原 app/core/database.py 里的 _SCHEMA 逐字一致）
OLD_SCHEMA_SQL = """CREATE TABLE IF NOT EXISTS blacklist (
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

#: 旧结构有、新结构没有的字段
OLD_ONLY_COLUMNS = ("player_id", "tk_count", "encounter_count",
                    "evidence_path", "last_seen")

_NEW_COLUMNS = ("id", "player_name", "note", "created_at")


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str = "blacklist"):
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _is_old_schema(path: str) -> bool:
    try:
        conn = _connect(path)
    except sqlite3.Error:
        return False
    try:
        cols = set(_columns(conn))
        return bool(cols & set(OLD_ONLY_COLUMNS))
    finally:
        conn.close()


def _is_new_schema(path: str) -> bool:
    try:
        conn = _connect(path)
    except sqlite3.Error:
        return False
    try:
        return set(_columns(conn)) == set(_NEW_COLUMNS)
    finally:
        conn.close()


def _count(path: str) -> int:
    try:
        conn = _connect(path)
    except sqlite3.Error:
        return -1
    try:
        return conn.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0]
    except sqlite3.Error:
        return -1
    finally:
        conn.close()


# ---------------------------------------------------------------- 备份
def find_databases(root: str) -> list:
    """找出工作区里所有 blacklist.db（跳过备份目录自己）。"""
    out = []
    for path in glob.glob(os.path.join(root, "**", "blacklist.db"),
                          recursive=True):
        if os.path.abspath(DEFAULT_BACKUP_DIR) in os.path.abspath(path):
            continue
        out.append(path)
    return sorted(out)


def _label_for(path: str, root: str) -> str:
    """给备份起个短而能认出来的名字：来源文件夹（最多两段）+ 条数。"""
    rel = os.path.relpath(path, root)
    parts = [p for p in rel.split(os.sep) if p not in (".", "")]
    if parts and parts[-1].lower() == "blacklist.db":
        parts.pop()
    if parts and parts[-1] in ("data", "Data"):
        parts.pop()
    if parts and parts[0] == "_packaged_data_backup":
        parts.pop(0)                       # 这层没有区分度
    # 产品文件夹名（HD2Blacklist / HD2Blacklist.exe 旁边那层）没有区分度，跳过
    while len(parts) > 1 and parts[-1] in ("HD2Blacklist",):
        parts.pop()
    keep = parts[-2:] or ["root"]
    name = "__".join(keep)
    for bad in ('\\', '/', ':', '*', '?', '"', '<', '>', '|'):
        name = name.replace(bad, "_")
    return f"{name}__{_count(path)}条"


def make_empty_old_schema(out_path: str) -> None:
    if os.path.exists(out_path):
        os.remove(out_path)
    conn = sqlite3.connect(out_path)
    try:
        conn.executescript(OLD_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def do_backup(root: str, backup_dir: str) -> int:
    db_dir = os.path.join(backup_dir, "db")
    os.makedirs(db_dir, exist_ok=True)

    with io.open(os.path.join(backup_dir, "schema_old.sql"), "w",
                 encoding="utf-8", newline="\n") as f:
        f.write("-- HD2 黑名单 v1.1.1 及更早的数据库结构（旧结构）\n")
        f.write(f"-- 备份于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(OLD_SCHEMA_SQL)

    empty = os.path.join(backup_dir, "empty_old_schema.db")
    make_empty_old_schema(empty)
    print(f"[空库] {empty}")

    n_old = n_new = 0
    for path in find_databases(root):
        label = _label_for(path, root)
        if _is_old_schema(path):
            target = os.path.join(db_dir, f"{label}.db")
            shutil.copy2(path, target)
            print(f"[备份] {path}\n       → {target}")
            n_old += 1
        elif _is_new_schema(path):
            print(f"[跳过] {path}（已经是新结构，结构本身见 empty_old_schema.db）")
            n_new += 1
        else:
            print(f"[跳过] {path}（认不出结构）")
    print(f"\n共备份 {n_old} 个旧结构库；{n_new} 个已是新结构。")
    return n_old


# ---------------------------------------------------------------- 恢复
def do_restore(name: str, target: str, backup_dir: str) -> int:
    db_dir = os.path.join(backup_dir, "db")
    if not os.path.isdir(db_dir):
        print(f"备份目录不存在：{db_dir}（先跑 --backup）", file=sys.stderr)
        return 2
    cands = [p for p in glob.glob(os.path.join(db_dir, "*.db"))
             if name.lower() in os.path.basename(p).lower()]
    if not cands:
        print(f"没找到匹配「{name}」的备份。用 --list 看看有哪些。",
              file=sys.stderr)
        return 2
    if len(cands) > 1:
        print(f"「{name}」匹配到多个备份，请写全一点：", file=sys.stderr)
        for p in cands:
            print("   ", os.path.basename(p), file=sys.stderr)
        return 2

    src = cands[0]
    if os.path.isdir(target):
        target = os.path.join(target, "blacklist.db")
    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
    if os.path.exists(target):
        keep = f"{target}.before_restore_{_ts()}"
        shutil.copy2(target, keep)
        print(f"[保底] 原来的库已改名保留：{keep}")
    shutil.copy2(src, target)
    for suffix in ("-wal", "-shm"):
        stale = target + suffix
        if os.path.exists(stale):
            os.remove(stale)
            print(f"[清理] 删掉旧的 {os.path.basename(stale)}（避免半新半旧）")
    print(f"[恢复] {os.path.basename(src)} → {target}（{_count(target)} 条）")
    return 0


def do_list(backup_dir: str) -> int:
    db_dir = os.path.join(backup_dir, "db")
    empty = os.path.join(backup_dir, "empty_old_schema.db")
    print(f"备份目录：{backup_dir}")
    if os.path.exists(empty):
        print(f"  空库模板：empty_old_schema.db（旧结构，0 条）")
    if not os.path.isdir(db_dir):
        print("  （还没有备份，先跑 --backup）")
        return 1
    files = sorted(glob.glob(os.path.join(db_dir, "*.db")))
    if not files:
        print("  （备份目录里没有 .db）")
        return 1
    for p in files:
        conn = _connect(p)
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM blacklist").fetchone()[0]
            try:
                enc = conn.execute(
                    "SELECT COUNT(*) FROM encounters").fetchone()[0]
            except sqlite3.Error:
                enc = "-"
            # 旧结构里名字也可能填在 player_id 栏（真实用户就是这么填的），
            # 列表里两个都显示，方便认人
            names = "、".join(
                str((r[0] or "").strip() or (r[1] or "").strip() or "(无名)")
                for r in conn.execute(
                    "SELECT player_name, player_id FROM blacklist "
                    "ORDER BY id LIMIT 6"))
        finally:
            conn.close()
        print(f"  · {os.path.basename(p)}")
        print(f"      名单 {rows} 条 / 命中记录 {enc} 条 / 名字：{names}")
    return 0


# ---------------------------------------------------------------- 降级
def do_downgrade(path: str, out: str = None) -> int:
    """把新结构库转回旧结构（缺的字段填默认值），encounters 原样搬过去。"""
    if not os.path.exists(path):
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2
    if not _is_new_schema(path):
        print(f"{path} 不是新结构（没有可转换的东西）", file=sys.stderr)
        return 2
    out = out or os.path.join(
        os.path.dirname(os.path.abspath(path)),
        f"blacklist_old_schema_{_ts()}.db")
    if os.path.exists(out):
        os.remove(out)
    make_empty_old_schema(out)

    src = _connect(path)
    dst = _connect(out)
    try:
        rows = list(src.execute(
            "SELECT id, player_name, note, created_at FROM blacklist"))
        for _id, name, note, created in rows:
            # 旧结构里 player_id 是 NOT NULL，且「只用名字录入」时旧代码就是存 '-'
            dst.execute(
                "INSERT INTO blacklist (id, player_id, player_name, note, "
                "tk_count, encounter_count, evidence_path, created_at, "
                "last_seen) VALUES (?, '-', ?, ?, 0, 0, '', ?, NULL)",
                (_id, name or "", note or "", created))
        try:
            enc = list(src.execute(
                "SELECT id, blacklist_id, name_seen, match_score, source, "
                "screenshot_path, seen_at FROM encounters"))
            dst.executemany(
                "INSERT INTO encounters (id, blacklist_id, name_seen, "
                "match_score, source, screenshot_path, seen_at) "
                "VALUES (?,?,?,?,?,?,?)", enc)
        except sqlite3.Error:
            pass
        dst.commit()
        n = dst.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0]
    finally:
        src.close()
        dst.close()
    print(f"[降级] {path}\n       → {out}（{n} 条，player_id/统计字段填了默认值）")
    print("提示：原库里已经丢掉的历史统计（遇到过几次、最后遇见时间）"
          "无法恢复，这里只能填 0 / NULL。")
    return 0


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="旧数据库结构（v1.1.1 及更早）的备份 / 恢复 / 降级")
    ap.add_argument("--backup", action="store_true",
                    help="扫描工作区里所有旧结构 blacklist.db 并备份")
    ap.add_argument("--list", action="store_true", help="列出已有备份")
    ap.add_argument("--restore", metavar="关键词",
                    help="恢复某份备份（配合 --to）")
    ap.add_argument("--to", metavar="目标",
                    help="恢复目标：blacklist.db 路径，或 data 目录")
    ap.add_argument("--downgrade", metavar="新结构.db",
                    help="把新结构库转回旧结构")
    ap.add_argument("--out", metavar="输出.db", help="降级的输出路径")
    ap.add_argument("--root", default=_WORKSPACE,
                    help=f"扫描根目录（默认 {_WORKSPACE}）")
    ap.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR,
                    help=f"备份目录（默认 {DEFAULT_BACKUP_DIR}）")
    args = ap.parse_args(argv)

    if args.backup:
        do_backup(args.root, args.backup_dir)
        return 0
    if args.list:
        return do_list(args.backup_dir)
    if args.restore:
        if not args.to:
            print("--restore 需要配 --to（目标 db 路径或 data 目录）",
                  file=sys.stderr)
            return 2
        return do_restore(args.restore, args.to, args.backup_dir)
    if args.downgrade:
        return do_downgrade(args.downgrade, args.out)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

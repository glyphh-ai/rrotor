"""What changed in an Ada store, captured on the head and applied on a follower (docs/specs/cluster.md, 3).

The kernel writes SQLite from inside the sandbox and the daemon never sees its calls, so on the head the daemon
installs in each store a change table and triggers on the two tables that sync, `precedents` (keyed by `id`) and
`meta` (by `key`). Each insert, update or delete there adds the row's key to `_rrotor_changes` while
`_rrotor_capture` says so (one row, 1 on the head). `collect` reads the keys past what was shipped and each one's
row as it is now (or that it is gone), in one read transaction; `clear` deletes what was shipped. A row is JSON: its
columns by name, a BLOB as base64.

On a follower the replica is written by `apply` alone, one entry in one transaction, with capture off: a run reading
it (with the kernel's read-only open) sees it before an entry or after, never half. **Replicas are kept in
`journal_mode=DELETE`** (`settle`, on every apply and snapshot): the kernel opens a read-only store `mode=ro` and
reads a WAL file with no `-wal` and `-shm` beside it as immutable, which an applier writing meanwhile would make
inconsistent; a rollback-journal file is never read immutable, and SQLite's own locks keep the read whole (the
applier waits on `busy_timeout` for a read in progress, and a read waits for a commit in progress)."""

from __future__ import annotations

import base64
import contextlib
import re
import sqlite3
from pathlib import Path
from typing import Any

TABLES = {"precedents": "id", "meta": "key"}
BUSY_MS = 10_000
COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def connect(store: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(store), timeout=BUSY_MS / 1000, isolation_level=None)
    db.execute(f"PRAGMA busy_timeout={BUSY_MS}")
    return db


def has(db: sqlite3.Connection, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def install(store: Path) -> bool:
    """The change table and its triggers in a store the kernel made (capture on). Answers whether they are new: the
    store's rows were never captured, so the whole store is to be shipped. A store without its tables yet: False."""
    db = connect(store)
    try:
        if not all(has(db, t) for t in TABLES):
            return False
        new = not has(db, "_rrotor_changes")
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS _rrotor_changes "
            "(n INTEGER PRIMARY KEY AUTOINCREMENT, tbl TEXT, op TEXT, key TEXT)"
        )
        db.execute("CREATE TABLE IF NOT EXISTS _rrotor_capture (on_ INTEGER)")
        if db.execute("SELECT COUNT(*) FROM _rrotor_capture").fetchone()[0] == 0:
            db.execute("INSERT INTO _rrotor_capture VALUES (1)")
        db.execute("UPDATE _rrotor_capture SET on_ = 1")
        for table, key in TABLES.items():
            for op, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS _rrotor_{table}_{op.lower()} AFTER {op} ON {table} "
                    "WHEN (SELECT on_ FROM _rrotor_capture) = 1 BEGIN "
                    f"INSERT INTO _rrotor_changes (tbl, op, key) VALUES ('{table}', '{op.lower()}', {row}.{key}); END"
                )
            db.execute(  # a key changed by an update: the old one is gone
                f"CREATE TRIGGER IF NOT EXISTS _rrotor_{table}_rekey AFTER UPDATE ON {table} "
                f"WHEN (SELECT on_ FROM _rrotor_capture) = 1 AND OLD.{key} IS NOT NEW.{key} BEGIN "
                f"INSERT INTO _rrotor_changes (tbl, op, key) VALUES ('{table}', 'delete', OLD.{key}); END"
            )
        db.execute("COMMIT")
        return new
    finally:
        db.close()


def encoded(row: sqlite3.Row) -> dict[str, Any]:
    return {k: base64.b64encode(v).decode() if isinstance(v, bytes) else v for k, v in dict(row).items()}


def collect(store: Path) -> tuple[list[dict[str, Any]], int]:
    """The rows changed since the last `clear`, each key once in the order last changed: (rows, the last change's
    number). Nothing when the store has no capture."""
    db = connect(store)
    db.row_factory = sqlite3.Row
    try:
        if not has(db, "_rrotor_changes"):
            return [], 0
        db.execute("BEGIN")
        changes = db.execute("SELECT n, tbl, key FROM _rrotor_changes ORDER BY n").fetchall()
        if not changes:
            db.execute("COMMIT")
            return [], 0
        order: dict[tuple[str, str], None] = {}
        for change in changes:
            order.pop((change["tbl"], change["key"]), None)
            order[(change["tbl"], change["key"])] = None
        rows = []
        for table, key in order:  # the triggers are on TABLES alone
            row = db.execute(f"SELECT * FROM {table} WHERE {TABLES[table]} = ?", (key,)).fetchone()
            rows.append(
                {"tbl": table, "op": "upsert", "row": encoded(row)}
                if row is not None
                else {"tbl": table, "op": "delete", "key": key}
            )
        db.execute("COMMIT")
        return rows, int(changes[-1]["n"])
    finally:
        db.close()


def clear(store: Path, upto: int) -> None:
    db = connect(store)
    try:
        db.execute("DELETE FROM _rrotor_changes WHERE n <= ?", (upto,))
    finally:
        db.close()


def everything(store: Path) -> dict[str, Any]:
    """The whole store as one entry's body: the tables' schema and every row (the first time a store is captured)."""
    db = connect(store)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        schema = [
            str(r["sql"])
            for r in db.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name IN ('precedents', 'meta') AND sql IS NOT NULL "
                "AND type IN ('table', 'index') "
                "ORDER BY type DESC, name"
            )
        ]
        rows = [
            {"tbl": table, "op": "upsert", "row": encoded(row)}
            for table in TABLES
            for row in db.execute(f"SELECT * FROM {table}")
        ]
        db.execute("COMMIT")
        return {"schema": schema, "rows": rows}
    finally:
        db.close()


def settle(db: sqlite3.Connection) -> None:
    """A replica as a follower keeps it: a rollback journal, capture off, no changes of its own."""
    db.execute("PRAGMA journal_mode=DELETE")
    if has(db, "_rrotor_capture"):
        db.execute("UPDATE _rrotor_capture SET on_ = 0")
    if has(db, "_rrotor_changes"):
        db.execute("DELETE FROM _rrotor_changes")


def blobs(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in db.execute(f"PRAGMA table_info({table})") if "BLOB" in str(r[2]).upper()}


def apply(store: Path, rows: list[dict[str, Any]], schema: list[str] | None = None, reset: bool = False) -> None:
    """One entry's rows on a follower's replica, in one transaction (made from `schema` when it has no tables yet;
    emptied first with `reset`). Raises sqlite3.Error or ValueError when it cannot."""
    store.parent.mkdir(parents=True, exist_ok=True)
    db = connect(store)
    try:
        settle(db)
        db.execute("BEGIN IMMEDIATE")
        for sql in schema or []:
            name = re.match(r"^CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+(?:IF NOT EXISTS\s+)?\"?(\w+)", sql, re.I)
            if name is None:
                raise ValueError(f"not a table or an index: {sql[:60]}")
            known = db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name.group(1),)).fetchone()
            if known is None:
                db.execute(sql)
        if reset:  # the whole store follows, with its schema
            for table in TABLES:
                db.execute(f"DELETE FROM {table}")
        binary = {t: blobs(db, t) for t in TABLES}
        for change in rows:
            table = str(change.get("tbl"))
            if table not in TABLES:
                raise ValueError(f"not a table that syncs: {table!r}")
            if change.get("op") == "delete":
                db.execute(f"DELETE FROM {table} WHERE {TABLES[table]} = ?", (change.get("key"),))
                continue
            row = dict(change["row"])
            if not all(COLUMN.match(c) for c in row):
                raise ValueError("a column name is not one")
            values = [base64.b64decode(v) if c in binary[table] and isinstance(v, str) else v for c, v in row.items()]
            columns = ", ".join(f'"{c}"' for c in row)
            marks = ", ".join("?" for _ in row)
            db.execute(f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({marks})", values)
        db.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def backup(store: Path, dest: Path) -> None:
    """A consistent copy of a store (SQLite's online backup, one read transaction), settled as a replica."""
    src, out = connect(store), connect(dest)
    try:
        src.backup(out)
        settle(out)
    finally:
        src.close()
        out.close()

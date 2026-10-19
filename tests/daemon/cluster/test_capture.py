"""Change capture on the head and apply on a follower: triggers on the two tables that sync, what changed read as the
rows are now, a whole store shipped once, a replica kept in journal_mode=DELETE, and a reader with the kernel's
read-only open seeing an entry whole or not at all."""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from rrotord.cluster import capture
from tests.fakes import KERNEL

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS precedents (id TEXT PRIMARY KEY, situation TEXT NOT NULL, outcome TEXT NOT NULL,"
    " dim INTEGER NOT NULL, glyph BLOB NOT NULL, author TEXT NOT NULL DEFAULT 'anon', ts REAL NOT NULL DEFAULT 0,"
    " events BLOB)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def store(path: Path, wal: bool = True) -> Path:
    db = sqlite3.connect(path)
    if wal:
        db.execute("PRAGMA journal_mode=WAL")
    for sql in SCHEMA:
        db.execute(sql)
    db.commit()
    db.close()
    return path


def put(path: Path, rid: str, outcome: str = "yes", glyph: bytes = b"\x01\x02") -> None:
    db = sqlite3.connect(path)
    db.execute(
        "INSERT OR REPLACE INTO precedents (id, situation, outcome, dim, glyph) VALUES (?, '{}', ?, 2, ?)",
        (rid, outcome, glyph),
    )
    db.commit()
    db.close()


def ids(path: Path) -> list[str]:
    db = sqlite3.connect(path)
    try:
        return [r[0] for r in db.execute("SELECT id FROM precedents ORDER BY id")]
    finally:
        db.close()


def test_what_changed_is_captured_as_the_rows_are_now_and_applied_on_a_replica(tmp_path):
    head, replica = store(tmp_path / "head.sqlite"), tmp_path / "f" / "replica.sqlite"
    assert capture.collect(head) == ([], 0)  # no capture yet
    put(head, "a")  # before the capture: shipped with the whole store
    assert capture.install(head) is True and capture.install(head) is False
    assert capture.collect(head) == ([], 0)
    whole = capture.everything(head)
    capture.apply(replica, whole["rows"], whole["schema"], reset=True)
    assert ids(replica) == ["a"]
    put(head, "b", glyph=b"\x00\xff")
    put(head, "b", outcome="no", glyph=b"\x00\xff")
    db = sqlite3.connect(head)
    db.execute("INSERT INTO meta VALUES ('learned', '{\"w\": 1}')")
    db.execute("UPDATE meta SET key = 'old' WHERE key = 'learned'")  # a key changed: the old one is gone
    db.execute("DELETE FROM precedents WHERE id = 'a'")
    db.commit()
    db.close()
    rows, upto = capture.collect(head)
    assert upto > 0
    assert [(r["tbl"], r["op"]) for r in rows] == [
        ("precedents", "upsert"),
        ("meta", "delete"),
        ("meta", "upsert"),
        ("precedents", "delete"),
    ]
    assert rows[0]["row"]["outcome"] == "no" and rows[0]["row"]["glyph"] == "AP8="
    capture.clear(head, upto)
    assert capture.collect(head) == ([], 0)
    capture.apply(replica, rows)
    assert ids(replica) == ["b"]
    db = sqlite3.connect(replica)
    assert db.execute("SELECT glyph FROM precedents").fetchone()[0] == b"\x00\xff"
    assert db.execute("SELECT key FROM meta").fetchall() == [("old",)]
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    db.close()
    assert (head.parent / "replica.sqlite").exists() is False
    assert replica.read_bytes()[18] == 1  # a rollback journal: never read immutable by the kernel


def test_a_store_without_its_tables_and_entries_that_do_not_apply(tmp_path):
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    assert capture.install(empty) is False
    replica = store(tmp_path / "r.sqlite", wal=False)
    for rows, schema, why in (
        ([{"tbl": "other", "op": "upsert", "row": {}}], None, "not a table that syncs"),
        ([{"tbl": "meta", "op": "upsert", "row": {"key; DROP": 1}}], None, "not one"),
        ([], ["CREATE VIEW v AS SELECT 1"], "not a table or an index"),
    ):
        with pytest.raises(ValueError, match=why):
            capture.apply(replica, rows, schema)
    with pytest.raises(sqlite3.Error):
        capture.apply(tmp_path / "n" / "x.sqlite", [{"tbl": "meta", "op": "upsert", "row": {"key": "k", "value": "v"}}])
    capture.apply(replica, [], [*SCHEMA, "CREATE INDEX IF NOT EXISTS by_outcome ON precedents (outcome)"])
    capture.apply(replica, [], ["CREATE INDEX by_outcome ON precedents (outcome)"])  # there already: kept


def test_a_backup_is_a_settled_replica(tmp_path):
    head = store(tmp_path / "head.sqlite")
    put(head, "a")
    capture.install(head)
    put(head, "b")
    copy = tmp_path / "copy.sqlite"
    capture.backup(head, copy)
    db = sqlite3.connect(copy)
    assert db.execute("SELECT on_ FROM _rrotor_capture").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM _rrotor_changes").fetchone() == (0,)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    db.close()
    put(copy, "c")  # capture off: a follower's replica records no changes of its own
    db = sqlite3.connect(copy)
    assert db.execute("SELECT COUNT(*) FROM _rrotor_changes").fetchone() == (0,)
    db.close()


@pytest.fixture
def kernel() -> Iterator[object]:
    sys.path.insert(0, str(KERNEL))
    try:
        import glyphh_kernel  # type: ignore[import-not-found]

        yield glyphh_kernel
    finally:
        sys.path.remove(str(KERNEL))
        for name in ("glyphh_kernel", "glyphh_spec", "glyphh_nsm", "glyphh_lexicon"):
            sys.modules.pop(name, None)


def test_a_reader_with_the_kernels_read_only_open_sees_an_entry_whole_or_not_at_all(tmp_path, kernel):
    """The coordinator's caveat: a WAL store with no -wal and -shm beside it is read immutable, which a writer would
    make inconsistent. A replica is a rollback-journal file, so the read-only open is plain mode=ro and SQLite's locks
    hold: an apply waits for a read in progress, and the read sees the store as it began."""
    head = tmp_path / "head.sqlite"
    writer = kernel.Space(dim=64, path=str(head))
    for i in range(3):
        writer.record({"x": i}, "yes", rid=f"r{i}")
    writer._db.close()
    capture.install(head)
    replica = tmp_path / "replica.sqlite"
    capture.backup(head, replica)
    reader = kernel.Space(dim=64, path=str(replica), readonly=True)
    assert len(reader) == 3 and "immutable" not in str(reader._db.execute("PRAGMA database_list").fetchall())
    with pytest.raises(kernel.ReadOnlyError):
        reader.record({"x": 9}, "no")
    other = kernel.Space(dim=64, path=str(head))
    other.record({"x": 3}, "no", rid="r3")
    other.record({"x": 4}, "no", rid="r4")
    other._db.close()
    rows, _ = capture.collect(head)
    reading = sqlite3.connect(f"{replica.resolve().as_uri()}?mode=ro", uri=True)
    reading.execute("BEGIN")
    assert reading.execute("SELECT COUNT(*) FROM precedents").fetchone() == (3,)
    done = threading.Event()

    def apply() -> None:
        capture.apply(replica, rows)
        done.set()

    applier = threading.Thread(target=apply)
    applier.start()
    time.sleep(0.3)
    assert not done.is_set()  # it waits for the read in progress
    assert reading.execute("SELECT COUNT(*) FROM precedents").fetchone() == (3,)  # never half of it
    reading.execute("COMMIT")
    applier.join(10)
    assert done.is_set() and reading.execute("SELECT COUNT(*) FROM precedents").fetchone() == (5,)
    reading.close()
    assert reader.refresh() == 2 and len(reader) == 5  # the kernel's read-only store, refreshed, sees the rows

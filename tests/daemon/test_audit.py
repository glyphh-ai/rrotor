"""The audit store: Enterprise keeps each event in a day file, fsynced; below it each is a log line, kept nowhere;
retention by days and by size, never today's file; a drain reads forward from a cursor; GET /v1/audit reads newest
first, filtered and paged; payloads only with RROTOR_LOG_PAYLOADS and only in the store."""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request

from rrotord import audit
from rrotord.audit import RROTOR, SERVER, Audit, Who, acting, backwards, client_of, clip, current
from rrotord.errors import BadRequest

DAY = dt.datetime(2026, 10, 19, 9, 0, tzinfo=dt.UTC)


class Clock:
    def __init__(self, at: dt.datetime = DAY) -> None:
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at


def store(tmp_path: Path, clock: Clock | None = None, **kw) -> Audit:
    return Audit(tmp_path / "audit", "rrotor-1", tier=lambda: "enterprise", clock=clock or Clock(), **kw)


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_enterprise_keeps_each_event_in_its_days_file(tmp_path):
    a = store(tmp_path)
    who = Who({"type": "key", "key_sha256": "3f" * 32}, "10.0.4.17", "rest")
    event = a.emit("run.call", who=who, model="incidents", version=3, outcome={"status": "done", "code": None})
    path = tmp_path / "audit" / "2026-10-19.jsonl"
    assert lines(path) == [event] and path.stat().st_mode & 0o777 == 0o600
    assert event["at"] == "2026-10-19T09:00:00Z" and event["node_id"] == "rrotor-1" and event["cluster_id"] is None
    assert (event["kind"], event["actor"], event["client"], event["via"]) == (
        "run.call",
        who.actor,
        "10.0.4.17",
        "rest",
    )
    assert a.kept and a.days() == ["2026-10-19.jsonl"]


def test_below_enterprise_an_event_is_a_log_line_and_nothing_is_kept(tmp_path, caplog):
    a = Audit(tmp_path / "audit", "rrotor-1")
    with caplog.at_level(logging.INFO, logger="rrotor.audit"):
        a.emit("run.end", model="m", payload={"inputs": {"secret": "s3cr3t"}})
    record = caplog.records[-1]
    assert (record.name, record.getMessage(), record.__dict__["model"]) == ("rrotor.audit", "run.end", "m")
    assert record.__dict__["actor"] == {"type": "rrotor"} and "inputs" not in record.__dict__
    assert not (tmp_path / "audit").exists() and a.days() == [] and not a.kept


def test_payloads_are_kept_only_when_asked_and_cut_to_4_kb(tmp_path):
    big = {"text": "x" * 10_000}
    off = store(tmp_path / "off").emit("run.end", payload={"inputs": big, "data": 1})
    assert "inputs" not in off and "data" not in off
    on = store(tmp_path / "on", payloads=True).emit("run.end", payload={"inputs": big, "data": {"a": 1}})
    assert len(on["inputs"].encode()) == 4096 and on["data"] == '{"a":1}'
    assert clip("é" * 3000).endswith("é") and len(clip("é" * 3000).encode()) <= 4096


def test_who_is_the_caller_set_else_rrotor():
    assert current() is RROTOR
    with acting(SERVER):
        assert current() is SERVER
    assert current() is RROTOR


def test_the_client_is_the_last_forwarded_hop_else_the_connection():
    assert client_of(make_mocked_request("GET", "/", headers={"X-Forwarded-For": "1.2.3.4, 10.0.4.17"})) == "10.0.4.17"
    assert client_of(make_mocked_request("GET", "/", headers={"X-Forwarded-For": " , "})) is None  # no transport


def test_retention_deletes_old_days_then_the_oldest_past_the_size_never_today(tmp_path, caplog):
    folder = tmp_path / "audit"
    folder.mkdir()
    for day in ("2026-07-01", "2026-07-21", "2026-10-17", "2026-10-18"):
        (folder / f"{day}.jsonl").write_text("x" * (600 * 1024) + "\n")
    (folder / "notes.txt").write_text("not a day")
    a = store(tmp_path, max_mb=1)
    with caplog.at_level(logging.INFO, logger="rrotor"):
        a.emit("keys.changed", keys=1)
    assert a.days() == ["2026-10-18.jsonl", "2026-10-19.jsonl"] and (folder / "notes.txt").exists()
    deleted = next(r for r in caplog.records if r.getMessage() == "audit files deleted")
    assert deleted.__dict__["files"] == ["2026-07-01.jsonl", "2026-07-21.jsonl", "2026-10-17.jsonl"]
    assert a.prune() == []  # nothing more to go: what is left fits, or is today's


def test_a_full_folder_is_pruned_as_it_grows(tmp_path):
    clock = Clock()
    a = store(tmp_path, clock, max_mb=1)
    a.emit("one")
    (tmp_path / "audit" / "2026-10-18.jsonl").write_text("x" * (1024 * 1024))
    a.size = 1024 * 1024  # as if it had grown
    a.emit("two")
    assert a.days() == ["2026-10-19.jsonl"]
    clock.at += dt.timedelta(days=1)
    a.emit("three")  # a new day prunes first
    assert a.days() == ["2026-10-19.jsonl", "2026-10-20.jsonl"]


def test_today_is_never_deleted_even_alone_over_the_size(tmp_path):
    a = store(tmp_path, max_mb=1)
    a.emit("one")
    (tmp_path / "audit" / "2026-10-19.jsonl").write_text("x" * (2 * 1024 * 1024))
    assert a.prune() == [] and a.days() == ["2026-10-19.jsonl"]


def test_a_drain_reads_forward_from_its_cursor_across_days(tmp_path):
    clock = Clock()
    a = store(tmp_path, clock)
    assert a.read(None, 10) == ([], None)
    first = [a.emit("e", n=n) for n in range(3)]
    events, cursor = a.read(None, 2)
    assert [e["n"] for e in events] == [0, 1]
    events, cursor = a.read(cursor, 10)
    assert [e["n"] for e in events] == [2] and cursor is not None
    assert a.read(cursor, 10) == ([], cursor)  # nothing new: the cursor stays
    clock.at += dt.timedelta(days=1)
    a.emit("e", n=3)
    with (tmp_path / "audit" / "2026-10-20.jsonl").open("a") as f:
        f.write('not json\n{"n": 4')  # a broken line skipped, a line being written left for later
    events, cursor2 = a.read(cursor, 10)
    assert [e["n"] for e in events] == [3] and cursor2 is not None and cursor2[0] == "2026-10-20.jsonl"
    assert a.read(None, 2)[0] == first[:2]


def test_a_cursor_whose_file_went_moves_to_the_next_kept(tmp_path):
    a = store(tmp_path)
    a.emit("e", n=1)
    assert a.read(("2026-01-01.jsonl", 99), 10)[0][0]["n"] == 1
    assert a.read(("2027-01-01.jsonl", 0), 10) == ([], ("2027-01-01.jsonl", 0))


def test_backwards_reads_lines_last_first_across_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "BLOCK", 7)
    path = tmp_path / "f"
    path.write_bytes(b"alpha\n\nbravo-charlie\ndelta\n")
    found = list(backwards(path, path.stat().st_size))
    assert found == [(21, b"delta"), (7, b"bravo-charlie"), (0, b"alpha")]
    assert list(backwards(path, 7)) == [(0, b"alpha")]
    path.write_bytes(b"solo")
    assert list(backwards(path, 4)) == [(0, b"solo")]
    path.write_bytes(b"\nlast\n")
    assert list(backwards(path, 6)) == [(1, b"last")]


def test_the_query_is_newest_first_filtered_and_paged(tmp_path):
    clock = Clock()
    a = store(tmp_path, clock)
    key = Who({"type": "key", "key_sha256": "ab" * 32})
    signed = Who({"type": "sign-in", "sub": "user_1"})
    for n in range(5):
        a.emit("run.call", who=key if n % 2 else signed, model="m" if n < 4 else "other", run_id=f"r{n}", n=n)
        clock.at += dt.timedelta(hours=1)
    clock.at += dt.timedelta(days=1)
    a.emit("keys.changed", n=5)
    with (tmp_path / "audit" / "2026-10-19.jsonl").open("a") as f:
        f.write("[1]\n")
    newest = a.query({})
    assert [e["n"] for e in newest["events"]] == [5, 4, 3, 2, 1, 0] and newest["cursor"] is None
    page = a.query({"limit": "2"})
    assert [e["n"] for e in page["events"]] == [5, 4] and page["cursor"]
    rest = a.query({"limit": 3, "cursor": page["cursor"]})
    assert [e["n"] for e in rest["events"]] == [3, 2, 1]
    assert [e["n"] for e in a.query({"cursor": rest["cursor"]})["events"]] == [0]
    assert [e["n"] for e in a.query({"kind": "run.call", "model": "m"})["events"]] == [3, 2, 1, 0]
    assert [e["n"] for e in a.query({"actor": "user_1"})["events"]] == [4, 2, 0]
    assert [e["n"] for e in a.query({"actor": "ab" * 32})["events"]] == [3, 1]
    assert [e["n"] for e in a.query({"actor": "sign-in", "run_id": "r2"})["events"]] == [2]
    window = {"since": "2026-10-19T10:00:00Z", "until": "2026-10-19T12:00:00+00:00"}
    assert [e["n"] for e in a.query(window)["events"]] == [3, 2, 1]
    assert [e["n"] for e in a.query({"since": "2026-10-20T00:00:00"})["events"]] == [5]
    assert [e["n"] for e in a.query({"until": "2026-10-19T23:59:59Z"})["events"]] == [4, 3, 2, 1, 0]
    assert a.query({"model": ""})["events"][0]["n"] == 5


def test_an_event_without_a_time_matches_only_without_a_window(tmp_path):
    a = store(tmp_path)
    folder = tmp_path / "audit"
    folder.mkdir()
    (folder / "2026-10-19.jsonl").write_text('{"kind": "odd"}\n')
    assert len(a.query({})["events"]) == 1
    assert a.query({"since": "2026-10-01T00:00:00Z"})["events"] == []


@pytest.mark.parametrize(
    "given",
    [
        {"limit": 0},
        {"limit": "many"},
        {"limit": 1001},
        {"limit": True},
        {"cursor": "nope"},
        {"cursor": "2026-10-19:x"},
        {"since": "yesterday"},
        {"kind": 3},
    ],
)
def test_a_bad_query_is_refused(tmp_path, given):
    with pytest.raises(BadRequest):
        store(tmp_path).query(given)

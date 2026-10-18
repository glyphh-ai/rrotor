"""The runtime's precedents: counted across its Ada models' stores, read again every nine minutes, due when the total
differs from the one last reported."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rrotor import ada
from rrotord import precedents
from rrotord.manifest import Manifest
from rrotord.models import Models
from rrotord.precedents import Precedents


def model(models: Models, name: str, needs: list[str], rows: int | None) -> None:
    """A model's version recorded, and its store holding `rows` precedents (None: no store)."""
    found = {"name": name, "functions": {"start": {}}, "needs": needs, "data": True}
    models.record(name, 1, "a" * 40, Manifest.model_validate(found))
    if rows is None:
        return
    store(models.data(name) / ada.STORE, rows)


def store(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS precedents (id TEXT PRIMARY KEY)")
    have = db.execute("SELECT COUNT(*) FROM precedents").fetchone()[0]
    db.executemany("INSERT INTO precedents VALUES (?)", [(f"p{i}",) for i in range(have, rows)])
    db.commit()
    db.close()


def test_the_total_is_every_ada_models_store_and_nothing_else(tmp_path):
    models = Models(tmp_path)
    assert Precedents(models).count() == 0  # no models
    model(models, "tickets", ["ada"], 3)
    model(models, "incidents", ["ada"], 4)
    model(models, "fresh", ["ada"], None)  # deployed, nothing recorded yet
    model(models, "sales", [], 9)  # not an Ada model: a file of that name is not precedents
    assert Precedents(models).count() == 7


def test_a_store_that_does_not_read_makes_the_total_unknown_and_is_logged(tmp_path, caplog):
    models = Models(tmp_path)
    model(models, "tickets", ["ada"], 3)
    model(models, "broken", ["ada"], None)
    (models.data("broken") / ada.STORE).parent.mkdir(parents=True)
    (models.data("broken") / ada.STORE).write_bytes(b"not a database")
    assert Precedents(models).count() is None and "a precedent store did not read" in caplog.text


def test_the_total_is_due_once_after_a_start_then_when_a_new_reading_differs(tmp_path):
    models = Models(tmp_path)
    model(models, "tickets", ["ada"], 3)
    now = [0.0]
    counted = Precedents(models, clock=lambda: now[0])
    assert counted.due() and counted.total == 3  # the first reading: due, whatever it is
    counted.reported = 3
    assert not counted.due()
    store(models.data("tickets") / ada.STORE, 5)
    now[0] = precedents.READ_EVERY - 1
    assert not counted.due() and counted.total == 3  # not read again yet
    now[0] = precedents.READ_EVERY
    assert counted.due() and counted.total == 5
    counted.reported = 5
    now[0] = 2 * precedents.READ_EVERY
    assert not counted.due()  # read again, unchanged


def test_a_reading_that_fails_keeps_the_one_before(tmp_path, monkeypatch):
    counted = Precedents(Models(tmp_path), clock=lambda: 0.0)
    monkeypatch.setattr(counted, "count", lambda: None)
    assert not counted.due() and counted.total is None  # nothing has read: nothing to report
    counted.total, counted.reported, counted.read_at = 4, 4, -precedents.READ_EVERY
    assert not counted.due() and counted.total == 4

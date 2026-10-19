"""rrotor.ada: the spec read and checked, the store opened on the kernel, a re-encode, and what it refuses."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from rrotor import ada
from rrotor.ctx import Ctx, RunState
from rrotor.errors import ModelError, PartMissing
from rrotor.http import Http
from tests.fakes import KERNEL

SPEC = {
    "name": "t",
    "seed": 7,
    "dimension": 2048,
    "layers": [{"name": "a", "segments": [{"name": "b", "roles": [{"name": "x"}]}]}],
}


@pytest.fixture
def kernel():
    sys.path.insert(0, str(KERNEL))
    try:
        yield
    finally:
        sys.path.remove(str(KERNEL))
        for name in ("glyphh_kernel", "glyphh_spec", "glyphh_nsm"):
            sys.modules.pop(name, None)


def ctx(home: Path | None, data: Path | None, clearance: float = 1.0) -> Ctx:
    return Ctx(
        inputs={},
        approvals=frozenset(),
        state=RunState(),
        files=home or Path("."),
        http=Http(frozenset(), None),
        answer=None,
        sink=lambda op, d: None,
        data=data,
        home=home,
        clearance=clearance,
        caller="test",
    )


def test_the_spec_is_read_checked_and_its_encoding_named(tmp_path):
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    assert ada.read_spec(tmp_path) == SPEC
    assert ada.encoding_of(SPEC) == {"seed": 7, "dimension": 2048} and ada.encoding_of({}) == {
        "seed": 42,
        "dimension": 4096,
    }
    for text, why in (
        ("", "not JSON"),
        ("[]", "a JSON object"),
        ('{"seed": "x"}', "whole number"),
        ('{"dimension": true}', "whole number"),
    ):
        (tmp_path / "spec.json").write_text(text)
        with pytest.raises(ModelError, match=why):
            ada.read_spec(tmp_path)
    with pytest.raises(ModelError, match=r"beside model\.py"):
        ada.read_spec(tmp_path / "nowhere")


def test_without_the_part_the_kernel_is_missing(tmp_path):
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    with pytest.raises(PartMissing) as caught:
        ada.open(ctx(tmp_path, tmp_path / "data"))
    assert caught.value.mod == "ada"
    with pytest.raises(PartMissing):
        ada.reencode(ctx(tmp_path, tmp_path / "data"))


def test_open_needs_the_data_folder_and_a_spec_the_kernel_takes(tmp_path, kernel):
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    with pytest.raises(ModelError, match='"data": true'):
        ada.open(ctx(tmp_path, None))
    (tmp_path / "spec.json").write_text(json.dumps({**SPEC, "layers": []}))
    with pytest.raises(ModelError, match="refused"):
        ada.open(ctx(tmp_path, tmp_path / "data"))


def test_open_records_and_reencode_rebuilds_the_store(tmp_path, kernel):
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    data = tmp_path / "data"
    c = ctx(tmp_path, data, clearance=0.5)
    model = ada.open(c, author="ada")
    assert c.ada is model and model.spec.seed == 7
    model.record({"a": {"b": {"x": "one"}}}, "yes")
    assert len(model) == 1 and (data / ada.STORE).is_file()
    assert ada.reencode(c) == 1
    (tmp_path / "spec.json").write_text(json.dumps({**SPEC, "seed": 8}))
    assert ada.reencode(c) == 1
    again = ada.open(ctx(tmp_path, data), refresh_every=50)
    assert len(again) == 1 and again.query({"a": {"b": {"x": "one"}}}).top == "yes"
    assert again.refresh_every == 50 and again.given.spec_id == again.spec.spec_id
    assert (again.space.k, again.space.min_sim) == (15, 0.1)  # the kernel's defaults, no argument given
    assert ada.open(ctx(tmp_path, data), k=3).space.k == 3
    assert ada.reencode(ctx(tmp_path, tmp_path / "empty")) == 0


def test_the_model_folder_defaults_to_the_working_directory(tmp_path, kernel, monkeypatch):
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    monkeypatch.chdir(tmp_path)
    assert ada.open(ctx(None, tmp_path / "data")).spec.name == "t"
    assert ada.reencode(ctx(None, tmp_path / "data")) == 0


def test_the_workers_internal_reencode_is_not_a_models_function(tmp_path, kernel):
    import types

    from rrotor._worker import loop

    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    outcome = loop.Worker._call(types.ModuleType("model"), ada.REENCODE, ctx(tmp_path, tmp_path / "data"))
    assert outcome.result.status == "done" and outcome.result.data == {"reencoded": 0}


KEYED = {
    "name": "t",
    "seed": 7,
    "dimension": 2048,
    "layers": [{"name": "a", "segments": [{"name": "b", "roles": [{"name": "x"}, {"name": "id", "key_part": True}]}]}],
}


def case(i: int, x: str = "one") -> dict[str, Any]:
    return {"a": {"b": {"x": x, "id": i}}}


def capped(tmp_path: Path, data: str = "data", spec: dict[str, Any] = KEYED, **limits: Any) -> Any:
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    c = ctx(tmp_path, tmp_path / data)
    c.limits = limits
    return ada.open(c)


def test_at_the_cap_a_new_key_is_limit_exceeded_not_indexed_and_the_store_unchanged(tmp_path, kernel):
    model = capped(tmp_path, precedents=2, plan="Pro")
    model.record(case(1), "yes")
    model.record(case(2), "yes")
    with pytest.raises(ada.LimitExceeded) as caught:
        model.record(case(3), "yes")
    assert caught.value.data == {
        "status": "limit_exceeded",
        "indexed": False,
        "message": "Precedent store capacity limit reached (2 max for the Pro plan). "
        "Upgrade the key to index further precedents.",
        "precedents": 2,
        "cap": 2,
    }
    assert str(caught.value) == caught.value.data["message"] and not isinstance(caught.value, ModelError)
    assert len(model) == 2 and ada.count(tmp_path / "data" / ada.STORE) == 2
    with pytest.raises(ada.LimitExceeded):
        model.space.record({"a": {"b": {"x": "raw"}}}, "yes")  # the kernel's own record, no id: new
    big = capped(tmp_path, "big", precedents=50000, plan="Pro", precedents_elsewhere=50000)
    with pytest.raises(ada.LimitExceeded, match=r"\(50,000 max for the Pro plan\)"):
        big.record(case(1), "yes")


def test_a_record_whose_key_the_store_holds_replaces_its_row_at_the_cap(tmp_path, kernel):
    model = capped(tmp_path, precedents=2)
    model.record(case(1), "yes")
    model.record(case(2), "yes")
    model.record(case(2, "two"), "no")  # the same key: replaces, the store does not grow
    assert len(model) == 2 and model.query(case(2, "two")).top == "no"
    rid = model.space.record({"a": {"b": {"x": "kept"}}}, "yes", None, ada._ids(tmp_path / "data" / ada.STORE).pop())
    assert isinstance(rid, str) and len(model) == 2  # by position, too
    with pytest.raises(ada.LimitExceeded, match="for the Community plan"):  # no plan named: Community
        model.record(case(3), "yes")


def test_unlimited_or_no_cap_is_never_checked(tmp_path, kernel):
    assert capped(tmp_path, "a", precedents=None).record(case(1), "yes")
    assert capped(tmp_path, "b").record(case(1), "yes")
    odd = capped(tmp_path, "c", precedents=1, precedents_elsewhere=True)  # not a count: none elsewhere
    assert odd.record(case(1), "yes")
    assert capped(tmp_path, "d", precedents=True).record(case(1), "yes")  # not a count: no cap


def test_the_cap_is_on_the_nodes_total_with_the_other_models_counted(tmp_path, kernel):
    model = capped(tmp_path, precedents=3, precedents_elsewhere=2)
    model.record(case(1), "yes")
    with pytest.raises(ada.LimitExceeded) as caught:
        model.record(case(2), "yes")
    assert (caught.value.data["precedents"], caught.value.data["cap"], len(model)) == (3, 3, 1)


def store_of(tmp_path: Path, name: str, ids: range) -> Path:
    other = capped(tmp_path, name)
    for i in ids:
        other.record(case(i), "yes")
    other.space.close()
    return tmp_path / name / ada.STORE


def test_a_merge_is_capped_all_or_nothing(tmp_path, kernel):
    five = store_of(tmp_path, "five", range(5))
    model = capped(tmp_path, precedents=4)
    model.record(case(0), "yes")
    with pytest.raises(ada.LimitExceeded) as caught:
        model.space.merge(str(five))  # four new rows on one held: past the cap of four, so none
    assert caught.value.data["precedents"] == 1 and len(model) == 1 and ada.count(tmp_path / "data" / ada.STORE) == 1
    three = store_of(tmp_path, "three", range(3))
    assert model.space.merge(str(three)) == 2 and len(model) == 3  # two new: within the cap
    held = store_of(tmp_path, "held", range(3))
    model.record(case(9), "yes")
    assert len(model) == 4 and model.space.merge(str(held)) == 0  # at the cap, nothing new: it goes
    (tmp_path / "junk.sqlite").write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        model.space.merge(str(tmp_path / "junk.sqlite"))  # not a store: the kernel says so
    with pytest.raises(sqlite3.Error):
        model.space.merge(str(tmp_path / "missing.sqlite"))  # no such file: nothing to add, and the kernel refuses it
    fresh = capped(tmp_path, "fresh", precedents=10)
    assert fresh.space.merge(str(three)) == 3  # into a store made at open, with nothing in it
    assert not ada._held(tmp_path / "nowhere.sqlite", "x") and ada._ids(tmp_path / "nowhere.sqlite") == set()


def test_a_store_over_the_cap_after_a_downgrade_is_kept_answers_and_learns_and_only_growth_is_refused(tmp_path, kernel):
    full = capped(tmp_path)
    for i in range(60):
        full.record(case(i, "one" if i % 2 else "two"), "yes" if i % 2 else "no")
    full.space.close()
    model = capped(tmp_path, precedents=50, plan="Free")
    assert len(model) == 60 and model.query(case(1)).top == "yes"
    assert model.facts(case(1)) and model.probabilities(case(1))
    model.refresh()  # learn: weights from the store's own precedents
    model.calibrate()
    with pytest.raises(ada.LimitExceeded) as caught:
        model.record(case(100), "yes")
    assert caught.value.data["precedents"] == 60 and len(model) == 60
    model.record(case(1), "no")  # a replace still goes, over the cap
    assert ada.count(tmp_path / "data" / ada.STORE) == 60  # nothing deleted, nothing added


def test_count_reads_the_stores_precedents_as_the_cap_counts_them_without_the_kernel(tmp_path, kernel):
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    store = tmp_path / "data" / ada.STORE
    assert ada.count(store) == 0  # not made yet
    model = ada.open(ctx(tmp_path, tmp_path / "data"))
    model.record({"a": {"b": {"x": "one"}}}, "yes")
    model.record({"a": {"b": {"x": "two"}}}, "no")
    assert ada.count(store) == len(model) == 2  # read while the model holds it open
    model.space.close()
    (tmp_path / "other.sqlite").write_bytes(b"")
    with pytest.raises(sqlite3.Error):
        ada.count(tmp_path / "other.sqlite")  # no precedents table: not a store


def test_a_followers_run_opens_the_store_read_only_on_kernel_3_10(tmp_path, kernel):
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    data = tmp_path / "data"
    writer = ada.open(ctx(tmp_path, data))
    writer.record({"a": {"b": {"x": "one"}}}, "yes")
    follower = ctx(tmp_path, data)
    follower.readonly = True
    model = ada.open(follower)
    assert model.readonly and len(model.space) == 1
    with pytest.raises(Exception, match="E_READ_ONLY") as caught:
        model.record({"a": {"b": {"x": "two"}}}, "no")
    assert getattr(caught.value, "code", None) == "E_READ_ONLY" and ada.count(data / ada.STORE) == 1


def test_an_older_kernel_cannot_open_a_store_read_only(tmp_path, monkeypatch):
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))

    class Spec:
        @staticmethod
        def from_json(_data: Any) -> object:
            return object()

    class Model:  # kernel 3.9: no readonly
        def __init__(self, spec: Any, *, author: str, **levers: Any) -> None:
            if author == "boom":
                raise TypeError("boom")
            raise TypeError("Model.__init__() got an unexpected keyword argument 'readonly'")

    old = type(sys)("glyphh_spec")
    old.Spec, old.Model, old.SpecError = Spec, Model, ValueError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "glyphh_spec", old)
    monkeypatch.setitem(sys.modules, "glyphh_kernel", type(sys)("glyphh_kernel"))
    monkeypatch.setattr(ada, "_kernel", lambda: (sys.modules["glyphh_kernel"], old))
    follower = ctx(tmp_path, tmp_path / "data")
    follower.readonly = True
    with pytest.raises(ada.KernelTooOld, match=r"kernel 3\.10") as caught:
        ada.open(follower)
    assert caught.value.code == "E_ADA_KERNEL"
    with pytest.raises(TypeError, match="boom"):
        ada.open(follower, author="boom")

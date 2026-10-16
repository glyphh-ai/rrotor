"""rrotor.ada: the spec read and checked, the store opened on the kernel, a re-encode, and what it refuses."""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
    again = ada.open(ctx(tmp_path, data))
    assert len(again) == 1 and again.query({"a": {"b": {"x": "one"}}}).top == "yes"
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


def test_the_plans_precedents_limit_caps_record(tmp_path, kernel):
    (tmp_path / "spec.json").write_text(json.dumps(SPEC))
    c = ctx(tmp_path, tmp_path / "data")
    c.limits = {"precedents": 2, "models": 1}
    model = ada.open(c)
    model.record({"a": {"b": {"x": "one"}}}, "yes")
    model.record({"a": {"b": {"x": "two"}}}, "yes")
    with pytest.raises(ModelError, match="allows 2 precedents") as caught:
        model.record({"a": {"b": {"x": "three"}}}, "yes")
    assert caught.value.code == "E_LIMIT_PRECEDENTS" and len(model) == 2
    free = ctx(tmp_path, tmp_path / "data2")
    free.limits = {"precedents": None}
    assert ada.open(free).record({"a": {"b": {"x": "one"}}}, "yes")

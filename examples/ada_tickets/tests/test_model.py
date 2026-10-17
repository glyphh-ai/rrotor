"""The tickets model, run as rrotor would (rrotor.testing.Harness), on the kernel the repo's tests carry."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from rrotor.testing import Harness

HERE = Path(__file__).parent.parent
KERNEL = HERE.parent.parent / "tests" / "kernel"


@pytest.fixture
def model(tmp_path):
    sys.path.insert(0, str(KERNEL))
    spec = importlib.util.spec_from_file_location("tickets_model", HERE / "model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.path.remove(str(KERNEL))


def ticket(i, title, component, severity, team="infra", opened="2026-10-01T12:00:00Z"):
    return {
        "issue": {"body": {"title": title, "area": component, "severity": severity, "id": i}},
        "meta": {"time": {"opened": opened}, "owner": {"team": team}},
    }


def harness(model, tmp_path, clearance=1.0):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Harness(model, tmp_path / "files", data=data, home=HERE, clearance=clearance)


def test_tickets_are_recorded_and_the_closest_precedent_answers(model, tmp_path):
    h = harness(model, tmp_path)
    assert h.call("start").data == {"precedents": 0}
    for i, (title, comp, sev, outcome) in enumerate(
        [
            ("login fails", "auth", 8, "page oncall"),
            ("login slow", "auth", 5, "page oncall"),
            ("typo in docs", "docs", 1, "backlog"),
        ]
    ):
        r = harness(model, tmp_path).call("record", {"data": ticket(i, title, comp, sev), "outcome": outcome})
        assert r.status == "done" and r.data["precedents"] == i + 1
    r = harness(model, tmp_path).call("query", {"data": ticket(9, "login broken", "auth", 7)})
    assert r.status == "done" and r.data["sufficient"] and r.data["top"] == "page oncall"
    facts = harness(model, tmp_path).call("facts", {"data": ticket(9, "login broken", "auth", 7), "top": 2}).data
    assert len(facts) == 2 and facts[0]["tree"]
    probs = harness(model, tmp_path).call("probabilities", {"data": ticket(9, "login broken", "auth", 7)}).data
    assert set(probs) <= {"page oncall", "backlog"} and abs(sum(probs.values()) - 1) < 1e-6


def test_decide_answers_when_confident_and_asks_then_records_when_not(model, tmp_path):
    for i in range(3):
        harness(model, tmp_path).call("record", {"data": ticket(i, "login fails", "auth", 8), "outcome": "page oncall"})
    sure = harness(model, tmp_path).call("decide", {"data": ticket(7, "login fails again", "auth", 8)})
    assert sure.status == "done" and sure.data["outcome"] == "page oncall" and sure.data["from"] == 3
    h = harness(model, tmp_path)
    asked = h.call(
        "decide", {"data": ticket(8, "printer on fire", "facilities", 2, team="office", opened="2020-01-01T00:00:00Z")}
    )
    assert asked.status == "needs_input"
    answered = h.resume("call the fire brigade")
    assert (
        answered.status == "done" and answered.data["outcome"] == "call the fire brigade" and answered.data["recorded"]
    )
    assert harness(model, tmp_path).call("start").data == {"precedents": 4}


def test_a_low_clearance_sees_no_meta_layer(model, tmp_path):
    harness(model, tmp_path).call("record", {"data": ticket(1, "login fails", "auth", 8), "outcome": "page oncall"})
    full = harness(model, tmp_path, clearance=1.0).call("facts", {"data": ticket(2, "login fails", "auth", 8)}).data
    low = harness(model, tmp_path, clearance=0.5).call("facts", {"data": ticket(2, "login fails", "auth", 8)}).data
    assert '"meta"' in json.dumps(full[0]["tree"]) and '"meta"' not in json.dumps(low[0]["tree"])


def test_learn_weighs_the_roles_from_the_store_and_needs_ten_graded_tickets(model, tmp_path):
    assert harness(model, tmp_path).call("learn").status == "failed"
    for i in range(12):
        auth = i % 2 == 0
        harness(model, tmp_path).call(
            "record",
            {"data": ticket(i, "login fails" if auth else "typo in docs", "auth" if auth else "docs", 8 if auth else 1), "outcome": "page oncall" if auth else "backlog"},
        )
    r = harness(model, tmp_path).call("learn")
    assert r.status == "done" and r.data["precedents"] == 12 and r.data["roles"]["issue.body.component"] == 1.0
    assert harness(model, tmp_path).call("query", {"data": ticket(9, "login broken", "auth", 7)}).data["top"] == "page oncall"

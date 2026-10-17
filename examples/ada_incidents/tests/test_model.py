"""The incidents model, run as rrotor would (rrotor.testing.Harness), on the kernel the repo's tests carry."""

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
    spec = importlib.util.spec_from_file_location("incidents_model", HERE / "model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.path.remove(str(KERNEL))


def incident(
    i,
    message,
    service="api",
    endpoint="/v1/checkout",
    cls="TimeoutError",
    status=504,
    users=1200,
    cause="gateway rate limit hit on retry storm",
    owner="payments",
    runbook="RB-12",
    opened="2026-10-16T09:12:00Z",
):
    return {
        "error": {
            "where": {"service": service, "endpoint": endpoint, "region": "iad", "version": "2.14.0"},
            "what": {"class": cls, "message": message, "status": status, "id": f"inc-{i}"},
            "when": {"opened": opened},
        },
        "impact": {"scale": {"users": users, "tier": "pro"}},
        "resolution": {"fix": {"cause": cause, "owner": owner, "runbook": runbook}},
    }


def harness(model, tmp_path, clearance=1.0):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Harness(model, tmp_path / "files", data=data, home=HERE, clearance=clearance)


def test_incidents_are_recorded_and_the_closest_precedent_answers(model, tmp_path):
    h = harness(model, tmp_path)
    assert h.call("start").data == {"incidents": 0}
    past = [
        ("upstream payment gateway timed out after 30s", "raise circuit breaker"),
        ("payment gateway timeout on retry", "raise circuit breaker"),
        ("gateway 504 while charging card", "raise circuit breaker"),
    ]
    for i, (message, fix) in enumerate(past):
        r = harness(model, tmp_path).call("record", {"data": incident(i, message), "outcome": fix})
        assert r.status == "done" and r.data["incidents"] == i + 1
    r = harness(model, tmp_path).call(
        "record",
        {
            "data": incident(
                9, "cannot open file /var/lib/export/report.csv", "worker", "/jobs/export", "OSError", 500, 3
            ),
            "outcome": "free disk",
        },
    )
    assert r.data["incidents"] == 4
    new = incident(41, "payment gateway timed out charging card")
    q = harness(model, tmp_path).call("query", {"data": new})
    assert q.status == "done" and q.data["sufficient"] and q.data["top"] == "raise circuit breaker"
    facts = harness(model, tmp_path).call("facts", {"data": new, "top": 2}).data
    assert len(facts) == 2 and facts[0]["tree"]
    probs = harness(model, tmp_path).call("probabilities", {"data": new}).data
    assert set(probs) <= {"raise circuit breaker", "free disk"} and abs(sum(probs.values()) - 1) < 1e-6


def test_decide_gives_the_fix_and_last_times_resolution_or_asks_and_records(model, tmp_path):
    for i in range(3):
        harness(model, tmp_path).call(
            "record", {"data": incident(i, "payment gateway timed out"), "outcome": "raise circuit breaker"}
        )
    sure = harness(model, tmp_path).call("decide", {"data": incident(7, "payment gateway timed out again")})
    assert sure.status == "done" and sure.data["fix"] == "raise circuit breaker" and sure.data["from"] == 3
    assert sure.data["last_time"] == {
        "cause": "gateway rate limit hit on retry storm",
        "owner": "payments",
        "runbook": "RB-12",
    }
    h = harness(model, tmp_path)
    asked = h.call(
        "decide",
        {
            "data": incident(
                8,
                "certificate expired",
                "edge",
                "/",
                "SSLError",
                495,
                50000,
                "cert",
                "infra",
                "RB-3",
                "2020-01-01T00:00:00Z",
            )
        },
    )
    assert asked.status == "needs_input" and "How was this one fixed" in asked.prompt
    answered = h.resume("rotate certificate")
    assert answered.status == "done" and answered.data["fix"] == "rotate certificate" and answered.data["recorded"]
    assert answered.data["last_time"] == {}
    assert harness(model, tmp_path).call("start").data == {"incidents": 4}


def test_a_low_clearance_gets_the_fix_but_never_the_cause_owner_or_runbook(model, tmp_path):
    for i in range(3):
        harness(model, tmp_path).call(
            "record", {"data": incident(i, "payment gateway timed out"), "outcome": "raise circuit breaker"}
        )
    new = incident(5, "payment gateway timed out")
    full = harness(model, tmp_path, clearance=1.0).call("facts", {"data": new}).data
    low = harness(model, tmp_path, clearance=0.5).call("facts", {"data": new}).data
    assert '"resolution"' in json.dumps(full[0]["tree"]) and '"resolution"' not in json.dumps(low[0]["tree"])
    bot = harness(model, tmp_path, clearance=0.5).call("decide", {"data": new})
    assert bot.status == "done" and bot.data["fix"] == "raise circuit breaker" and bot.data["last_time"] == {}


def test_learn_weighs_the_roles_from_the_store_and_needs_ten_graded_incidents(model, tmp_path):
    too_few = harness(model, tmp_path).call("learn")
    assert too_few.status == "failed" and "ten precedents" in too_few.data["reason"]
    for i in range(12):
        disk = i % 2 == 0
        harness(model, tmp_path).call(
            "record",
            {
                "data": incident(
                    i,
                    "cannot open file /var/lib/export/report.csv" if disk else "payment gateway timed out",
                    "worker" if disk else "api",
                    "/jobs/export" if disk else "/v1/checkout",
                    "OSError" if disk else "TimeoutError",
                    500 if disk else 504,
                ),
                "outcome": "free disk" if disk else "raise circuit breaker",
            },
        )
    r = harness(model, tmp_path).call("learn", {"sample": 12})
    assert r.status == "done" and r.data["precedents"] == 12 and 0 < r.data["tau"] <= 1
    assert r.data["roles"]["error.where.service"] == 1.0 and r.data["roles"]["error.where.region"] == 0.0
    assert r.data["interactions"] == []  # nothing a pair tells that service alone does not
    q = harness(model, tmp_path).call("query", {"data": incident(50, "gateway timed out", "api")})
    assert q.data["top"] == "raise circuit breaker"

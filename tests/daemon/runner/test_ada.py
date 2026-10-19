"""An Ada model end to end on the runner: the part installed and licensed, the store kept in the data folder across
calls and versions, clearance carried to the model, and rrotor's own re-encode in the sandbox (`maintain`)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from rrotor import ada
from rrotord.licence import Licence
from rrotord.models import Models
from rrotord.parts import Parts
from tests.daemon.runner.test_engine import Env
from tests.fakes import KERNEL, LocalLauncher, ada_part, install, keys, manifest, outcome

HERE = Path(__file__).parent.parent.parent.parent / "examples" / "ada_tickets"


def ticket(i: int, title: str, area: str, severity: int) -> dict[str, Any]:
    return {
        "issue": {"body": {"title": title, "area": area, "severity": severity, "id": i}},
        "meta": {"time": {"opened": "2026-10-01T12:00:00Z"}, "owner": {"team": "infra"}},
    }


def ada_ready(root: Path, sock: Path) -> tuple[Env, Parts]:
    store = Parts(root, keys().public, "arm64")
    data, sha, sig = ada_part(root)
    store.install("ada", "3.9.0", data, sha, sig)
    lic = Licence(root)
    lic.offer(keys().key(), "test")
    env = Env(root, sock)
    env.runner.parts, env.runner.licence = store, lic
    return env, store


def tickets_version(models: Models, sha: str = "a" * 40, version: int = 1, seed: int = 42):
    data = json.loads((HERE / "glyphh.json").read_text())
    made = install(models, data, (HERE / "model.py").read_text(), sha=sha, version=version)
    spec = json.loads((HERE / "spec.json").read_text())
    (made.src / "spec.json").write_text(json.dumps({**spec, "seed": seed}))
    return made


async def test_the_tickets_model_records_queries_and_keeps_its_store_with_clearance(tmp_path, short_dir):
    env, _ = ada_ready(tmp_path, short_dir)
    tickets_version(env.models)
    for i, said in enumerate(["page oncall", "page oncall", "page oncall"]):
        run = await env.runner.call(
            model="tickets", function="record", inputs={"data": ticket(i, "login fails", "auth", 8), "outcome": said}
        )
        assert outcome(run)["status"] == "done", run.result
    assert (env.models.data("tickets") / ada.STORE).is_file()
    run = await env.runner.call(model="tickets", function="query", inputs={"data": ticket(9, "login fails", "auth", 8)})
    assert outcome(run)["data"]["top"] == "page oncall" and outcome(run)["data"]["count"] == 3
    full = await env.runner.call(
        model="tickets", function="facts", inputs={"data": ticket(9, "login fails", "auth", 8)}, clearance=1.0
    )
    low = await env.runner.call(
        model="tickets", function="facts", inputs={"data": ticket(9, "login fails", "auth", 8)}, clearance=0.5
    )
    assert '"meta"' in json.dumps(outcome(full)["data"]) and '"meta"' not in json.dumps(outcome(low)["data"])
    assert low.clearance == 0.5 and env.runner.get(low.id).clearance == 0.5
    spec = env.launcher.specs[-1]
    assert spec.mounts.data == env.models.data("tickets") and spec.mounts.parts[0][0] == "ada"


async def test_maintain_reencodes_the_store_in_the_sandbox_and_says_when_it_cannot(tmp_path, short_dir):
    env, store = ada_ready(tmp_path, short_dir)
    v1 = tickets_version(env.models)
    await env.runner.call(
        model="tickets",
        function="record",
        inputs={"data": ticket(1, "login fails", "auth", 8), "outcome": "page oncall"},
    )
    v2 = tickets_version(env.models, sha="b" * 40, version=2, seed=7)
    done = await env.runner.maintain(v2, ada.REENCODE)
    assert done == {"status": "done", "data": {"reencoded": 1}, "step": None, "prompt": None}
    run = await env.runner.call(model="tickets", function="query", inputs={"data": ticket(2, "login fails", "auth", 8)})
    assert outcome(run)["data"]["top"] == "page oncall"
    (env.models.data("tickets") / ada.STORE).write_bytes(b"not a database")
    broken = await env.runner.maintain(v2, ada.REENCODE)
    assert broken["status"] == "failed" and broken["step"] == ada.REENCODE
    store.remove("ada")
    assert (await env.runner.maintain(v1, ada.REENCODE))["data"]["code"] == "E_PART_MISSING"


async def test_maintain_says_when_the_worker_stops_before_it_answers(tmp_path, short_dir, monkeypatch):
    from rrotord.runner import worker as workers

    env, _ = ada_ready(tmp_path, short_dir)
    v1 = tickets_version(env.models)

    async def gone(self, timeout):
        return None

    monkeypatch.setattr(workers.Worker, "next", gone)
    done = await env.runner.maintain(v1, ada.REENCODE)
    assert done["data"]["code"] == "E_WORKER" and done["step"] == ada.REENCODE


async def test_maintain_without_a_worker_or_a_sandbox_fails_plainly(tmp_path, short_dir):
    env, _ = ada_ready(tmp_path, short_dir)
    v1 = tickets_version(env.models)
    env.launcher = LocalLauncher(fail=OSError("no bwrap"))
    env.runner = env.make()
    env.runner.parts, env.runner.licence = ada_ready(tmp_path / "again", short_dir / "s2")[1], env.runner.licence
    assert (await env.runner.maintain(v1, ada.REENCODE))["data"]["code"] in ("E_SANDBOX", "E_PART_MISSING")


async def test_the_internal_operation_is_never_a_models_function(tmp_path, short_dir):
    env, _ = ada_ready(tmp_path, short_dir)
    tickets_version(env.models)
    plain = Env(tmp_path / "plain", short_dir / "p")
    install(
        plain.models,
        manifest("sample"),
        "from rrotor import Result\ndef start(ctx, inputs):\n    return Result.done(ctx.clearance)\n",
    )
    run = await plain.runner.call(model="sample", function="start", inputs={}, clearance=0.25)
    assert outcome(run)["data"] == 0.25


async def test_the_keys_precedent_cap_reaches_the_model(tmp_path, short_dir):
    env, _ = ada_ready(tmp_path, short_dir)
    lic = env.runner.licence
    assert lic is not None
    lic.offer(keys().key(max_precedents=1, issued_at="2026-10-02T00:00:00Z"), "test")
    tickets_version(env.models)
    one = {"data": ticket(1, "login fails", "auth", 8), "outcome": "page oncall"}
    two = {"data": ticket(2, "login slow", "auth", 5), "outcome": "page oncall"}
    first = await env.runner.call(model="tickets", function="record", inputs=one)
    assert outcome(first)["status"] == "done"
    second = await env.runner.call(model="tickets", function="record", inputs=two)
    assert second.status == "done" and outcome(second)["data"] == {
        "status": "limit_exceeded",
        "indexed": False,
        "message": "Precedent store capacity limit reached (1 max for the Pro plan). "
        "Upgrade the key to index further precedents.",
        "precedents": 1,
        "cap": 1,
    }
    v1 = env.models.active("tickets")
    assert v1 is not None
    assert env.runner._limits(v1) == {"precedents": 1, "plan": "Pro", "precedents_elsewhere": 0}
    lic.offer(keys().key(tier="enterprise", max_precedents=None, issued_at="2026-10-03T00:00:00Z"), "test")
    assert env.runner._limits(v1) == {"precedents": None, "plan": "Enterprise"}  # unlimited: nothing to count
    env.runner.licence = None
    assert env.runner._limits(v1) == {"precedents": 5000, "plan": "Community", "precedents_elsewhere": 0}


async def test_an_ada_model_runs_with_no_key_at_all_under_communitys_cap(tmp_path, short_dir):
    env, _ = ada_ready(tmp_path, short_dir)
    env.runner.licence = Licence(tmp_path / "none")
    tickets_version(env.models)
    run = await env.runner.call(
        model="tickets", function="record", inputs={"data": ticket(1, "login fails", "auth", 8), "outcome": "x"}
    )
    assert outcome(run)["status"] == "done"
    v1 = env.models.active("tickets")
    assert v1 is not None
    assert env.runner._limits(v1) == {"precedents": 5000, "plan": "Community", "precedents_elsewhere": 0}


async def test_the_cap_is_on_the_runtimes_total_across_its_ada_models(tmp_path, short_dir, caplog):
    env, _ = ada_ready(tmp_path, short_dir)
    lic = env.runner.licence
    assert lic is not None
    lic.offer(keys().key(max_precedents=2, issued_at="2026-10-02T00:00:00Z"), "test")
    tickets_version(env.models)
    data = json.loads((HERE / "glyphh.json").read_text())
    other = install(env.models, {**data, "name": "triage"}, (HERE / "model.py").read_text(), sha="b" * 40)
    (other.src / "spec.json").write_text((HERE / "spec.json").read_text())
    one = {"data": ticket(1, "login fails", "auth", 8), "outcome": "page oncall"}
    two = {"data": ticket(2, "login slow", "auth", 5), "outcome": "page oncall"}
    three = {"data": ticket(3, "disk full", "infra", 9), "outcome": "page oncall"}
    assert outcome(await env.runner.call(model="tickets", function="record", inputs=one))["status"] == "done"
    assert outcome(await env.runner.call(model="triage", function="record", inputs=two))["status"] == "done"
    refused = outcome(await env.runner.call(model="tickets", function="record", inputs=three))
    assert refused["status"] == "done" and refused["data"]["status"] == "limit_exceeded"
    assert (refused["data"]["precedents"], refused["data"]["cap"]) == (2, 2)
    v1 = env.models.active("tickets")
    assert v1 is not None and env.runner._limits(v1)["precedents_elsewhere"] == 1
    (env.models.data("triage") / ada.STORE).write_bytes(b"not a database")
    assert env.runner._limits(v1)["precedents_elsewhere"] == 0 and "a precedent store did not read" in caplog.text
    plain = install(env.models, manifest("sample"), "def start(ctx, inputs):\n    return None\n", sha="c" * 40)
    assert env.runner._limits(plain) == {}  # not an Ada model


async def test_two_models_at_six_each_under_a_cap_of_ten_the_seconds_run_is_refused_at_its_first_record(
    tmp_path, short_dir
):
    env, _ = ada_ready(tmp_path, short_dir)
    lic = env.runner.licence
    assert lic is not None
    lic.offer(keys().key(tier="enterprise", max_precedents=None, issued_at="2026-10-02T00:00:00Z"), "test")
    tickets_version(env.models)
    data = json.loads((HERE / "glyphh.json").read_text())
    other = install(env.models, {**data, "name": "triage"}, (HERE / "model.py").read_text(), sha="b" * 40)
    (other.src / "spec.json").write_text((HERE / "spec.json").read_text())
    sys.path.insert(0, str(KERNEL))
    try:
        import glyphh_spec  # type: ignore[import-not-found]

        spec = glyphh_spec.Spec.from_json(json.loads((HERE / "spec.json").read_text()))
        for model in ("tickets", "triage"):  # six each, recorded while the plan was unlimited
            held = glyphh_spec.Model(spec, path=str(env.models.data(model) / ada.STORE))
            for i in range(6):
                held.record(ticket(i, f"case {i}", "auth", 3), "x")
            held.space.close()
    finally:
        sys.path.remove(str(KERNEL))
        for name in ("glyphh_kernel", "glyphh_spec", "glyphh_nsm", "glyphh_lexicon"):
            sys.modules.pop(name, None)
    lic.offer(keys().key(tier="team", max_precedents=10, issued_at="2026-10-03T00:00:00Z"), "test")  # a downgrade
    inputs = {"data": ticket(99, "new", "auth", 3), "outcome": "x"}
    run = await env.runner.call(model="triage", function="record", inputs=inputs)
    assert run.status == "done" and outcome(run)["data"]["status"] == "limit_exceeded"
    assert (outcome(run)["data"]["precedents"], outcome(run)["data"]["cap"]) == (12, 10)
    assert "10 max for the Team plan" in outcome(run)["data"]["message"]
    query = await env.runner.call(model="triage", function="query", inputs={"data": ticket(1, "case 1", "auth", 3)})
    assert outcome(query)["status"] == "done"  # over the cap: reads answer, nothing deleted
    assert ada.count(env.models.data("triage") / ada.STORE) == 6

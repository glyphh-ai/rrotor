"""An Ada model end to end on the runner: the part installed and licensed, the store kept in the data folder across
calls and versions, clearance carried to the model, and rrotor's own re-encode in the sandbox (`maintain`)."""

from __future__ import annotations

import json
from pathlib import Path

from rrotor import ada
from rrotord.licence import Licence
from rrotord.models import Models
from rrotord.parts import Parts
from tests.daemon.runner.test_engine import Env
from tests.fakes import LocalLauncher, ada_part, install, keys, manifest

HERE = Path(__file__).parent.parent.parent.parent / "examples" / "ada_tickets"


def ticket(i, title, area, severity):
    return {
        "issue": {"body": {"title": title, "area": area, "severity": severity, "id": i}},
        "meta": {"time": {"opened": "2026-10-01T12:00:00Z"}, "owner": {"team": "infra"}},
    }


def ada_ready(root: Path, sock: Path) -> tuple[Env, Parts]:
    store = Parts(root, keys().public, "arm64")
    data, sha, sig = ada_part(root)
    store.install("ada", "3.6.0", data, sha, sig)
    lic = Licence(root, keys().public)
    given = keys().licence(["ada"])
    lic.set(given["payload"], given["signature"])
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
    for i, outcome in enumerate(["page oncall", "page oncall", "page oncall"]):
        run = await env.runner.call(
            model="tickets", function="record", inputs={"data": ticket(i, "login fails", "auth", 8), "outcome": outcome}
        )
        assert run.result["status"] == "done", run.result
    assert (env.models.data("tickets") / ada.STORE).is_file()
    run = await env.runner.call(model="tickets", function="query", inputs={"data": ticket(9, "login fails", "auth", 8)})
    assert run.result["data"]["top"] == "page oncall" and run.result["data"]["count"] == 3
    full = await env.runner.call(
        model="tickets", function="facts", inputs={"data": ticket(9, "login fails", "auth", 8)}, clearance=1.0
    )
    low = await env.runner.call(
        model="tickets", function="facts", inputs={"data": ticket(9, "login fails", "auth", 8)}, clearance=0.5
    )
    assert '"meta"' in json.dumps(full.result["data"]) and '"meta"' not in json.dumps(low.result["data"])
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
    assert run.result["data"]["top"] == "page oncall"
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
    assert run.result["data"] == 0.25


async def test_the_licences_limits_reach_the_model(tmp_path, short_dir):
    from rrotord.keys import canonical

    env, store = ada_ready(tmp_path, short_dir)
    given = keys().licence(["ada"])
    given["payload"]["mods"]["ada"] = {"limits": {"models": 3, "precedents": 1}}
    given["signature"] = keys().sign(canonical(given["payload"]))
    env.runner.licence.set(given["payload"], given["signature"])
    tickets_version(env.models)
    one = {"data": ticket(1, "login fails", "auth", 8), "outcome": "page oncall"}
    two = {"data": ticket(2, "login slow", "auth", 5), "outcome": "page oncall"}
    first = await env.runner.call(model="tickets", function="record", inputs=one)
    assert first.result["status"] == "done"
    second = await env.runner.call(model="tickets", function="record", inputs=two)
    assert second.result["status"] == "failed" and second.result["data"]["code"] == "E_LIMIT_PRECEDENTS"
    v1 = env.models.active("tickets")
    assert env.runner._limits(v1) == {"models": 3, "precedents": 1}
    env.runner.licence.payload = None
    assert env.runner._limits(v1) == {}
    env.runner.licence.payload = {**given["payload"], "mods": {"ada": {"limits": "junk"}}}
    assert env.runner._limits(v1) == {}
    store.remove("ada")
    assert env.runner._limits(v1) == {}

"""Secrets and schedules in a cluster: node-local, kept by the head. A follower refuses to set a secret and asks the
head for the list, every call of a program that declares secrets runs on the head, whose vault holds them, and only
the head fires schedules."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.daemon.cluster.conftest import Nodes, until
from tests.daemon.cluster.ring import Ring
from tests.fakes import install, manifest

READS = "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.done(ctx.secrets['API_KEY'][:3])\n"


@pytest.fixture
async def ring(nodes: Nodes) -> AsyncIterator[Ring]:
    made = Ring(nodes)
    yield made
    await made.close()


async def test_secrets_stay_on_the_head_and_calls_that_need_them_run_there(ring: Ring):
    ids = ["n0", "n1"]
    head = await ring.add("n0", ids)
    await until(lambda: head.cluster.role == "head")
    functions = {"start": {"writes": False}}
    install(head.runner.programs, manifest("keyed", secrets=["API_KEY"], functions=functions), READS)
    one = await ring.add("n1", ids)
    await until(lambda: one.cluster.ready()[0] and one.runner.programs.active("keyed") is not None)
    path = "/v1/programs/keyed/secrets/API_KEY"
    res = await ring.clients["n1"].put(path, json={"value": "sk-follower"}, headers=ring.headers())
    body = await res.json()
    assert (res.status, body["error"]["code"], body["error"]["head"]) == (503, "E_NOT_HEAD", "n0")
    res = await ring.clients["n0"].put(path, json={"value": "sk-head"}, headers=ring.headers())
    assert res.status == 200
    status, listed = await ring.get("n1", "/v1/programs/keyed/secrets")
    assert (status, listed["secrets"]) == (200, [{"name": "API_KEY", "declared": True, "set": True}])
    assert one.runner.vault is not None and one.runner.vault.names("keyed") == []  # never replicated
    status, run, _ = await ring.call("n1", "start", program="keyed")
    assert (run["status"], run["data"]) == ("done", "sk-") and run["run_id"].startswith("n0~"), run


async def test_only_the_head_fires_schedules_and_a_follower_lists_the_heads(ring: Ring):
    ids = ["n0", "n1"]
    head = await ring.add("n0", ids)
    await until(lambda: head.cluster.role == "head")
    schedules = [{"function": "start", "cron": "* * * * *"}]
    install(head.runner.programs, manifest("ticks", schedules=schedules), READS.replace("ctx.secrets['API_KEY']", "''"))
    one = await ring.add("n1", ids)
    await until(lambda: one.cluster.ready()[0] and one.runner.programs.active("ticks") is not None)
    assert head.scheduler.leading() and not one.scheduler.leading()
    assert await one.scheduler.tick() == [] and one.scheduler.due == {}
    head.scheduler.pause("ticks", "start", True)
    status, listed = await ring.get("n1", "/v1/schedules")
    assert (status, listed["schedules"][0]["id"], listed["schedules"][0]["paused"]) == (200, "ticks/start", True)
    res = await ring.clients["n1"].post("/v1/schedules/ticks/start/resume", headers=ring.headers())
    assert (res.status, (await res.json())["error"]["code"]) == (503, "E_NOT_HEAD")

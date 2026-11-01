"""A CLI in a cluster: its install is the head's build and its home the head's, so a follower sends every command to
the head, which runs it."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.daemon.cluster.conftest import Nodes, until
from tests.daemon.cluster.ring import Ring
from tests.fakes import ECHO, FakeFetch, cli_manifest, install_cli, sha, tarball

URL = "https://example.com/tool.tar.gz"


@pytest.fixture
async def ring(nodes: Nodes) -> AsyncIterator[Ring]:
    made = Ring(nodes)
    yield made
    await made.close()


async def test_a_followers_cli_command_runs_on_the_head(ring: Ring):
    ids = ["n0", "n1"]
    head = await ring.add("n0", ids)
    await until(lambda: head.cluster.role == "head")
    archive = tarball({"bin/tool": ECHO}, {"bin/tool": 0o755})
    block = {"install": {"url": URL, "sha256": sha(archive)}}
    await install_cli(head.runner.programs, cli_manifest(cli=block), FakeFetch({URL: archive}))
    one = await ring.add("n1", ids)
    await until(lambda: one.cluster.ready()[0] and one.runner.programs.active("tool") is not None)
    res = await ring.clients["n1"].post("/v1/clis/tool/run", json={"args": ["hi"]}, headers=ring.headers())
    body = await res.json()
    assert res.status == 200, body
    assert (body["status"], body["data"]["exit_code"]) == ("done", 3) and body["run_id"].startswith("n0~")
    assert body["data"]["stdout"].startswith("args: hi\n")

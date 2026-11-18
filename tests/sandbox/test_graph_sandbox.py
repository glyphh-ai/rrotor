"""ctx.graph from inside the real sandbox: a pipeline writes the rrotor's graph and reads it back over its run's own
channel, and the graph's file itself is out of its reach.

Linux only, inside the image, with /data writable: `pytest -m sandbox -o addopts='' tests/sandbox`."""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import rrotor
from rrotord.deploy import Deployer
from rrotord.graph import Graph
from rrotord.programs import Programs
from rrotord.proxy import EgressProxy
from rrotord.runner.engine import Runner
from rrotord.runner.launch import SandboxLauncher
from rrotord.runner.record import Records
from rrotord.sandbox.probe import probe
from tests.fakes import GitRepo, manifest

pytestmark = pytest.mark.sandbox

PIPELINE = """
from rrotor import Result

def start(ctx, inputs):
    wrote = ctx.graph.rows(
        [{"id": "d1", "name": "Renewal 2027", "notes": "The renewal closed in March.", "account": "acme"}],
        type="deal", key="id", title="name", text=["notes"], links={"account": ("belongs_to", "account")},
    )
    hits = ctx.graph.search("renewal", expand=True)
    try:
        open(inputs["file"], "rb").close()
        reached = True
    except OSError:
        reached = False
    return Result.done({"wrote": wrote, "hit": hits[0]["key"], "linked": hits[0]["out"][0]["key"], "file": reached})
"""


@pytest.fixture
async def runner() -> AsyncIterator[Runner]:
    layers = await probe()
    assert layers.ready, layers.why()
    root = Path(tempfile.mkdtemp(prefix="sandbox-graph-", dir="/data"))
    programs = Programs(root)
    repo = GitRepo(Path(tempfile.mkdtemp(prefix="rr-repo-")) / "pipeline")
    sha = repo.program(manifest("pipeline", graph="write"), PIPELINE)
    await Deployer(programs, Path(tempfile.mkdtemp(prefix="rr-deploy-"))).from_bundle(
        repo.bundle(repo.path.parent / "pipeline.bundle"), sha
    )
    made = Runner(
        programs=programs,
        records=Records(root),
        proxy=EgressProxy(Path(tempfile.mkdtemp(prefix="rr-proxy-", dir="/tmp"))),
        launcher=SandboxLauncher(),
        sdk=Path(rrotor.__file__).parent,
        layers=layers,
        graph=Graph(root),
    )
    yield made
    await made.shutdown()


async def test_a_sandboxed_pipeline_writes_and_reads_the_graph_but_never_its_file(runner):
    assert runner.graph is not None
    run = await runner.call(program="pipeline", function="start", inputs={"file": str(runner.graph.path)})
    assert run.status == "done", run.result
    assert run.result["data"] == {"wrote": {"nodes": 1, "edges": 1}, "hit": "d1", "linked": "acme", "file": False}
    assert runner.graph.path.is_file() and runner.graph.node("deal", "d1")["source"] == "pipeline"

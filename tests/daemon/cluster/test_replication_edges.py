"""Replication's edges, one node's side at a time: entries a follower skips, refuses or cannot apply, a version
fetched from a head of another architecture, a snapshot that is not one, a shipper whose follower stops answering,
forwarding without a caller or a head, and what a forwarded request may ask."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

from rrotord.access import Access
from rrotord.api import forward
from rrotord.cluster import replication as rep
from rrotord.cluster.replication import Replication
from rrotord.cluster.transport import ClusterError
from rrotord.errors import BadRequest, RrotorError
from rrotord.licence import Licence
from rrotord.models import Models
from tests.daemon.cluster.conftest import Nodes, until
from tests.daemon.cluster.ring import Ring, tickets
from tests.fakes import install, manifest


def follower(tmp_path: Path, role: str = "follower") -> Replication:
    saved: list[int] = []
    r = Replication(
        tmp_path / "log",
        tmp_path / "scratch",
        1 << 20,
        Models(tmp_path),
        Access(tmp_path),
        Licence(tmp_path),
        role=lambda: role,
        term=lambda: 1,
        saved=saved.append,
    )
    return r


def entry(seq: int, kind: str, **body: Any) -> dict[str, Any]:
    return {"seq": seq, "term": 1, "kind": kind, **body}


async def test_a_follower_skips_what_it_had_and_stops_at_what_does_not_apply(tmp_path):
    r = follower(tmp_path)
    sent = [
        "not an entry",
        {"seq": "x"},
        entry(1, "keys", hashes=["a" * 64]),
        entry(2, "licence", key=None),
        entry(3, "remove", model="gone", commit_sha="b" * 40),
    ]
    assert await r.receive({"entries": sent, "commit_seq": 3}) == 3
    assert r.access.keys == ["a" * 64] and r.current()
    assert await r.receive({"entries": [entry(2, "keys", hashes=[])], "commit_seq": 3}) == 3  # had already
    assert await r.receive({"entries": [entry(4, "gateway")], "commit_seq": 4}) == 3
    assert r.failure is not None and r.failure["code"] == "E_CLUSTER_KIND" and not r.current()
    # the next append tries it again (it is in the log already) and goes on once it applies
    assert await r.receive({"entries": [entry(4, "keys", hashes=["c" * 64])], "commit_seq": 5}) == 4
    assert r.failure is None and r.behind_since is not None and r.current()  # behind, but not for long
    since = r.behind_since
    assert await r.receive({"entries": [], "commit_seq": 6}) == 4 and r.behind_since == since
    r.wall = lambda: 10**10
    assert not r.current()
    assert await r.receive({"entries": [entry(5, "rows", model="m", rows=[{"tbl": "x"}])], "commit_seq": 5}) == 4
    assert r.failure is not None and r.failure["code"] == "E_CLUSTER_APPLY"


async def test_a_version_is_fetched_from_the_head_of_this_architecture_only(tmp_path):
    r = follower(tmp_path)
    found = manifest("m")
    deploy = entry(1, "deploy", model="m", version=2, commit_sha="a" * 40, manifest=found)
    assert await r.receive({"entries": [deploy], "commit_seq": 1}) == 0
    assert r.failure is not None and r.failure["code"] == "E_NO_HEAD"

    async def foreign(path: str, dest: Path) -> dict[str, str]:
        dest.write_bytes(b"")
        return {"Rrotor-Arch": "riscv64"}

    r.fetch = foreign
    assert await r.receive({"entries": [deploy], "commit_seq": 1}) == 0
    assert r.failure is not None and r.failure["code"] == "E_CLUSTER_ARCH"
    install(r.models, found, "def start(ctx, inputs): pass\n", sha="a" * 40)  # held already: nothing fetched
    assert await r.receive({"entries": [deploy], "commit_seq": 1}) == 1
    active = r.models.active("m")
    assert active is not None and active.version == 2


def tar_of(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


async def test_a_snapshot_that_is_not_one_is_refused_and_one_that_is_replaces_what_the_follower_had(tmp_path):
    r = follower(tmp_path)
    install(r.models, manifest("old"), "def start(ctx, inputs): pass\n", sha="c" * 40)
    sent: dict[str, bytes] = {}

    async def fetch(path: str, dest: Path) -> dict[str, str]:
        dest.write_bytes(sent["tar"])
        return {}

    r.fetch = fetch
    sent["tar"] = tar_of({"other": b"x"})
    with pytest.raises(RrotorError) as caught:
        await r.install()
    assert caught.value.code == "E_CLUSTER_APPLY" and r.failure is not None
    about = {"seq": 7, "arch": r.arch, "keys": [], "licence": None, "models": {"old": {"active": None, "versions": []}}}
    sent["tar"] = tar_of({"snapshot.json": json.dumps(about).encode()})
    assert await r.install() == 7
    assert r.models.versions("old") == [] and r.log.last == 7 and r.current()


async def test_the_heads_capture_and_shippers_when_things_go_wrong(tmp_path, caplog):
    r = follower(tmp_path, role="head")
    install(r.models, manifest("m", needs=["ada"], data=True), "", sha="a" * 40)
    store = r.models.data("m") / "precedents.sqlite"
    store.parent.mkdir(parents=True)
    store.write_text("not a database")
    await r.capture("m")
    assert "were not captured" in caplog.text
    r.heard_from("n1", "x")
    r.trim([])
    with pytest.raises(RrotorError) as caught:
        r.version_tar("m", "f" * 40)
    assert caught.value.status == 404

    class Channel:
        def __init__(self, *answers: Any) -> None:
            self.answers = list(answers)

        async def request(self, kind: str, /, **_: Any) -> dict[str, Any]:
            got = self.answers.pop(0)
            if isinstance(got, BaseException):
                raise got
            return dict(got)

    rep_idle = rep.IDLE
    rep.IDLE = 0.01
    try:
        refused = ClusterError("no", code="E_CLUSTER_ARCH")
        closed = ClusterError("closed", code="E_CLUSTER_CLOSED")
        r.attach("n1", Channel(refused, closed), None)  # type: ignore[arg-type]
        await until(lambda: r.shippers["n1"].done())
        assert "did not take a snapshot" in caplog.text and "shipping to a follower stopped" in caplog.text
        r.acked["n2"] = 0
        r.attach("n2", Channel(TimeoutError()), 5)  # type: ignore[arg-type]
        await until(lambda: r.shippers["n2"].done())
        assert "did not answer" in caplog.text
    finally:
        rep.IDLE = rep_idle
    r.lead()
    ticker = r.ticker
    r.lead()
    assert r.ticker is ticker
    await r.stop()


async def test_forwarding_without_a_caller_a_head_or_a_server_and_what_may_be_asked(nodes: Nodes):
    ring = Ring(nodes)
    try:
        head = await ring.add("n0", ["n0", "n1"])
        await until(lambda: head.cluster.role == "head")
        tickets(head)
        one = await ring.add("n1", ["n0", "n1"])
        await until(lambda: one.cluster.ready()[0])
        got = await one.cluster.forward("approvals", {})  # no caller in hand: the head does it as rrotor
        assert got["result"] == {"approvals": []} and isinstance(got["seq"], int)
        assert (await one.cluster.forward("approvals_here", {}, "n1"))["result"] == {"approvals": []}
        # a run whose id names no node: the head's, unless this node holds it
        status, body = await ring.get("n1", "/v1/runs/00000000-0000-4000-8000-000000000000")
        assert (status, body["error"]["code"]) == (404, "E_RUN_MISSING")
        status, body = await ring.post("n1", "/v1/runs", {"model": "nothing", "inputs": {}})
        assert (status, body["error"]["code"]) == (404, "E_MODEL_MISSING")  # not here: the head says so
        with pytest.raises(BadRequest):
            await one.cluster._local({"op": "call", "args": "x"})
        with pytest.raises(BadRequest, match="no forwarded operation"):
            await one.cluster._local({"op": "nothing"})
        tmp = one.config.run_dir / "x.tar"
        with pytest.raises(ClusterError) as caught:
            await one.cluster._fetch(f"/cluster/v1/versions/tickets/{'f' * 40}.tar", tmp)
        assert (caught.value.code, caught.value.status) == ("E_VERSION_MISSING", 404) and not tmp.exists()
        one.cluster.serve_forward = None
        with pytest.raises(RrotorError, match="does not serve"):
            await one.cluster._local({"op": "approvals"})
        one.cluster.state.pop("head_address")
        with pytest.raises(ClusterError, match="No head"):
            await one.cluster._fetch("/cluster/v1/snapshot", tmp)

        # the head gathers approvals without a follower that does not answer
        async def refuse(*_: Any, **__: Any) -> dict[str, Any]:
            raise RrotorError("gone", code="E_RUN_LOST")

        head.cluster.forward = refuse  # type: ignore[method-assign]
        status, body = await ring.get("n0", "/v1/approvals")
        assert (status, body) == (200, {"approvals": []})
        # a follower reports no usage of its own: its calls go with its pings
        one.access.usage.count("a" * 64)
        await one.report_usage()
        assert one.access.usage.counts != {}
        assert forward.follower(ring.daemons["n1"].services())
    finally:
        await ring.close()

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
        saved=saved.append,
        me="f" if role == "follower" else "h",
    )
    if role == "head":
        r.raft.bootstrap({"node_id": "h"}, 0.0)
    return r


def entry(seq: int, kind: str, **body: Any) -> dict[str, Any]:
    return {"seq": seq, "term": 1, "kind": kind, **body}


def append(prev: int, entries: list[dict[str, Any]], commit: int, term: int = 1) -> dict[str, Any]:
    return {
        "term": term,
        "head": "h",
        "prev_seq": prev,
        "prev_term": 1 if prev else 0,
        "entries": entries,
        "commit_seq": commit,
    }


async def applied(r: Replication, msg: dict[str, Any]) -> int:
    return int((await r.receive(msg))["applied_seq"])


async def test_a_follower_applies_what_is_committed_and_stops_at_what_does_not_apply(tmp_path):
    r = follower(tmp_path)
    told: list[dict[str, Any]] = []
    r.member = told.append
    sent = [
        entry(1, "keys", hashes=["a" * 64]),
        entry(2, "licence", key=None),
        entry(3, "remove", model="gone", commit_sha="b" * 40),
        entry(4, "head", node="h"),
        entry(5, "member", op="add", node="f", members=[{"node_id": "h"}, {"node_id": "f"}]),
    ]
    assert await applied(r, append(0, sent, 3)) == 3  # what is committed, and no further
    assert r.access.keys == ["a" * 64] and r.raft.ids() == ["h", "f"] and told == []  # members: when held
    assert await applied(r, append(5, [], 5)) == 5 and told[0]["node"] == "f" and r.current()
    assert await applied(r, append(1, [entry(2, "keys", hashes=[])], 5)) == 5  # held already
    stale = await r.receive(append(5, [], 5, term=0))  # a lower term's head: refused with this node's term
    assert (stale["accepted"], stale["term"]) == (False, 1)
    assert await applied(r, append(5, [entry(6, "gateway")], 6)) == 5
    assert r.failure is not None and r.failure["code"] == "E_CLUSTER_KIND" and not r.current()
    assert await applied(r, append(6, [], 6)) == 5  # tried again with every append: an upgrade lets it apply


async def test_a_follower_behind_its_head_for_long_is_not_ready(tmp_path):
    r = follower(tmp_path)
    assert await applied(r, append(0, [entry(1, "keys", hashes=[])], 1)) == 1 and r.current()
    assert await applied(r, append(1, [], 9)) == 1  # the head has committed more than it sent yet
    assert r.behind_since is not None and r.current()  # behind, but not for long
    since = r.behind_since
    assert await applied(r, append(1, [], 9)) == 1 and r.behind_since == since
    r.wall = lambda: 10**10
    assert not r.current()
    assert await applied(r, append(1, [entry(2, "rows", model="m", rows=[{"tbl": "x"}])], 2)) == 1
    assert r.failure is not None and r.failure["code"] == "E_CLUSTER_APPLY"


async def test_a_version_is_fetched_from_the_head_of_this_architecture_only(tmp_path):
    r = follower(tmp_path)
    found = manifest("m")
    deploy = entry(1, "deploy", model="m", version=2, commit_sha="a" * 40, manifest=found)
    assert await applied(r, append(0, [deploy], 1)) == 0
    assert r.failure is not None and r.failure["code"] == "E_NO_HEAD"

    async def foreign(path: str, dest: Path) -> dict[str, str]:
        dest.write_bytes(b"")
        return {"Rrotor-Arch": "riscv64"}

    r.fetch = foreign
    assert await applied(r, append(1, [], 1)) == 0
    assert r.failure is not None and r.failure["code"] == "E_CLUSTER_ARCH"
    install(r.models, found, "def start(ctx, inputs): pass\n", sha="a" * 40)  # held already: nothing fetched
    assert await applied(r, append(1, [], 1)) == 1
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
    asked = {"term": 2, "head": "h", "commit_seq": 5}
    sent["tar"] = tar_of({"other": b"x"})
    with pytest.raises(RrotorError) as caught:
        await r.install(asked)
    assert caught.value.code == "E_CLUSTER_APPLY" and r.failure is not None
    about = {
        "seq": 7,
        "seq_term": 2,
        "term": 1,
        "arch": r.arch,
        "keys": [],
        "licence": None,
        "members": [],
        "models": {"old": {"active": None, "versions": []}},
    }
    sent["tar"] = tar_of({"snapshot.json": json.dumps(about).encode()})
    with pytest.raises(RrotorError) as caught:
        await r.install(asked)  # a snapshot of another term than the head that asked
    assert caught.value.code == "E_CLUSTER_TERM"
    sent["tar"] = tar_of({"snapshot.json": json.dumps(about | {"term": 2}).encode()})
    got = await r.install(asked)
    assert (got["accepted"], got["match"], got["applied_seq"]) == (True, 7, 7)
    assert r.models.versions("old") == [] and r.log.last == 7 and r.log.term_at(7) == 2 and r.current()
    assert r.raft.commit == 5  # what the head had committed: the rest of the snapshot is not known committed
    stale = await r.install({"term": 1, "head": "h", "commit_seq": 0})
    assert (stale["accepted"], stale["term"]) == (False, 2)


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


async def test_a_shipper_stops_when_its_head_hears_a_higher_term_and_a_head_that_cannot_apply_its_log_does_not_lead(
    tmp_path, caplog
):
    r = follower(tmp_path, role="head")
    moved: list[str] = []
    r.moved = lambda: moved.append(r.raft.role)

    class Channel:
        async def request(self, kind: str, /, **_: Any) -> dict[str, Any]:
            return {"term": 7, "accepted": False}

    r.raft.add({"node_id": "n1"})
    r.attach("n1", Channel(), 1)  # type: ignore[arg-type]
    await until(lambda: r.shippers["n1"].done())
    assert r.raft.role == "follower" and r.raft.term == 7 and moved == ["follower"]
    other = follower(tmp_path / "other")
    other.log.append({"term": 1, "kind": "gateway"})
    assert not await other.own() and "does not lead" in caplog.text
    await r.stop()

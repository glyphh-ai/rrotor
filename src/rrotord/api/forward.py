"""The API in a cluster (docs/specs/cluster.md, 3): what a follower sends to its head, what goes to the node that holds
a run, and what a forwarded request does where it lands.

- **Writes go to the head:** a call of a function marked `"writes": true` (the default for a model with data), any
  call of a model with data that is not Ada (the head's alone), a deploy. Reads (`"writes": false`) run here, on
  this node's replica.
- **A run lives on the node that ran it** (`<node id>~<uuid>`): its continue, cancel, status and log go there, by
  way of the head (`E_RUN_LOST`, 410, when that node is gone). `GET /v1/approvals` gathers every node's.
- **Commit:** a write done on the head (here, or forwarded to it) is answered once committed: at once without HA
  (on the head's disk), with HA (Enterprise) once a majority of voters hold it (503 `E_NOT_COMMITTED` after 5 s, the
  entry kept); a head deposed meanwhile answers `E_NO_HEAD`, with `Retry-After`.
- **Read-your-writes:** a follower that forwarded a write waits (up to 2 s) until it has applied the seq the head
  answered with before it answers; every answer of a member carries `Rrotor-Cluster-Seq` (the head's last seq, a
  follower's applied one). A caller that moves between nodes may send `Rrotor-Min-Seq: <seq>`: a node behind it
  waits up to 2 s, then sends the call to the head.

The caller travels as data: the head does the request as who the follower's gate let in (its audit events name
them), and a forwarded call is audited where it runs, never twice.

**Traced, with a drain:** the forward is a span (`rrotor.cluster.forward`, under the request's) and so is the wait
for the seq the head answered (`rrotor.cluster.commit_wait`); the forward carries its `traceparent`, so what the head
does is a span of the same trace (`rrotor.cluster.forwarded`)."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from opentelemetry.trace import SpanKind

from rrotord.errors import BadRequest, RrotorError
from rrotord.otel import PROPAGATOR
from rrotord.runner.record import holder

if TYPE_CHECKING:
    from rrotord.api.routes import Services

MIN_SEQ: contextvars.ContextVar[int] = contextvars.ContextVar("rrotor_min_seq", default=0)
SEQ_HEADER, MIN_SEQ_HEADER = "Rrotor-Cluster-Seq", "Rrotor-Min-Seq"


def follower(s: Services) -> bool:
    return s.cluster is not None and s.cluster.role == "follower"


def writes(s: Services, model: str, function: str, version: int | None) -> bool:
    """Whether a call runs on the head: its function writes, or its model is the head's alone. A model or version
    this node does not hold (yet) is the head's to answer for."""
    try:
        found = s.models.get(model, version)
    except RrotorError:
        return True
    return found.manifest.writes(function)


def elsewhere(s: Services, run_id: str) -> tuple[bool, str | None]:
    """Whether a run is held by another node, and which (None: the head, for a run whose id names no node)."""
    c = s.cluster
    if c is None or not c.member:
        return False, None
    node = holder(run_id)
    if node == c.node_id:
        return False, None
    if node is None:
        return (c.role != "head" and s.runner.records.load(run_id) is None), None
    return True, node


def leading(s: Services) -> int | None:
    """The term this node is the cluster's head in, when it is: a write it does is answered once committed."""
    c = s.cluster
    return c.raft.term if c is not None and c.role == "head" else None


async def committed(s: Services, term: int | None) -> None:
    """A write done on the head in `term`: answered once committed (with HA, by a majority of voters), refused
    E_NO_HEAD if the head was deposed meanwhile, E_NOT_COMMITTED if most voters do not hold it within 5 s."""
    if term is not None and s.cluster is not None:
        await s.cluster.committed(term)


@contextlib.contextmanager
def traced(s: Services, name: str, attributes: dict[str, Any], **given: Any) -> Iterator[None]:
    """A span, with a drain; nothing without one."""
    if s.drain is None:
        yield
        return
    with s.drain.telemetry.span(name, attributes, **given):
        yield


async def relayed(s: Services, op: str, args: dict[str, Any], target: str | None = None) -> dict[str, Any]:
    """Done by the head (or the node named), and, for a write, answered once this node has applied it."""
    assert s.cluster is not None  # noqa: S101  (only a member forwards)
    with traced(s, "rrotor.cluster.forward", {"rrotor.op": op, "rrotor.target": target or "head"}):
        carrier: dict[str, str] = {}
        PROPAGATOR.inject(carrier)
        got = await s.cluster.forward(op, {**args, "trace": carrier} if carrier else args, target)
    seq = got.get("seq")
    if isinstance(seq, int):
        with traced(s, "rrotor.cluster.commit_wait", {"rrotor.seq": seq}):
            await s.cluster.caught_up(seq)
    result = got.get("result")
    return result if isinstance(result, dict) else {}


async def serve(s: Services, request: dict[str, Any]) -> dict[str, Any]:
    """A forwarded request, done here as its caller (the cluster set who that is); with a drain, a span of the
    sender's trace."""
    op, args = request.get("op"), request.get("args") or {}
    if not isinstance(args, dict):
        raise BadRequest("A forwarded request's args are an object.")
    carrier = args.pop("trace", None)
    parent = PROPAGATOR.extract(carrier=carrier) if isinstance(carrier, dict) else None
    with traced(s, "rrotor.cluster.forwarded", {"rrotor.op": str(op)}, kind=SpanKind.SERVER, parent=parent):
        return await _serve(s, op, args)


async def _serve(s: Services, op: Any, args: dict[str, Any]) -> dict[str, Any]:
    from rrotord.api import ops, routes

    run_id = str(args.get("run_id") or "")
    match op:
        case "call":
            return await ops.call(s, dict(args.get("data") or {}), str(args.get("caller") or "claude"))
        case "deploy":
            return await ops.deploy(s, dict(args.get("data") or {}))
        case "resume":
            return await ops.resume(s, run_id, args.get("answer"))
        case "cancel":
            return await ops.cancel(s, run_id)
        case "status":
            return await ops.run_status(s, run_id)
        case "log":
            return await ops.run_log(s, run_id)
        case "approvals":
            return {"approvals": await ops.approvals(s)}
        case "approvals_here":
            return {"approvals": s.runner.approvals()}
        case "audit":
            return await routes.audited(s, args)
    raise BadRequest(f"There is no forwarded operation {op!r}.", code="E_CLUSTER_MESSAGE")

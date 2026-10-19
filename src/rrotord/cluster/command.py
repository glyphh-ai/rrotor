"""`rrotord cluster join | leave <node> | promote <node>` (docs/specs/cluster.md, 10): run on a node (`docker compose
exec`, `kubectl exec`), never over the API, so no runtime key can change the cluster. It is authenticated by the
cluster secret: from the node's own environment it makes a leaf from the cluster CA naming itself the command (never
a node id) and speaks to the node's own cluster listener on loopback, which does it (a follower passes a leave to its
head, any node a promotion to the node it names).

- `join`: this node asks the head to join now, its leaving (if it left) forgotten; answers what came of it.
- `leave <node>`: the head removes the member; it is told and does not ask again until `join` is run on it.
- `promote <node> [--force] [--yes]`: manual failover (node.py). The node named becomes head at the next term if a
  majority of members is reachable from it and gives it their votes. It shows each member's lag, and refuses a node
  behind another (`E_CLUSTER_LAG`) unless `--force`, which loses the writes it lacks. When the head does not answer
  it asks the operator to confirm that it is down (at a terminal; else `--yes` says so)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import aiohttp

from rrotord.cluster.tls import COMMAND, Credentials
from rrotord.cluster.transport import ClusterError, dial
from rrotord.config import Config
from rrotord.errors import RrotorError

USAGE = "rrotord cluster join | leave <node id> | promote <node id> [--force] [--yes]"
PROMOTE_WITHIN = 90.0  # the survey, the votes, and what the node promoted applies before it leads

Confirm = Callable[[str], bool]


async def ask(
    config: Config, name: str, node: str | None = None, host: str = "127.0.0.1", **more: Any
) -> dict[str, Any]:
    """The command, done by this node's listener: its answer, or a ClusterError."""
    if not config.cluster:
        raise RrotorError("The cluster is off on this node (RROTOR_CLUSTER).", code="E_CONFIG")
    credentials = Credentials(list(config.cluster_secrets), COMMAND, config.run_dir / "cluster")
    async with aiohttp.ClientSession() as session:
        channel, _, reader = await dial(
            session, credentials, f"{host}:{config.cluster_port}", {"node_id": COMMAND, "kind": "command"}
        )
        try:
            got = await channel.request("command", name=name, node=node, within=PROMOTE_WITHIN, **more)
        finally:
            reader.cancel()
            await channel.close()
    return {k: v for k, v in got.items() if k not in ("type", "id", "ok")}


def lags(detail: dict[str, Any]) -> str:
    """Each member's lag behind the longest log, as the promotion found it."""
    members = detail.get("members")
    if not isinstance(members, dict):
        return ""
    lines = []
    for node, seen in sorted(members.items()):
        shown = "unreachable" if not seen.get("reachable") else f"seq {seen.get('last_seq')}, lag {seen.get('lag')}"
        lines.append(f"  {node}: {shown}")
    return "\n" + "\n".join(lines)


async def run(config: Config, args: Sequence[str], confirm: Confirm | None = None) -> tuple[int, str]:
    """The command line after `rrotord cluster`: the exit code and what to print. `confirm` asks the operator a
    question at a terminal (None: there is none, and `--yes` is the answer)."""
    more: dict[str, Any] = {}
    match list(args):
        case ["join"]:
            name, node = "join", None
        case ["leave", node_id]:
            name, node = "leave", node_id
        case ["promote", node_id, *flags] if set(flags) <= {"--force", "--yes"}:
            name, node = "promote", node_id
            more = {"force": "--force" in flags, "yes": "--yes" in flags}
        case _:
            return 2, f"usage: {USAGE}"
    try:
        try:
            done = await ask(config, name, node, **more)
        except ClusterError as err:
            if err.code != "E_CLUSTER_CONFIRM" or confirm is None or not confirm(f"{err.message} Is it down? [y/N] "):
                raise
            done = await ask(config, name, node, **(more | {"yes": True}))
    except ClusterError as err:
        hint = " (run again with --yes once it is down)" if err.code == "E_CLUSTER_CONFIRM" else ""
        return 1, f"rrotord cluster {name}: {err.code}: {err.message}{hint}{lags(err.detail)}"
    except RrotorError as err:
        return 2, f"rrotord cluster {name}: {err.code}: {err.message}"
    return 0, json.dumps(done, indent=2)


def terminal(question: str) -> bool:
    """A yes from the operator at a terminal."""
    return input(question).strip().lower() in ("y", "yes")

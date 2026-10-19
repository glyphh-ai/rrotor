"""`rrotord cluster join | leave <node>` (docs/specs/cluster.md, 10): run on a node (`docker compose exec`,
`kubectl exec`), never over the API, so no runtime key can change the cluster. It is authenticated by the cluster
secret: from the node's own environment it makes a leaf from the cluster CA naming itself the command (never a node
id) and speaks to the node's own cluster listener on loopback, which does it (a follower passes a leave to its head).

- `join`: this node asks the head to join now, its leaving (if it left) forgotten; answers what came of it.
- `leave <node>`: the head removes the member; it is told and does not ask again until `join` is run on it.

`promote` (manual failover) comes with failover; it is refused here as not built yet."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import aiohttp

from rrotord.cluster.tls import COMMAND, Credentials
from rrotord.cluster.transport import ClusterError, dial
from rrotord.config import Config
from rrotord.errors import RrotorError

USAGE = "rrotord cluster join | leave <node id>"


async def ask(config: Config, name: str, node: str | None = None, host: str = "127.0.0.1") -> dict[str, Any]:
    """The command, done by this node's listener: its answer, or a ClusterError."""
    if not config.cluster:
        raise RrotorError("The cluster is off on this node (RROTOR_CLUSTER).", code="E_CONFIG")
    credentials = Credentials(list(config.cluster_secrets), COMMAND, config.run_dir / "cluster")
    async with aiohttp.ClientSession() as session:
        channel, _, reader = await dial(
            session, credentials, f"{host}:{config.cluster_port}", {"node_id": COMMAND, "kind": "command"}
        )
        try:
            got = await channel.request("command", name=name, node=node)
        finally:
            reader.cancel()
            await channel.close()
    return {k: v for k, v in got.items() if k not in ("type", "id", "ok")}


async def run(config: Config, args: Sequence[str]) -> tuple[int, str]:
    """The command line after `rrotord cluster`: the exit code and what to print."""
    match list(args):
        case ["join"]:
            name, node = "join", None
        case ["leave", node_id]:
            name, node = "leave", node_id
        case ["promote", *_]:
            return 2, "rrotord cluster promote: manual failover is not built yet."
        case _:
            return 2, f"usage: {USAGE}"
    try:
        done = await ask(config, name, node)
    except ClusterError as err:
        return 1, f"rrotord cluster {name}: {err.code}: {err.message}"
    except RrotorError as err:
        return 2, f"rrotord cluster {name}: {err.code}: {err.message}"
    return 0, json.dumps(done, indent=2)

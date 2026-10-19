"""The cluster's transport (docs/specs/cluster.md, 6): a WebSocket on aiohttp, on its own listener (`:7778`), never the
API's port and never behind the public load balancer, with mutual TLS from the cluster secret (tls.py).

Frames are JSON objects with a `type` and an `id`, as on the link. A request (`hello`, `join`, `ping`, `command`) is
answered by an `answer` with the request's id: `ok` true and the answer's fields, or `ok` false and an `error`
{code, message}. The head may also push to a follower without being asked (`removed`). The first frame on every
connection is the dialer's `hello`, naming the node (or `rrotord cluster`'s command) its certificate names; the
listener's answer names the listener, which the dialer checks against the listener's certificate the same way.

Replication's (replication.py, node.py): the head's `append` and `snapshot`, asked of a follower over the follower's
own connection and answered with the seq applied; `forward`, either way; and two plain GETs on the same listener with
the same mutual TLS (`fetch`), `/cluster/v1/versions/<model>/<commit>.tar` and `/cluster/v1/snapshot`. A refusal
carries its HTTP status and detail, so a forwarded request's caller sees what the head said. `vote` and `telemetry`
come with elections and the aggregate drain: an unknown type is answered E_CLUSTER_MESSAGE."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import aiohttp

from rrotord.cluster.tls import Credentials, peer_name
from rrotord.errors import RrotorError

log = logging.getLogger("rrotor.cluster")
PATH = "/cluster/v1/link"
ANSWER_WITHIN = 10.0  # seconds a request waits for its answer
CONNECT_WITHIN = 5.0
MESSAGE_MAX = 128 * 1024 * 1024  # a forwarded deploy's bundle, an append of a store's rows
CHUNK = 1024 * 1024
FOREIGN = "cluster: peer certificate not from this cluster"


class ClusterError(RrotorError):
    code, status = "E_CLUSTER", 503


def frame(kind: str, /, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "id": uuid.uuid4().hex, **fields}


def answer(asked: dict[str, Any], /, **fields: Any) -> dict[str, Any]:
    return {"type": "answer", "id": asked.get("id"), "ok": True, **fields}


def failed(asked: dict[str, Any], err: RrotorError) -> dict[str, Any]:
    """A refusal, as the asking end raises it again: its code, message, HTTP status and detail."""
    error = {"code": err.code, "message": err.message, "status": err.status, "detail": err.detail}
    return {"type": "answer", "id": asked.get("id"), "ok": False, "error": error}


def decoded(text: str) -> dict[str, Any] | None:
    try:
        msg = json.loads(text)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) and isinstance(msg.get("type"), str) else None


class Channel:
    """One WebSocket between two nodes (or `rrotord cluster` and its node), either end of it: send a frame, ask and
    wait for the answer, read what arrives. `peer` is the name the other end's certificate carries."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse | Any, peer: str | None) -> None:
        self.ws, self.peer = ws, peer
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def send(self, msg: dict[str, Any]) -> None:
        await self.ws.send_str(json.dumps(msg, separators=(",", ":")))

    async def request(self, kind: str, /, *, within: float | None = None, **fields: Any) -> dict[str, Any]:
        """A request and its answer, within `within` seconds (TimeoutError past it); a refusal raises ClusterError
        with the code, status and detail the other end gave. Someone must be reading this channel (`pump`, or the
        listener's loop) for the answer to arrive."""
        msg = frame(kind, **fields)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[msg["id"]] = future
        try:
            await self.send(msg)
            got = await asyncio.wait_for(future, within or ANSWER_WITHIN)
        finally:
            self.pending.pop(msg["id"], None)
        if not got.get("ok"):
            error = got["error"] if isinstance(got.get("error"), dict) else {}
            status = error.get("status")
            sent = error.get("detail")
            detail: dict[str, Any] = sent if isinstance(sent, dict) else {}
            raise ClusterError(
                str(error.get("message", "refused")),
                code=str(error.get("code", "E_CLUSTER")),
                status=status if isinstance(status, int) else None,
                **detail,
            )
        return got

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        """Every frame that arrives, answers to this end's requests given to their waiters instead; until it
        closes. A frame that is not a JSON object with a type is dropped."""
        async for raw in self.ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            msg = decoded(raw.data)
            if msg is None:
                continue
            waiter = self.pending.get(str(msg.get("id"))) if msg["type"] == "answer" else None
            if waiter is not None:
                if not waiter.done():
                    waiter.set_result(msg)
                continue
            yield msg

    async def pump(self, pushed: Callable[[dict[str, Any]], Any] = lambda _: None) -> None:
        """Read until the channel closes, each frame not an answer handed to `pushed`; then every waiter fails."""
        try:
            async for msg in self.frames():
                pushed(msg)
        finally:
            for waiter in self.pending.values():
                if not waiter.done():
                    waiter.set_exception(ClusterError("The connection closed.", code="E_CLUSTER_CLOSED"))

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close()


async def dial(
    session: aiohttp.ClientSession,
    credentials: Credentials,
    address: str,
    hello: dict[str, Any],
    pushed: Callable[[dict[str, Any]], Any] = lambda _: None,
) -> tuple[Channel, dict[str, Any], asyncio.Task[None]]:
    """Connect to a node at host:port with each identity in turn (the current secret's, then the previous one's),
    say hello, and check that its answer names the node its certificate does. Answers the channel, the hello's answer
    and the task reading it. Raises ClusterError: E_CLUSTER_UNREACHABLE, E_CLUSTER_FOREIGN (a certificate not from
    this cluster's CA, logged as such), E_CLUSTER_PEER (a hello naming another node), or what the other end refused."""
    foreign = False
    for identity in credentials.identities:
        try:
            ws = await session.ws_connect(
                f"https://{address}{PATH}",
                ssl=identity.client,
                server_hostname=identity.server_name,
                timeout=aiohttp.ClientWSTimeout(ws_close=CONNECT_WITHIN),
                autoping=True,
                heartbeat=None,
                max_msg_size=MESSAGE_MAX,
            )
        except aiohttp.ClientConnectorCertificateError:
            foreign = True
            continue
        except (aiohttp.ClientError, OSError, TimeoutError, ssl.SSLError):
            continue  # unreachable, or our certificate refused: the next identity, if any
        channel = Channel(ws, peer_name(ws.get_extra_info("ssl_object")))
        reader = asyncio.create_task(channel.pump(pushed))
        try:
            answered = await channel.request("hello", **hello)
        except ClusterError as err:
            reader.cancel()
            await channel.close()
            if err.code == "E_CLUSTER_CLOSED":
                continue  # closed at once: our certificate was refused; the next identity, if any
            raise
        if answered.get("node_id") != channel.peer:
            reader.cancel()
            await channel.close()
            log.warning("cluster: peer certificate names another node", extra={"address": address})
            raise ClusterError(f"The node at {address} is not the one its certificate names.", code="E_CLUSTER_PEER")
        return channel, answered, reader
    if foreign:
        log.error(FOREIGN, extra={"address": address})
        raise ClusterError(f"{address}: {FOREIGN}.", code="E_CLUSTER_FOREIGN")
    raise ClusterError(f"No node answered at {address}.", code="E_CLUSTER_UNREACHABLE")


async def fetch(
    session: aiohttp.ClientSession, credentials: Credentials, address: str, path: str, dest: Path, within: float
) -> dict[str, str]:
    """A plain GET on a node's listener (`/cluster/v1/...`), with the same mutual TLS, streamed into `dest`; answers
    its headers. Raises ClusterError: what the other end refused (its code and status), or E_CLUSTER_UNREACHABLE."""
    for identity in credentials.identities:
        try:
            async with session.get(
                f"https://{address}{path}",
                ssl=identity.client,
                server_hostname=identity.server_name,
                timeout=aiohttp.ClientTimeout(total=within),
            ) as res:
                if res.status != 200:
                    error = (await res.json(content_type=None) or {}).get("error") or {}
                    raise ClusterError(
                        str(error.get("message", f"{res.status} from {path}")),
                        code=str(error.get("code", "E_CLUSTER")),
                        status=res.status,
                    )
                with dest.open("wb") as f:
                    async for chunk in res.content.iter_chunked(CHUNK):
                        f.write(chunk)
                return dict(res.headers)
        except (aiohttp.ClientError, OSError, TimeoutError, ssl.SSLError, ValueError):
            continue
    raise ClusterError(f"No node answered {path} at {address}.", code="E_CLUSTER_UNREACHABLE")

"""The transport: JSON frames over a WebSocket with mutual TLS, a request answered by its id; dialling with each
identity in turn, a foreign certificate logged as not from this cluster, a hello that names another node refused;
and what the listener refuses."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import WSMsgType, web

from rrotord.cluster import transport
from rrotord.cluster.node import Cluster
from rrotord.cluster.tls import COMMAND, Credentials, peer_name
from rrotord.cluster.transport import Channel, ClusterError, dial
from tests.daemon.cluster.conftest import SECRET, Nodes, until


class Raw:
    def __init__(self, kind: WSMsgType, data: Any) -> None:
        self.type, self.data = kind, data


class FakeWs:
    def __init__(self, frames: list[Raw]) -> None:
        self.frames, self.closed = frames, False
        self.sent: list[dict[str, Any]] = []

    async def send_str(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self) -> None:
        raise RuntimeError("already gone")

    def __aiter__(self) -> AsyncIterator[Raw]:
        return self._each()

    async def _each(self) -> AsyncIterator[Raw]:
        for f in self.frames:
            await asyncio.sleep(0)
            yield f


async def test_frames_skip_what_is_not_a_typed_json_object_and_give_answers_to_their_waiters():
    ws = FakeWs([])
    channel = Channel(ws, "b")
    asking = asyncio.create_task(channel.request("ping"))
    await asyncio.sleep(0)
    msg_id = ws.sent[0]["id"]
    ws.frames = [
        Raw(WSMsgType.BINARY, b"x"),
        Raw(WSMsgType.TEXT, "not json"),
        Raw(WSMsgType.TEXT, "[1]"),
        Raw(WSMsgType.TEXT, '{"no": "type"}'),
        Raw(WSMsgType.TEXT, json.dumps({"type": "answer", "id": msg_id, "ok": True, "n": 1})),
        Raw(WSMsgType.TEXT, json.dumps({"type": "answer", "id": "done", "ok": True})),  # its waiter answered: dropped
        Raw(WSMsgType.TEXT, json.dumps({"type": "answer", "id": "other", "ok": True})),
        Raw(WSMsgType.TEXT, json.dumps({"type": "removed", "id": "r"})),
    ]
    pushed: list[dict[str, Any]] = []
    done: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    done.set_result({})
    channel.pending["done"] = done
    pump = asyncio.create_task(channel.pump(pushed.append))
    assert (await asyncio.wait_for(asking, 2))["n"] == 1
    await pump
    assert [m["type"] for m in pushed] == ["answer", "removed"]
    await channel.close()  # an error closing is nobody's concern


async def test_a_refusal_raises_its_code_and_a_closed_channel_fails_its_waiters():
    ws = FakeWs([])
    channel = Channel(ws, "b")
    asking = [asyncio.create_task(channel.request("join")) for _ in range(3)]
    await asyncio.sleep(0)
    ids = [m["id"] for m in ws.sent]
    ws.frames = [
        Raw(WSMsgType.TEXT, json.dumps({"type": "answer", "id": ids[0], "ok": False, "error": {"code": "E_X"}})),
        Raw(WSMsgType.TEXT, json.dumps({"type": "answer", "id": ids[1], "ok": False, "error": "nope"})),
    ]
    await channel.pump()
    codes = []
    for task in asking:
        with pytest.raises(ClusterError) as err:
            await task
        codes.append((err.value.code, err.value.message))
    assert codes == [("E_X", "refused"), ("E_CLUSTER", "refused"), ("E_CLUSTER_CLOSED", "The connection closed.")]
    assert transport.decoded('{"type": 1}') is None


async def serve(credentials: Credentials, answer: Any) -> tuple[web.AppRunner, str]:
    """A listener that answers every frame with `answer(msg, peer)`, or closes when it answers None."""

    async def link(request: web.Request) -> web.WebSocketResponse:
        name = peer_name(request.transport.get_extra_info("ssl_object"))  # type: ignore[union-attr]
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for raw in ws:
            msg = json.loads(raw.data)
            reply = answer(msg, name)
            if reply is None:
                break
            await ws.send_str(json.dumps(reply))
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get(transport.PATH, link)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=credentials.server)
    await site.start()
    return runner, f"127.0.0.1:{runner.addresses[0][1]}"


def says(node_id: str, ok: bool = True) -> Any:
    def answer(msg: dict[str, Any], peer: str | None) -> dict[str, Any]:
        if ok:
            return transport.answer(msg, node_id=node_id, peer=peer)
        return transport.failed(msg, ClusterError("no", code="E_NOPE"))

    return answer


async def test_dialling_checks_the_listeners_certificate_and_name(tmp_path: Path, caplog):
    listener = Credentials([SECRET], "b", tmp_path / "l")
    me = Credentials([SECRET], "a", tmp_path / "me")
    async with aiohttp.ClientSession() as session:
        runner, address = await serve(listener, says("b"))
        channel, answered, reader = await dial(session, me, address, {"node_id": "a"})
        assert (answered["node_id"], answered["peer"], channel.peer) == ("b", "a", "b")
        reader.cancel()
        await channel.close()
        await runner.cleanup()
        runner, address = await serve(listener, says("c"))  # a hello naming another node than its certificate
        with pytest.raises(ClusterError) as err:
            await dial(session, me, address, {"node_id": "a"})
        assert err.value.code == "E_CLUSTER_PEER" and "names another node" in caplog.text
        await runner.cleanup()
        runner, address = await serve(listener, says("b", ok=False))
        with pytest.raises(ClusterError) as err:
            await dial(session, me, address, {"node_id": "a"})
        assert err.value.code == "E_NOPE"
        foreign = Credentials([os.urandom(32)], "a", tmp_path / "f")
        with caplog.at_level(logging.ERROR), pytest.raises(ClusterError) as err:
            await dial(session, foreign, address, {"node_id": "a"})
        assert err.value.code == "E_CLUSTER_FOREIGN" and transport.FOREIGN in caplog.text
        await runner.cleanup()
        with pytest.raises(ClusterError) as err:
            await dial(session, me, address, {"node_id": "a"})
        assert err.value.code == "E_CLUSTER_UNREACHABLE"


async def test_while_a_new_secret_rolls_out_old_and_new_nodes_still_meet(tmp_path: Path):
    new = os.urandom(32)
    old_only = Credentials([SECRET], "old", tmp_path / "o")
    rolled = Credentials([new, SECRET], "rolled", tmp_path / "r")
    new_only = Credentials([new], "new", tmp_path / "n")
    async with aiohttp.ClientSession() as session:
        for listener, dialler in ((old_only, rolled), (rolled, old_only), (rolled, new_only)):
            runner, address = await serve(listener, lambda msg, peer, name=listener.identities[0]: None)
            await runner.cleanup()
            name = "old" if listener is old_only else "rolled"
            runner, address = await serve(listener, says(name))
            channel, answered, reader = await dial(session, dialler, address, {})
            assert answered["node_id"] == name
            reader.cancel()
            await channel.close()
            await runner.cleanup()
        runner, address = await serve(old_only, says("old"))
        with pytest.raises(ClusterError) as err:
            await dial(session, new_only, address, {})  # rolled past the old secret: no longer one cluster
        assert err.value.code == "E_CLUSTER_FOREIGN"
        await runner.cleanup()


async def test_a_listener_that_closes_at_once_is_tried_with_the_next_identity(tmp_path: Path):
    listener = Credentials([SECRET], "b", tmp_path / "l")
    async with aiohttp.ClientSession() as session:
        runner, address = await serve(listener, lambda msg, peer: None)
        with pytest.raises(ClusterError) as err:
            await dial(session, Credentials([SECRET], "a", tmp_path / "a"), address, {})
        assert err.value.code == "E_CLUSTER_UNREACHABLE"
        await runner.cleanup()


async def raw(session: aiohttp.ClientSession, credentials: Credentials, c: Cluster) -> Channel:
    ws = await session.ws_connect(
        f"https://{c.address}{transport.PATH}",
        ssl=credentials.identities[0].client,
        server_hostname=credentials.identities[0].server_name,
    )
    channel = Channel(ws, None)
    asyncio.create_task(channel.pump())  # noqa: RUF006
    return channel


async def test_what_the_listener_refuses(nodes: Nodes, tmp_path: Path):
    a = nodes.make("a", ["a"], cluster_head="a")
    await a.start()
    await until(lambda: a.role == "head")
    async with aiohttp.ClientSession() as session:
        x = Credentials([SECRET], "x", tmp_path / "x")
        channel = await raw(session, x, a)
        with pytest.raises(ClusterError) as err:
            await channel.request("ping")
        assert err.value.code == "E_CLUSTER_HELLO"
        hellos: list[dict[str, Any]] = [
            {"node_id": "y", "kind": "node"},
            {"node_id": "x", "kind": "other"},
            {"kind": "node"},
        ]
        for hello in hellos:
            with pytest.raises(ClusterError) as err:
                await channel.request("hello", **hello)
            assert err.value.code == "E_CLUSTER_PEER"
        bad = await raw(session, Credentials([SECRET], "Bad_Id", tmp_path / "b"), a)
        with pytest.raises(ClusterError) as err:
            await bad.request("hello", node_id="Bad_Id", kind="node")
        assert err.value.code == "E_CLUSTER_PEER"
        await channel.request("hello", node_id="x", kind="node")
        for kind in ("vote", "append", "telemetry"):  # telemetry: a member's only
            with pytest.raises(ClusterError) as err:
                await channel.request(kind)
            assert err.value.code == "E_CLUSTER_MESSAGE"
        with pytest.raises(ClusterError) as err:
            await channel.request("ping")
        assert err.value.code == "E_CLUSTER_MEMBER"
        command = await raw(session, Credentials([SECRET], COMMAND, tmp_path / "c"), a)
        hello = await command.request("hello", node_id=COMMAND, kind="command")
        assert (hello["node_id"], hello["role"], hello["head"]) == ("a", "head", "a")
        with pytest.raises(ClusterError) as err:
            await command.request("join")
        assert err.value.code == "E_CLUSTER_MESSAGE"
        for c in (channel, bad, command):
            await c.close()


async def test_a_join_the_head_cannot_answer_now_is_tried_again(nodes: Nodes):
    c = nodes.make("c", ["c"], cluster_head="z")
    await c._open()
    assert c.membership is not None
    c.membership.cancel()  # step by hand

    def head(msg: dict[str, Any], peer: str | None) -> dict[str, Any]:
        if msg["type"] == "hello":
            return transport.answer(msg, node_id="h", role="head")
        return transport.failed(msg, ClusterError("Not now.", code="E_NOT_HEAD"))

    runner, address = await serve(Credentials([SECRET], "h", c.config.run_dir / "h"), head)
    from rrotord.cluster import node as cluster_node

    assert await c._join(address) == cluster_node.RETRY and c.status == "joining" and c.refusal is None
    await runner.cleanup()
    await c._shut()


async def test_a_fetch_nobody_answers_is_unreachable(tmp_path: Path):
    from tests.daemon.cluster.conftest import free_port

    async with aiohttp.ClientSession() as session:
        with pytest.raises(ClusterError) as err:
            await transport.fetch(
                session, Credentials([SECRET], "a", tmp_path / "a"), f"127.0.0.1:{free_port()}", "/x", tmp_path / "x", 2
            )
    assert err.value.code == "E_CLUSTER_UNREACHABLE"

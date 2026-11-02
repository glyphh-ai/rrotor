"""The link client against a fake Cloud: a real aiohttp WebSocket server speaking protocol 3."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from rrotord.link import protocol as p
from rrotord.link.client import LinkClient
from rrotord.link.credentials import Credentials
from rrotord.link.outbox import Outbox
from tests.fakes import token

Behaviour = Callable[[web.WebSocketResponse, "Cloud"], Awaitable[None]]
SHA = "b" * 40


async def receive(ws: web.WebSocketResponse) -> dict[str, Any]:
    """The next message that is not a ping or pong."""
    while True:
        msg = await ws.receive()
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError(msg.type)
        data: dict[str, Any] = json.loads(msg.data)
        if data.get("type") not in ("ping", "pong"):
            return data


LICENCE = {"payload": {"plan": "Pro", "mods": {"rrotor": {"limits": {}}}}, "signature": "c2ln"}


async def welcome(ws: web.WebSocketResponse, cloud: Cloud) -> None:
    cloud.hellos.append(await receive(ws))
    await ws.send_json({"type": "welcome", "id": p.new_id(), "licence": LICENCE})


async def hold(ws: web.WebSocketResponse, cloud: Cloud) -> None:
    await welcome(ws, cloud)
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            cloud.got.append(json.loads(msg.data))


def closes(code: int) -> Behaviour:
    async def go(ws: web.WebSocketResponse, cloud: Cloud) -> None:
        await welcome(ws, cloud)
        await ws.close(code=code, message=b"bye")

    return go


class Cloud:
    def __init__(self, *behaviours: Behaviour) -> None:
        self.behaviours = list(behaviours)
        self.connections = 0
        self.headers: list[str] = []
        self.hellos: list[dict[str, Any]] = []
        self.got: list[dict[str, Any]] = []
        self.sockets: list[web.WebSocketResponse] = []
        app = web.Application()
        app.router.add_get("/v1/runtime/link", self.link)
        self.server = TestServer(app)

    async def link(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(autoping=True)
        await ws.prepare(request)
        self.sockets.append(ws)
        self.headers.append(request.headers.get("Authorization", ""))
        behaviour = self.behaviours[min(self.connections, len(self.behaviours) - 1)]
        self.connections += 1
        with contextlib.suppress(ConnectionError):
            await behaviour(ws, self)
        return ws

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.got if m.get("type") == kind]


class Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def deployed(self) -> list[dict[str, Any]]:
        return [{"program": "x", "version": 1, "commit_sha": SHA}]

    async def on_welcome(self, message: p.Welcome) -> None:
        self.calls.append(("welcome", message))

    async def on_call(self, message: p.Call) -> None:
        self.calls.append(("call", message))

    async def on_continue(self, message: p.Continue) -> None:
        self.calls.append(("continue", message))

    async def on_cancel(self, message: p.Cancel) -> None:
        self.calls.append(("cancel", message))
        raise RuntimeError("handler broke")

    async def on_deploy(self, message: p.Deploy) -> None:
        self.calls.append(("deploy", message))

    async def on_access(self, message: p.Keys | p.Pause | p.Resume) -> None:
        self.calls.append((message.type, message))

    async def on_app(self, message: p.App) -> None:
        self.calls.append(("app", message))


async def fast(delay: float) -> None:
    await asyncio.sleep(min(delay, 0.05))


async def until(check: Callable[[], bool], seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


class Link:
    """A client, its credentials and its task, against a cloud."""

    def __init__(
        self, cloud: Cloud, tmp_path: Any, session: aiohttp.ClientSession, handler: Handler | None = None
    ) -> None:
        self.cloud = cloud
        self.credentials = Credentials(tmp_path)
        self.handler = handler or Handler()

        async def connect(url: str, headers: dict[str, str]) -> aiohttp.ClientWebSocketResponse:
            return await session.ws_connect(url, headers=headers)

        self.client = LinkClient(
            url=str(cloud.server.make_url("/v1/runtime/link")),
            version="0.1.0",
            mode="local",
            credentials=self.credentials,
            outbox=Outbox(tmp_path / "link"),
            handler=self.handler,
            connect=connect,
            sleep=fast,
        )
        self.task: asyncio.Task[None] | None = None

    def sign_in(self) -> str:
        secret = token()
        self.credentials.set(str(uuid.UUID(int=1)), secret)
        return secret

    def start(self) -> None:
        self.task = asyncio.create_task(self.client.run())

    async def stop(self) -> None:
        assert self.task is not None
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


async def link_to(cloud: Cloud, tmp_path: Any, session: aiohttp.ClientSession) -> Link:
    await cloud.server.start_server()
    return Link(cloud, tmp_path, session)


async def test_without_a_token_it_waits(tmp_path, session):
    cloud = Cloud(hold)
    link = await link_to(cloud, tmp_path, session)
    link.start()
    await asyncio.sleep(0.05)
    assert link.client.state == "no_token" and cloud.connections == 0
    await link.stop()
    await cloud.server.close()


async def test_it_says_hello_goes_online_and_replays_what_it_owes(tmp_path, session):
    cloud = Cloud(hold)
    link = await link_to(cloud, tmp_path, session)
    secret = link.sign_in()
    link.client.outbox.restart(__import__("hashlib").sha256(secret.encode()).hexdigest())
    await link.client.owe(lambda seq: p.event(seq, "x", "k", {}))
    assert link.client.view()["owed"] == 1
    link.start()
    await until(lambda: bool(cloud.of("event")))
    assert cloud.headers == [f"Bearer {secret}"]
    hello = cloud.hellos[0]
    assert (hello["protocol"], hello["runtime_id"], hello["deployed"][0]["program"]) == (3, str(uuid.UUID(int=1)), "x")
    assert link.client.state == "online" and link.client.attempt == 0
    welcomed = [m for kind, m in link.handler.calls if kind == "welcome"]
    assert len(welcomed) == 1 and welcomed[0].licence is not None and welcomed[0].licence.payload == LICENCE["payload"]
    await link.client.owe(lambda seq: p.result(seq, "r", {"status": "done"}, 1))
    await until(lambda: bool(cloud.of("result")))
    assert cloud.of("result")[0]["seq"] == 2
    await link.stop()
    await cloud.server.close()


async def test_it_answers_pings_takes_acks_and_hands_messages_to_the_handler(tmp_path, session, caplog):
    caplog.set_level(logging.INFO, logger="rrotor.link")
    run_id = str(uuid.uuid4())
    acked = asyncio.Event()

    async def talk(ws: web.WebSocketResponse, cloud: Cloud) -> None:
        await welcome(ws, cloud)
        owed = await receive(ws)
        await ws.send_bytes(b"binary is skipped")
        await ws.send_str("not json")
        await ws.send_json({"type": "ack", "of": owed["id"]})
        await ws.send_json({"type": "ping"})
        for message in (
            {
                "type": "call",
                "run_id": run_id,
                "program": "x",
                "version": 1,
                "function": "start",
                "inputs": {},
                "caller": "routine",
            },
            {"type": "continue", "run_id": run_id, "answer": {"approved": True}},
            {"type": "cancel", "run_id": run_id},
            {"type": "deploy", "program": "x", "version": 2, "commit_sha": SHA},
            {"type": "keys", "hashes": ["a" * 64]},
            {"type": "pause", "reason": "runtime_hours"},
            {"type": "resume"},
            {"type": "app", "program": "x", "version": 2, "state": "approved", "roles": []},
            {"type": "artifact", "program": "x", "version": 2, "state": "rejected", "roles": []},  # protocol 2's name
            {"type": "secret_set"},
            {"type": "pong"},
        ):
            await ws.send_json(message)
        while (msg := await ws.receive()).type == aiohttp.WSMsgType.TEXT:
            if json.loads(msg.data)["type"] == "pong":
                cloud.got.append(json.loads(msg.data))
                acked.set()

    cloud = Cloud(talk)
    link = await link_to(cloud, tmp_path, session)
    secret = link.sign_in()
    link.client.outbox.restart(__import__("hashlib").sha256(secret.encode()).hexdigest())
    await link.client.owe(lambda seq: p.event(seq, "x", "k", {}))
    link.start()
    await asyncio.wait_for(acked.wait(), 5)
    await until(lambda: len(link.handler.calls) == 10)
    assert [c[0] for c in link.handler.calls] == [
        "welcome",
        "call",
        "continue",
        "cancel",
        "deploy",
        "keys",
        "pause",
        "resume",
        "app",
        "app",
    ]
    assert [c[1].state for c in link.handler.calls[-2:]] == ["approved", "rejected"]  # `artifact` read as `app`
    await until(lambda: not link.client.outbox.pending)
    await until(lambda: "a message from the Cloud failed" in caplog.text)
    assert "unreadable message skipped" in caplog.text  # an earlier server's secret_set among them
    await link.stop()
    await cloud.server.close()


@pytest.mark.parametrize(
    ("code", "state"), [(4401, "refused"), (4409, "replaced"), (4426, "upgrade_needed"), (4400, "bad_hello")]
)
async def test_a_close_that_says_stop_stops_until_a_new_token(tmp_path, session, code, state):
    cloud = Cloud(closes(code), hold)
    link = await link_to(cloud, tmp_path, session)
    link.sign_in()
    link.start()
    await until(lambda: link.client.state == state)
    assert link.client.view()["last_close"] == {"code": code, "reason": "bye"}
    await asyncio.sleep(0.1)
    assert cloud.connections == 1
    link.sign_in()
    await until(lambda: link.client.state == "online")
    assert cloud.connections == 2
    await link.stop()
    await cloud.server.close()


async def test_other_closes_reconnect_with_backoff(tmp_path, session):
    cloud = Cloud(closes(p.FAILED), closes(p.RESTART), hold)
    link = await link_to(cloud, tmp_path, session)
    link.sign_in()
    states: list[str] = []
    real = link.client._set

    def track(state: str) -> None:
        states.append(state)
        real(state)

    link.client._set = track  # type: ignore[method-assign]
    link.start()
    await until(lambda: cloud.connections == 3 and link.client.state == "online")
    assert "backoff" in states and link.client.attempt == 0
    await link.stop()
    await cloud.server.close()


async def test_no_welcome_closes_and_retries(tmp_path, session):
    async def binary(ws: web.WebSocketResponse, cloud: Cloud) -> None:
        await receive(ws)
        await ws.send_bytes(b"x")
        await ws.receive()

    async def garbage(ws: web.WebSocketResponse, cloud: Cloud) -> None:
        await receive(ws)
        await ws.send_str("{")
        await ws.receive()

    async def other(ws: web.WebSocketResponse, cloud: Cloud) -> None:
        await receive(ws)
        await ws.send_json({"type": "ping"})
        await ws.receive()

    async def silent(ws: web.WebSocketResponse, cloud: Cloud) -> None:
        await receive(ws)
        await ws.receive()

    cloud = Cloud(binary, garbage, other, silent, hold)
    link = await link_to(cloud, tmp_path, session)
    link.client.WELCOME_WAIT = 0.2
    link.sign_in()
    link.start()
    await until(lambda: link.client.state == "online")
    assert cloud.connections == 5
    await link.stop()
    await cloud.server.close()


async def test_a_connect_that_fails_backs_off_and_tries_again(tmp_path, session):
    cloud = Cloud(hold)
    link = await link_to(cloud, tmp_path, session)
    real = link.client.connect
    tries = []

    async def flaky(url: str, headers: dict[str, str]) -> aiohttp.ClientWebSocketResponse:
        tries.append(url)
        if len(tries) == 1:
            raise aiohttp.ClientConnectionError("refused")
        return await real(url, headers)

    link.client.connect = flaky
    link.sign_in()
    link.start()
    await until(lambda: link.client.state == "online")
    assert len(tries) == 2
    await link.stop()
    await cloud.server.close()


async def test_a_failed_connect_is_shown_as_the_last_close(tmp_path, session):
    cloud = Cloud(hold)
    link = await link_to(cloud, tmp_path, session)

    async def down(url: str, headers: dict[str, str]) -> aiohttp.ClientWebSocketResponse:
        raise OSError("unreachable")

    link.client.connect = down
    link.sign_in()
    link.start()
    await until(lambda: link.client.state == "backoff")
    assert link.client.last_close == {"code": None, "reason": "OSError"}
    await link.stop()
    await cloud.server.close()


async def test_a_new_token_while_online_reconnects_with_it(tmp_path, session):
    cloud = Cloud(hold)
    link = await link_to(cloud, tmp_path, session)
    link.sign_in()
    link.start()
    await until(lambda: link.client.state == "online")
    second = link.sign_in()
    await until(lambda: cloud.connections == 2 and link.client.state == "online")
    assert cloud.headers[1] == f"Bearer {second}"
    await link.stop()
    await cloud.server.close()


async def test_a_silent_cloud_is_left_and_reconnected(tmp_path, session):
    async def mute(ws: web.WebSocketResponse, cloud: Cloud) -> None:
        await welcome(ws, cloud)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                cloud.got.append(json.loads(msg.data))

    cloud = Cloud(mute)
    link = await link_to(cloud, tmp_path, session)
    link.client.PING_EVERY = 0.05
    link.sign_in()
    link.start()
    await until(lambda: cloud.connections >= 2)
    assert any(m["type"] == "ping" for m in cloud.got)
    await link.stop()
    await cloud.server.close()


class BrokenWs:
    closed = False
    close_code = None

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def send_str(self, data: str) -> None:
        raise self.error

    async def close(self, **_: Any) -> None:
        return None


async def test_send_answers_false_when_the_socket_fails(tmp_path):
    client = LinkClient(
        url="ws://x",
        version="0",
        mode="local",
        credentials=Credentials(tmp_path),
        outbox=Outbox(tmp_path),
        handler=Handler(),
        connect=None,  # type: ignore[arg-type]
    )
    assert not await client._send({"a": 1})
    client.ws, client.state = BrokenWs(ConnectionResetError()), "online"  # type: ignore[assignment]
    assert not await client._send({"a": 1})


async def test_a_session_whose_hello_fails_ends_quietly(tmp_path):
    async def connect(url: str, headers: dict[str, str]) -> Any:
        return BrokenWs(RuntimeError("closing"))

    client = LinkClient(
        url="ws://x",
        version="0",
        mode="local",
        credentials=Credentials(tmp_path),
        outbox=Outbox(tmp_path),
        handler=Handler(),
        connect=connect,
    )
    assert await client._session(token(), str(uuid.UUID(int=1))) is None
    assert client.ws is None and client.last_close == {"code": None, "reason": None}


async def test_the_pinger_leaves_a_link_heard_from_too_long_ago(tmp_path):
    closed: list[Any] = []

    class Ws:
        async def close(self, **kw: Any) -> None:
            closed.append(kw)

    client = LinkClient(
        url="ws://x",
        version="0",
        mode="local",
        credentials=Credentials(tmp_path),
        outbox=Outbox(tmp_path),
        handler=Handler(),
        connect=None,  # type: ignore[arg-type]
        sleep=fast,
    )
    await client._ping(Ws(), lambda: 0.0)  # type: ignore[arg-type]
    assert closed == [{"code": 1000, "message": b"reconnecting"}]


async def test_only_a_clusters_head_links_and_a_follower_lets_it_go(tmp_path, session):
    cloud = Cloud(hold)
    link = await link_to(cloud, tmp_path, session)
    link.sign_in()
    role = {"now": "follower"}
    link.client.held = lambda: role["now"] == "follower"
    link.client.identity = lambda: ("rrotor-1", "c-1", role["now"])
    link.client.BACKOFF_MAX = 0.05
    link.start()
    await until(lambda: link.client.state == "follower")
    await asyncio.sleep(0.1)
    assert cloud.connections == 0
    role["now"] = "head"
    link.client.regate.set()
    await until(lambda: link.client.state == "online")
    assert {k: cloud.hellos[0][k] for k in ("node_id", "cluster_id", "role")} == {
        "node_id": "rrotor-1",
        "cluster_id": "c-1",
        "role": "head",
    }
    link.client.regate.set()  # looked at again, still the head: it stays
    await asyncio.sleep(0.05)
    assert link.client.state == "online"
    role["now"] = "follower"
    link.client.regate.set()
    await until(lambda: link.client.state == "follower")
    assert cloud.connections == 1
    await link.stop()
    await cloud.server.close()

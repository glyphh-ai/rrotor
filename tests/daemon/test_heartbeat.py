"""The heartbeat: exactly its documented body, the machine hashed, the server's licence kept, a failure tried again
with backoff and never fatal."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from rrotord import heartbeat as hb
from rrotord.heartbeat import Heartbeat, machine_hash
from rrotord.licence import Licence
from rrotord.link.credentials import Credentials
from tests.daemon.test_daemon import self_daemon
from tests.fakes import keys

RUNTIME = str(uuid.uuid4())
TOKEN = "tok_" + "c" * 20


class Server:
    """A fake of the server's /v1/license/verify: what it was sent, and what it answers."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.sent: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    async def __call__(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        self.sent.append((url, headers, body))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        return status, body


def beating(tmp_path: Path, post: Any, token: str | None = TOKEN, machine: str | None = "host-machine") -> Heartbeat:
    tmp_path.mkdir(parents=True, exist_ok=True)
    credentials = Credentials(tmp_path / "data")
    if token is not None:
        credentials.set(RUNTIME, token)
    machine_id = tmp_path / "machine-id"
    if machine is not None:
        machine_id.write_text(machine + "\n")
    return Heartbeat(
        server="https://api.glyphh.example/",
        credentials=credentials,
        licence=Licence(tmp_path / "data", keys().public, RUNTIME),
        post=post,
        active_runs=lambda: 2,
        data=tmp_path / "data",
        version="0.1.0",
        machine_id=machine_id,
    )


async def test_a_heartbeat_sends_exactly_its_body_and_keeps_the_licence_answered(tmp_path):
    given = keys().licence(["ada"], runtime_id=RUNTIME, kind="heartbeat", licence_id="l-1", grace_days=14)
    server = Server((200, {"licence": given}), (200, {"licence": given}))
    beat = beating(tmp_path, server)
    assert await beat.beat() and beat.licence.licence_id == "l-1"
    assert await beat.beat()
    machine = hashlib.sha256(f"{RUNTIME}:host-machine".encode()).hexdigest()
    first, second = server.sent
    assert first == (
        "https://api.glyphh.example/v1/license/verify",
        {"Authorization": f"Bearer {TOKEN}"},
        {"machine": machine, "licence_id": None, "version": "0.1.0", "active_runs": 2},
    )
    assert second[2] == {"machine": machine, "licence_id": "l-1", "version": "0.1.0", "active_runs": 2}
    view = beat.view()
    assert view is not None and (view["ok"], view["error"]) == (True, None)


def test_without_a_machine_id_a_random_one_is_made_once_and_kept_on_data(tmp_path):
    data = tmp_path / "data"
    first = machine_hash(RUNTIME, data, tmp_path / "none")
    kept = (data / "machine-id").read_text().strip()
    assert len(kept) == 32 and first == hashlib.sha256(f"{RUNTIME}:{kept}".encode()).hexdigest()
    assert machine_hash(RUNTIME, data, tmp_path / "none") == first
    (tmp_path / "empty").write_text("\n")
    assert machine_hash(RUNTIME, data, tmp_path / "empty") == first
    (tmp_path / "file").write_text("")
    unkept = machine_hash(RUNTIME, tmp_path / "file", tmp_path / "none")  # /data unwritable: random, still sent
    assert len(unkept) == 64 and unkept != first


async def test_a_failed_heartbeat_is_logged_and_says_why(tmp_path, caplog):
    tampered = keys().licence(["ada"])
    tampered["payload"]["plan"] = "Team"
    server = Server(ConnectionError("down"), (503, None), (200, {"nothing": 1}), (200, {"licence": tampered}))
    beat = beating(tmp_path, server)
    reasons = []
    for _ in range(4):
        assert not await beat.beat()
        reasons.append(beat.view()["error"])  # type: ignore[index]
    assert reasons == [
        "ConnectionError",
        "the server answered 503",
        "the server answered 200",
        "The licence's signature does not check out.",
    ]
    assert "heartbeat failed" in caplog.text and beat.licence.payload is None
    alone = beating(tmp_path / "alone", Server(), token=None)
    assert not await alone.beat() and alone.view()["error"] == "no runtime token"  # type: ignore[index]


async def test_it_beats_at_boot_and_daily_and_backs_off_a_minute_doubling_to_an_hour(tmp_path):
    given = keys().licence(["ada"])
    answers: list[Any] = [(500, None)] * 8 + [(200, {"licence": given}), (500, None)]
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == len(answers):
            raise asyncio.CancelledError

    beat = beating(tmp_path, Server(*answers))
    beat.sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        await beat.run()
    assert waits == [60, 120, 240, 480, 960, 1920, 3600, 3600, hb.EVERY, 60]


async def test_the_heartbeat_reaches_a_real_server_through_the_daemons_post(tmp_path, short_dir):
    seen = []
    given = keys().licence(["ada"], kind="heartbeat")

    async def verify(request: web.Request) -> web.Response:
        seen.append((request.headers.get("Authorization"), await request.json()))
        return web.json_response({"licence": given})

    app = web.Application()
    app.router.add_post("/v1/license/verify", verify)
    server = TestServer(app)
    await server.start_server()
    d = self_daemon(tmp_path, short_dir)
    try:
        assert d.heartbeat is not None
        d.heartbeat.url = f"http://127.0.0.1:{server.port}/v1/license/verify"
        d.licence.key = keys().public
        assert await d.heartbeat.beat()
        [(auth, body)] = seen
        assert auth == "Bearer " + d.config.runtime_token  # type: ignore[operator]
        assert sorted(body) == ["active_runs", "licence_id", "machine", "version"] and body["active_runs"] == 0
    finally:
        if d.session is not None:
            await d.session.close()
        await server.close()

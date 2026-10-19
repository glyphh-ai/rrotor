"""The heartbeat: exactly its documented body, the machine hashed, the licence key the server answers offered (and
release N's legacy licence), a failure tried again with backoff and never fatal."""

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
        licence=Licence(tmp_path / "data", RUNTIME, etc=tmp_path / "license.key"),
        post=post,
        active_runs=lambda: 2,
        data=tmp_path / "data",
        version="0.1.0",
        node_id="rrotor-1",
        precedents=lambda: 1200,
        machine_id=machine_id,
    )


async def test_a_heartbeat_sends_exactly_its_body_and_holds_the_key_answered(tmp_path):
    key = keys().key(license_id="2c7f0d9e-0000-4000-8000-000000000001")
    server = Server((200, {"license_key": key}), (200, {"license_key": key}))
    beat = beating(tmp_path, server)
    assert await beat.beat() and beat.licence.licence_id == "2c7f0d9e-0000-4000-8000-000000000001"
    assert beat.licence.view()["source"] == "heartbeat"
    assert await beat.beat()
    machine = hashlib.sha256(f"{RUNTIME}:host-machine".encode()).hexdigest()
    first, second = server.sent
    body = {
        "machine": machine,
        "licence_id": None,
        "version": "0.1.0",
        "active_runs": 2,
        "node_id": "rrotor-1",
        "cluster_id": None,
        "role": "standalone",
        "precedents": 1200,
    }
    assert first == ("https://api.glyphh.example/v1/license/verify", {"Authorization": f"Bearer {TOKEN}"}, body)
    assert second[2] == body | {"licence_id": "2c7f0d9e-0000-4000-8000-000000000001"}
    view = beat.view()
    assert view is not None and (view["ok"], view["error"]) == (True, None)


async def test_the_key_is_preferred_to_the_legacy_licence_answered_beside_it_and_either_alone_is_taken(tmp_path):
    given = keys().licence(["ada"], runtime_id=RUNTIME, kind="heartbeat", licence_id="l-1", grace_days=14, plan="Team")
    key = keys().key(tier="pro")
    beat = beating(tmp_path, Server((200, {"licence": given, "license_key": key}), (200, {"licence": given})))
    assert await beat.beat() and beat.licence.limits().tier == "pro"
    alone = beating(tmp_path / "legacy", Server((200, {"licence": given})))
    assert await alone.beat() and alone.licence.limits().tier == "team" and alone.licence.licence_id == "l-1"
    bad_key = beating(tmp_path / "bad", Server((200, {"licence": given, "license_key": "x.y"})))
    assert await bad_key.beat() and bad_key.licence.limits().tier == "team"  # the legacy licence still counts


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
    server = Server(
        ConnectionError("down"),
        (503, None),
        (200, {"nothing": 1}),
        (200, {"licence": tampered, "license_key": None}),
        (200, {"license_key": keys().key(drop=("tier",))}),
    )
    beat = beating(tmp_path, server)
    reasons = []
    for _ in range(5):
        assert not await beat.beat()
        reasons.append(beat.view()["error"])  # type: ignore[index]
    assert reasons == [
        "ConnectionError",
        "the server answered 503",
        "the server answered 200",
        "the licence key was refused",
        "the licence key was refused",
    ]
    assert "heartbeat failed" in caplog.text and beat.licence.held is None
    assert "licence key refused: tier missing" in caplog.text and beat.licence.state == "bad"
    alone = beating(tmp_path / "alone", Server(), token=None)
    assert not await alone.beat() and alone.view()["error"] == "no runtime token"  # type: ignore[index]


async def test_it_beats_at_boot_and_daily_and_backs_off_a_minute_doubling_to_an_hour(tmp_path):
    answers: list[Any] = [(500, None)] * 8 + [(200, {"license_key": keys().key()}), (500, None)]
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
    key = keys().key()

    async def verify(request: web.Request) -> web.Response:
        seen.append((request.headers.get("Authorization"), await request.json()))
        return web.json_response({"license_key": key})

    app = web.Application()
    app.router.add_post("/v1/license/verify", verify)
    server = TestServer(app)
    await server.start_server()
    d = self_daemon(tmp_path, short_dir)
    try:
        assert d.heartbeat is not None
        d.heartbeat.url = f"http://127.0.0.1:{server.port}/v1/license/verify"
        assert await d.heartbeat.beat()
        [(auth, body)] = seen
        assert auth == "Bearer " + d.config.runtime_token  # type: ignore[operator]
        assert sorted(body) == [
            "active_runs",
            "cluster_id",
            "licence_id",
            "machine",
            "node_id",
            "precedents",
            "role",
            "version",
        ]
        assert (body["active_runs"], body["node_id"], body["role"], body["precedents"]) == (
            0,
            "rrotor-1",
            "standalone",
            0,
        )
        assert d.licence.held is not None and d.licence.held.raw == key
    finally:
        if d.session is not None:
            await d.session.close()
        await server.close()

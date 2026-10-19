"""The drains against an in-process fake collector: logs, metrics and traces reach the customer's collector with
rrotor's resource; a down endpoint loses only the oldest records, counted, and no audit event; server-side logging
goes to Glyphh with the runtime token and without client addresses, payloads or the customer's own OTLP headers."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

import rrotor
from rrotord import drain as drains
from rrotord import log
from rrotord.audit import Audit, Who, acting
from rrotord.config import Config
from rrotord.drain import Buffer, Collector, Drain, Glyphh, SendFailed, Sink, warning
from tests.fakes import FakeCollector

TOKEN = "tok_" + "r" * 40


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def config(tmp_path: Path, url: str, **kw: Any) -> Config:
    base = Config(
        tmp_path / "data",
        "self",
        url,
        0,
        0,
        None,
        Path(rrotor.__file__).parent,
        tmp_path / "run",
        runtime_id="00000000-0000-4000-8000-000000000001",
        node_id="rrotor-1",
        otel=(("OTEL_EXPORTER_OTLP_ENDPOINT", url),),
    )
    return dataclasses.replace(base, **kw)


@pytest.fixture
async def fake() -> AsyncIterator[FakeCollector]:
    f = FakeCollector()
    f.url = await f.start()  # type: ignore[attr-defined]
    yield f
    await f.close()


@pytest.fixture(autouse=True)
def quiet_root() -> Iterator[None]:
    """Each test's lines at INFO through the root logger, the node named; put back after."""
    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    root.setLevel(logging.INFO)
    log.identify(node_id="rrotor-1")
    yield
    root.setLevel(level)
    root.handlers[:] = handlers
    log.identify()


def bodies(fake: FakeCollector, who: str = "collector") -> list[str]:
    return [r["body"] for r in fake.logs(who) if not r["event_name"]]


async def test_logs_metrics_and_traces_reach_the_collector_with_rrotors_resource(tmp_path, fake):
    d = Drain(config(tmp_path, fake.url), runtime_id="rt-1")
    d.start()
    with log.bind(run_id="r1", model="m"), d.telemetry.span("rrotor.function"):
        logging.getLogger("rrotor.t").info("hello", extra={"n": 1})
    logging.getLogger("opentelemetry.exporter").warning("never fed back")
    d.telemetry.ended("m", "start", "done", 0.5)
    await d.tick()
    lines = fake.logs()
    assert [line["body"] for line in lines] == ["hello"]
    assert (lines[0]["node_id"], lines[0]["run_id"], lines[0]["model"], lines[0]["n"]) == ("rrotor-1", "r1", "m", 1)
    assert lines[0]["trace_id"] == fake.spans()[0]["trace_id"] != "" and lines[0]["severity"] == "INFO"
    assert [s["name"] for s in fake.spans()] == ["rrotor.function"]
    assert {"rrotor.runs", "rrotor.run.seconds", "rrotor.drain.dropped"} <= fake.metrics()
    resources = fake.resources()
    assert {r["service.instance.id"] for r in resources} == {"rrotor-1"}
    assert {r["rrotor.runtime.id"] for r in resources} == {"rt-1"} and resources[0]["service.name"] == "rrotor"
    view = d.view()
    assert view["drains"]["logs"]["state"] == "ok" and view["drains"]["logs"]["last_export_at"] is not None
    assert view["drains"]["audit"]["state"] == "waiting" and view["server_logs"] == {"on": False, "warning": None}
    await d.stop()
    assert d.handler not in logging.getLogger().handlers


async def test_a_down_endpoint_loses_only_the_oldest_logs_and_no_audit_event(tmp_path, fake):
    clock = Clock()
    c = config(tmp_path, fake.url, drain_buffer=5, drain_signals=("logs", "audit"))
    audit = Audit(c.data / "audit", "rrotor-1", tier=lambda: "enterprise")
    d = Drain(c, runtime_id=None, audit=audit, collector=Collector({"logs": f"{fake.url}/v1/logs"}, 0.5), clock=clock)
    d.start()
    fake.down = True
    for n in range(8):
        logging.getLogger("rrotor.t").info(f"line {n}")
    for n in range(3):
        audit.emit("run.call", n=n)
    await d.tick()
    logs = d.view()["drains"]["logs"]
    assert (logs["state"], logs["buffered"], logs["error"]) == ("failing", 5, "the collector did not take the batch")
    assert logs["dropped"] == 4  # three, and one more for the drop's own warning, logged into the same full buffer
    clock.now += 5
    await d.tick()  # the backoff is not over: nothing tried
    assert fake.got["collector"] == []
    fake.down = False
    clock.now += 10
    await d.tick()
    assert bodies(fake) == [
        "line 4",
        "line 5",
        "line 6",
        "line 7",
        "the drain dropped its oldest records: its endpoint is not taking them",
    ]
    audited = [r for r in fake.logs() if r["event_name"]]
    assert [(r["event_name"], r["n"]) for r in audited] == [("rrotor.audit.run.call", n) for n in range(3)]
    assert (c.data / "audit" / ".cursor").exists()
    fake.got["collector"].clear()
    await d.tick()
    assert [r for r in fake.logs() if r["event_name"]] == []  # the cursor moved on: none sent twice
    await d.stop()


async def test_server_side_logging_takes_the_token_and_leaves_out_clients_payloads_and_the_customers_headers(
    tmp_path, fake, monkeypatch
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-customer-key=s3cret-header")
    c = config(tmp_path, fake.url, server_logs=True, log_payloads=True)
    tier = ["enterprise"]
    audit = Audit(c.data / "audit", "rrotor-1", tier=lambda: tier[0], payloads=True)
    d = Drain(c, runtime_id="rt-1", audit=audit, token=lambda: TOKEN)
    d.start()
    caller = Who({"type": "key", "key_sha256": "ab" * 32}, "10.0.4.17", "rest")
    audit.emit("run.end", who=caller, payload={"inputs": {"q": "PAYLOAD-IN"}, "data": "PAYLOAD-OUT"})
    tier[0] = "pro"
    with acting(caller):
        audit.emit("run.call", who=caller)  # below Enterprise: a log line, which carries the client
    with d.telemetry.span("rrotor.function") as span:
        d.telemetry.payload(span, inputs={"q": "PAYLOAD-IN"})
    logging.getLogger("rrotor.t").info("a plain line")
    await d.tick()
    assert bodies(fake, "glyphh") == ["a plain line"]
    customer, glyphh = fake.raw("collector"), fake.raw("glyphh")
    for secret in (b"10.0.4.17", b"PAYLOAD-IN", b"PAYLOAD-OUT"):
        assert secret in customer and secret not in glyphh, secret
    assert {r["event_name"] for r in fake.logs("glyphh")} >= {"rrotor.audit.run.end", "rrotor.audit.run.call"}
    assert fake.spans("glyphh")[0]["name"] == "rrotor.function" and fake.metrics("glyphh")
    for _, _, headers in fake.got["glyphh"]:
        assert headers["Authorization"] == f"Bearer {TOKEN}" and headers["Content-Encoding"] == "gzip"
        assert "x-customer-key" not in {k.lower() for k in headers}
    assert all(h.get("x-customer-key") == "s3cret-header" for _, _, h in fake.got["collector"])
    view = d.view()["server_logs"]
    assert view["on"] and view["warning"] == warning(fake.url) and view["signals"]["logs"]["state"] == "ok"
    assert "(127.0.0.1)" in view["warning"] and "leave your infrastructure" in view["warning"]
    await d.stop()


async def test_glyphh_failing_is_shown_and_tried_again(tmp_path, fake):
    token: list[str | None] = [None]
    sender = Glyphh(fake.url, lambda: token[0], timeout=2)
    with pytest.raises(SendFailed, match="no runtime token"):
        await sender.send("logs", [])
    token[0] = TOKEN
    fake.down = True
    with pytest.raises(SendFailed, match="answered 503"):
        await sender.send("metrics", [])
    await sender.close()
    gone = Glyphh("http://127.0.0.1:9", lambda: TOKEN, timeout=2)
    with pytest.raises(SendFailed, match="ClientConnectorError"):
        await gone.send("traces", [])
    await gone.close()
    await Glyphh(fake.url, lambda: None).close()  # never opened: nothing to close


def test_a_full_buffer_drops_its_oldest_and_a_failed_batch_goes_back_in_front():
    b = Buffer(3)
    for n in range(5):
        b.add(n)
    assert (list(b.items), b.dropped) == ([2, 3, 4], 2)
    batch = b.take(2)
    b.add(5)
    b.give_back(batch)  # room for one: the newer of the two
    assert (list(b.items), b.dropped) == ([3, 4, 5], 3)
    batch = b.take(10)
    b.give_back(batch)
    assert (list(b.items), b.dropped, len(b)) == ([3, 4, 5], 3, 3)


class Refuses:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int]] = []
        self.down = True

    async def send(self, signal: str, batch: list[Any]) -> None:
        if self.down:
            raise SendFailed("down")
        self.sent.append((signal, len(batch)))

    async def close(self) -> None:
        pass


async def test_audit_events_wait_on_disk_and_a_lost_files_cursor_moves_on(tmp_path):
    audit = Audit(tmp_path / "audit", tier=lambda: "enterprise")
    sender = Refuses()
    sink = Sink("t", sender, ("audit",), 2, audit, tmp_path / "audit" / ".cursor")
    assert await sink.flush() is True  # nothing kept yet
    for n in range(3):
        audit.emit("run.call", n=n)
    assert await sink.flush() is False and not (tmp_path / "audit" / ".cursor").exists()
    sender.down = False
    assert await sink.flush() is True and sender.sent == [("audit", 3)]
    (tmp_path / "audit" / ".cursor").write_text('{"file": "2026-01-01.jsonl", "offset": 9}')
    (tmp_path / "audit" / "2026-01-02.jsonl").write_text("")
    for name in audit.days():
        if name != "2026-01-02.jsonl":
            (tmp_path / "audit" / name).unlink()
    assert await sink.flush() is True
    assert (tmp_path / "audit" / ".cursor").read_text() == '{"file":"2026-01-02.jsonl","offset":0}'


def test_drops_are_logged_at_most_once_a_minute(caplog):
    sink = Sink("t", Refuses(), ("logs",), 1)
    sink.take("logs", 1)
    sink.take("logs", 2)
    sink.take("metrics", 3)  # not taken here: dropped silently, not counted
    with caplog.at_level(logging.WARNING, logger="rrotor.drain"):
        sink.report(100.0)
        sink.take("logs", 3)
        sink.report(130.0)
        sink.report(160.0)
        sink.report(170.0)
    told = [r.__dict__["dropped"] for r in caplog.records]
    assert told == [{"logs": 1}, {"logs": 2}]
    assert sink.view()["metrics"] == {"state": "off"} and sink.view()["logs"]["dropped"] == 2


def test_a_line_that_cannot_be_read_is_not_sent(tmp_path, capsys):
    d = Drain(config(tmp_path, "http://127.0.0.1:9"), runtime_id=None)
    record = logging.makeLogRecord({"name": "rrotor", "msg": "%d", "args": ("x",)})
    d.handler.emit(record)
    assert d.collector is not None and len(d.collector.buffers["logs"]) == 0
    assert "Logging error" in capsys.readouterr().err


async def test_the_loop_survives_a_bad_round_and_the_last_round_is_bounded(tmp_path, caplog, monkeypatch):
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    d = Drain(config(tmp_path, "http://127.0.0.1:9"), runtime_id=None, sleep=sleep)

    async def broken() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(d, "tick", broken)
    with caplog.at_level(logging.ERROR, logger="rrotor.drain"), pytest.raises(asyncio.CancelledError):
        await d.run()
    await d.stop()
    assert sleeps == [drains.EVERY, drains.EVERY]
    assert [r.getMessage() for r in caplog.records] == ["the drain failed a round", "the drain failed its last round"]


async def test_no_metrics_read_sends_none(tmp_path, monkeypatch):
    sender = Refuses()
    sender.down = False
    d = Drain(config(tmp_path, "http://x:1", drain_signals=("metrics",)), runtime_id=None, collector=sender)
    monkeypatch.setattr(d.telemetry, "collect", lambda: None)
    await d.tick()
    assert sender.sent == []
    await d.stop()


def test_a_drain_is_wanted_only_with_somewhere_to_send(tmp_path):
    none = config(tmp_path, "https://api.glyphh.ai", otel=())
    assert not Drain.wanted(none)
    assert Drain.wanted(dataclasses.replace(none, server_logs=True))
    assert Drain.wanted(dataclasses.replace(none, otel=(("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://c/t"),)))
    only_traces = dataclasses.replace(none, otel=(("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://c/t"),))
    assert not Drain.wanted(dataclasses.replace(only_traces, drain_signals=("logs", "audit")))
    off = drains.view(None)
    assert off["drains"]["logs"] == {"state": "off"} and off["server_logs"] == {"on": False, "warning": None}
    d = Drain(dataclasses.replace(none, server_logs=True), runtime_id=None)
    assert d.collector is None and d.view()["drains"]["traces"] == {"state": "off"}
    assert warning("https://api.glyphh.ai").startswith("Server-side logging is on: this rrotor's logs, metrics, ")

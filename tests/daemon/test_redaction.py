"""Nothing redacted leaks (docs/specs/cluster.md, 7): secrets, tokens, inputs, outputs and `ctx.log`'s fields fed
through the API, MCP, a deploy and a refusal, and every output grepped: stdout, the audit store, the customer's
collector and Glyphh. With RROTOR_LOG_PAYLOADS=on, inputs and results reach the customer's drain only."""

from __future__ import annotations

import base64
import dataclasses
import io
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rrotord import log
from rrotord.api.app import make
from rrotord.audit import Audit
from rrotord.drain import Drain
from tests.daemon.api.conftest import PORT, services
from tests.fakes import FakeCollector, install, manifest

LOCAL_TOKEN = "tok_LOCAL_" + "q" * 30
RUNTIME_TOKEN = "tok_RUNTIME_" + "w" * 30
REPO_TOKEN = "ghp_REPOTOKEN" + "e" * 30
INPUT = "INPUT-SECRET-7f3a"
OUTPUT = "OUTPUT-SECRET-91bc"
CTX = "CTXLOG-SECRET-2d4e"
BODY = "BODY-SECRET-5a6b"
PROGRAM = f"""
from rrotor import Result

def start(ctx, inputs):
    ctx.log("working", detail="{CTX}")
    return Result.done({{"answer": "{OUTPUT}", "echo": inputs}})
"""


@pytest.fixture
def stdout() -> Iterator[io.StringIO]:
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(log.JsonFormatter())
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield out
    root.removeHandler(handler)
    root.setLevel(level)


@pytest.fixture
async def fake() -> AsyncIterator[FakeCollector]:
    f = FakeCollector()
    f.url = await f.start()  # type: ignore[attr-defined]
    yield f
    await f.close()


async def exercise(tmp_path: Path, short_dir: Path, fake: FakeCollector, payloads: bool) -> Path:
    url = fake.url  # type: ignore[attr-defined]
    s = services(tmp_path, short_dir, LOCAL_TOKEN)
    s.config = dataclasses.replace(
        s.config,
        server=url,
        otel=(("OTEL_EXPORTER_OTLP_ENDPOINT", url),),
        server_logs=True,
        log_payloads=payloads,
    )
    s.audit = s.runner.audit = Audit(tmp_path / "audit", tier=lambda: "enterprise", payloads=payloads)
    s.drain = Drain(s.config, runtime_id="rt-1", audit=s.audit, token=lambda: RUNTIME_TOKEN)
    s.runner.telemetry = s.drain.telemetry
    s.drain.start()
    install(s.programs, manifest("sample"), PROGRAM)
    client = TestClient(TestServer(make(s)), headers={"Host": f"127.0.0.1:{PORT}"})
    await client.start_server()
    auth = {"Authorization": f"Bearer {LOCAL_TOKEN}"}
    try:
        res = await client.post("/v1/runs", json={"program": "sample", "inputs": {"secret": INPUT}}, headers=auth)
        assert (await res.json())["data"]["answer"] == OUTPUT
        rpc = {"name": "program_call", "arguments": {"program": "sample", "inputs": {"secret": INPUT}}}
        await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": rpc}, headers=auth)
        repo = {"repo": "owner/missing", "commit": "a" * 40, "token": REPO_TOKEN}
        assert (await client.post("/v1/programs/deploy", json=repo, headers=auth)).status >= 400
        bad = base64.b64encode(BODY.encode()).decode()
        await client.post("/v1/programs/deploy", json={"bundle": bad, "commit": "a" * 40}, headers=auth)
        await client.post("/v1/runs", json={"program": "nope", "inputs": {"secret": INPUT}}, headers=auth)
        await client.get("/v1/programs", headers={"Authorization": f"Bearer {RUNTIME_TOKEN}"})
        logging.getLogger("rrotor.t").info(f"a careless line: Bearer {RUNTIME_TOKEN}", extra={"token": REPO_TOKEN})
    finally:
        await client.close()
        await s.drain.stop()
        await s.runner.shutdown()
    return tmp_path / "audit"


SECRETS = (LOCAL_TOKEN, RUNTIME_TOKEN, REPO_TOKEN, INPUT, OUTPUT, CTX, BODY)


async def test_nothing_redacted_reaches_any_output(tmp_path, short_dir, fake, stdout):
    folder = await exercise(tmp_path, short_dir, fake, payloads=False)
    kept = "".join(p.read_text() for p in folder.glob("*.jsonl"))
    outputs = {
        "stdout": stdout.getvalue(),
        "audit": kept,
        "collector": fake.raw("collector").decode(errors="replace"),
        "glyphh": fake.raw("glyphh").decode(errors="replace"),
    }
    assert all(outputs.values()), outputs
    assert "run.call" in outputs["audit"] and "rrotor.audit.run.end" in outputs["collector"]
    for where, text in outputs.items():
        for secret in SECRETS:
            assert secret not in text, (where, secret)
    run_logs = "".join(p.read_text() for p in (tmp_path / "data" / "runs").glob("*/log.jsonl"))
    assert CTX in run_logs  # a run's own lines stay in its log on /data, as before


async def test_payloads_go_to_the_customers_drain_only_when_asked(tmp_path, short_dir, fake, stdout):
    folder = await exercise(tmp_path, short_dir, fake, payloads=True)
    collector = fake.raw("collector").decode(errors="replace")
    assert INPUT in collector and OUTPUT in collector
    assert INPUT in "".join(p.read_text() for p in folder.glob("*.jsonl"))  # the customer's own disk
    for where, text in (("glyphh", fake.raw("glyphh").decode(errors="replace")), ("stdout", stdout.getvalue())):
        for secret in SECRETS:
            assert secret not in text, (where, secret)
    for secret in (LOCAL_TOKEN, RUNTIME_TOKEN, REPO_TOKEN, CTX, BODY):
        assert secret not in collector, secret

"""What a run leaves behind besides its Result: an audit event for every call, continue and cancel, and one for its
end, refused calls included; its id, model, version and function on every line logged while it is handled; with a
drain, spans for the sandbox's start and the function, its end counted, payloads only when asked."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan

from rrotord import log
from rrotord.audit import Audit, Who, acting
from rrotord.errors import Conflict, NotFound
from rrotord.otel import Telemetry, resource, sampler
from rrotord.runner.record import Run
from tests.daemon.runner.test_engine import WAIT_RESULT, Env

DONE = "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.done({'echo': inputs})\n"


@pytest.fixture
async def env(tmp_path, short_dir):
    e = Env(tmp_path / "data", short_dir)
    e.runner.audit = Audit(tmp_path / "audit", "rrotor-1", tier=lambda: "enterprise", payloads=True)
    yield e
    await e.runner.shutdown()


def kinds(env: Env) -> list[tuple[str, Any]]:
    assert env.runner.audit is not None
    return [(e["kind"], e["outcome"]["status"]) for e in env.runner.audit.read(None, 100)[0]]


async def test_a_call_and_its_end_are_audit_events_naming_the_caller(env):
    env.deploy(DONE)
    caller = Who({"type": "key", "key_sha256": "ab" * 32}, "10.0.4.17", "mcp")
    with acting(caller):
        run = await env.runner.call(model="sample", function="start", inputs={"q": 1})
    events = env.runner.audit.read(None, 10)[0]
    assert [(e["kind"], e["outcome"]) for e in events] == [
        ("run.end", {"status": "done", "code": None}),
        ("run.call", {"status": "done", "code": None}),
    ]
    end, call = events
    assert (call["actor"], call["client"], call["via"]) == (caller.actor, "10.0.4.17", "mcp")
    assert (call["model"], call["version"], call["function"], call["run_id"]) == ("sample", 1, "start", run.id)
    assert end["inputs"] == '{"q":1}' and end["data"] == '{"echo":{"q":1}}' and "inputs" not in call


async def test_continue_cancel_and_refusals_are_audit_events_too(env):
    env.deploy(WAIT_RESULT)
    run = await env.runner.call(model="sample", function="start", inputs={})
    await env.runner.resume(run.id, "2026-10-19")
    waiting = await env.runner.call(model="sample", function="start", inputs={})
    await env.runner.cancel(waiting.id)
    with pytest.raises(NotFound):
        await env.runner.call(model="sample", function="nope", inputs={})
    with pytest.raises(NotFound):
        await env.runner.resume("6f1c2d4e-0000-4000-8000-000000000009", 1)
    with pytest.raises(Conflict):
        await env.runner.cancel(waiting.id)
    assert kinds(env) == [
        ("run.call", "needs_input"),
        ("run.end", "done"),
        ("run.continue", "done"),
        ("run.call", "needs_input"),
        ("run.end", "cancelled"),
        ("run.cancel", "cancelled"),
        ("run.call", "refused"),
        ("run.continue", "refused"),
        ("run.cancel", "refused"),
    ]
    refused = env.runner.audit.read(None, 100)[0][-3:]
    assert [e["outcome"]["code"] for e in refused] == ["E_FUNCTION_MISSING", "E_RUN_MISSING", "E_RUN_STATE"]
    assert refused[0]["actor"] == {"type": "rrotor"}


async def test_a_failed_end_names_its_code(env):
    env.deploy(WAIT_RESULT)
    run = await env.runner.call(model="sample", function="start", inputs={})
    env.models.remove(env.models.get("sample", 1))
    await env.runner.resume(run.id, 1)
    end = [e for e in env.runner.audit.read(None, 100)[0] if e["kind"] == "run.end"][-1]
    assert end["outcome"] == {"status": "failed", "code": "E_VERSION_MISSING"}


async def test_lines_logged_while_a_run_is_handled_name_it(env):
    seen: list[dict[str, Any]] = []

    async def notify(run: Run) -> None:
        seen.append(log.fields(logging.makeLogRecord({"name": "rrotor", "msg": "told"})))

    env.runner.notify = notify
    env.deploy(WAIT_RESULT)
    run = await env.runner.call(model="sample", function="start", inputs={})
    await env.runner.resume(run.id, 1)
    other = await env.runner.call(model="sample", function="start", inputs={})
    await env.runner.cancel(other.id)
    assert [(s["run_id"], s["model"], s["version"], s["function"]) for s in seen] == [
        (run.id, "sample", 1, "start"),
        (run.id, "sample", 1, "start"),
        (other.id, "sample", 1, "start"),
        (other.id, "sample", 1, "start"),
    ]
    assert "run_id" not in log.fields(logging.makeLogRecord({"msg": "after"}))


@pytest.mark.parametrize("payloads", [False, True])
async def test_with_a_drain_the_sandbox_start_and_the_function_are_spans_and_the_end_is_counted(env, payloads):
    spans: list[ReadableSpan] = []
    t = Telemetry(resource({}, node_id="n", runtime_id=None, mode="local"), sampler({}), spans.append, payloads)
    env.runner.telemetry = t
    env.deploy(DONE)
    run = await env.runner.call(model="sample", function="start", inputs={"q": 1})
    assert [s.name for s in spans] == ["rrotor.sandbox.start", "rrotor.function"]
    function = dict(spans[1].attributes or {})
    assert (function["rrotor.run_id"], function["rrotor.model"], function["rrotor.status"]) == (
        run.id,
        "sample",
        "done",
    )
    assert ("rrotor.inputs" in function, "rrotor.data" in function) == (payloads, payloads)
    data = t.collect()
    assert data is not None
    runs = [m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics if m.name == "rrotor.runs"]
    point: Any = runs[0].data.data_points[0]
    assert (point.value, dict(point.attributes)) == (1, {"model": "sample", "function": "start", "status": "done"})
    t.shutdown()

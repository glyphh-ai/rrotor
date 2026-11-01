"""OpenTelemetry's pieces: the resource every signal carries, the sampler from its variables, log lines and audit
events as OTLP records, what is stripped before Glyphh, the tracer and the meter."""

from __future__ import annotations

import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, sampling

from rrotord import VERSION, otel
from rrotord.otel import Telemetry, attributes_of, audit_record, decoded, encoded, flat, log_record, resource, sampler


def res() -> Resource:
    return resource({}, node_id="rrotor-1", runtime_id=None, mode="self")


def test_the_resource_is_rrotors_identity_over_the_customers_attributes():
    given = {
        "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=prod, team=a%20b,service.instance.id=x,junk",
        "OTEL_SERVICE_NAME": "rrotor-eu",
    }
    r = resource(given, node_id="rrotor-1", runtime_id="rt-1", mode="self").attributes
    assert dict(r) == {
        "deployment.environment": "prod",
        "team": "a b",
        "service.name": "rrotor-eu",
        "service.version": VERSION,
        "service.instance.id": "rrotor-1",
        "rrotor.node.role": "standalone",
        "rrotor.mode": "self",
        "rrotor.runtime.id": "rt-1",
    }
    assert res().attributes["service.name"] == "rrotor" and "rrotor.runtime.id" not in res().attributes
    named = resource({"OTEL_RESOURCE_ATTRIBUTES": "service.name=mine"}, node_id="n", runtime_id=None, mode="local")
    assert named.attributes["service.name"] == "mine"


@pytest.mark.parametrize(
    ("name", "arg", "description"),
    [
        (None, None, "ParentBased{root:AlwaysOnSampler"),
        ("always_on", None, "AlwaysOnSampler"),
        ("always_off", None, "AlwaysOffSampler"),
        ("traceidratio", "0.25", "TraceIdRatioBased{0.25}"),
        ("parentbased_always_off", None, "ParentBased{root:AlwaysOffSampler"),
        ("parentbased_traceidratio", "0.5", "ParentBased{root:TraceIdRatioBased{0.5}"),
    ],
)
def test_the_sampler_is_the_variables(name, arg, description):
    given = {k: v for k, v in (("OTEL_TRACES_SAMPLER", name), ("OTEL_TRACES_SAMPLER_ARG", arg)) if v is not None}
    chosen = sampler(given)
    assert chosen.get_description().startswith(description)
    assert isinstance(chosen, sampling.Sampler)


def test_fields_become_flat_masked_attributes():
    token = "tok_" + "a" * 30
    got = flat(
        {"actor": {"type": "key", "sub": None}, "n": 2, "ok": True, "x": 1.5, "list": [1], "m": f"Bearer {token}"}
    )
    assert got == {"actor.type": "key", "n": 2, "ok": True, "x": 1.5, "list": "[1]", "m": "Bearer ***"}


def record(name: str, message: str, level: str = "INFO") -> logging.LogRecord:
    return logging.makeLogRecord({"name": name, "levelname": level, "msg": message, "created": 1.5})


def test_a_log_line_becomes_a_log_record_and_an_audit_line_is_named():
    plain = log_record(record("rrotor", "up", "NOTICE"), {"at": "x", "message": "up", "program": "m"}, res())
    assert plain.log_record.body == "up" and plain.log_record.event_name is None
    assert str(plain.log_record.severity_number) == "SeverityNumber.INFO" and dict(
        plain.log_record.attributes or {}
    ) == {"program": "m"}
    assert plain.log_record.timestamp == 1_500_000_000
    audit = log_record(record("rrotor.audit", "run.call", "WARNING"), {"message": "run.call"}, res())
    assert (
        audit.log_record.event_name == "rrotor.audit.run.call"
        and str(audit.log_record.severity_number) == "SeverityNumber.WARN"
    )


def test_a_kept_audit_event_becomes_a_named_record():
    event = {"at": "2026-10-19T09:00:00Z", "kind": "keys.changed", "actor": {"type": "server"}, "client": None}
    got = audit_record(event, res()).log_record
    assert (got.event_name, got.body) == ("rrotor.audit.keys.changed", "keys.changed")
    assert dict(got.attributes or {}) == {"kind": "keys.changed", "actor.type": "server"}
    assert got.timestamp == 1_792_400_400_000_000_000
    assert audit_record({"kind": "odd"}, res()).log_record.timestamp is None


def test_what_goes_to_glyphh_has_no_client_and_no_payloads():
    event = {"kind": "run.end", "client": "10.0.4.17", "inputs": "{}", "data": "{}", "program": "m"}
    batch = [audit_record(event, res())]
    customer = attributes_of(
        decoded("logs", encoded("logs", batch, False)).resource_logs[0].scope_logs[0].log_records[0].attributes
    )
    glyphh = attributes_of(
        decoded("logs", encoded("logs", batch, True)).resource_logs[0].scope_logs[0].log_records[0].attributes
    )
    assert {"client", "inputs", "data"} <= set(customer) and set(glyphh) == {"kind", "program"}


async def test_the_tracer_and_the_meter():
    spans: list[ReadableSpan] = []
    t = Telemetry(res(), sampler({}), spans.append, payloads=True)
    parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    with (
        t.request_span("POST", "/v1/runs", {"traceparent": parent}) as request,
        t.span("rrotor.function", {"rrotor.program": "m"}) as span,
    ):
        t.payload(span, inputs={"a": 1})
        assert trace.get_current_span() is span
    assert [s.name for s in spans] == ["rrotor.function", "POST /v1/runs"]
    assert spans[1].context.trace_id == int("0af7651916cd43dd8448eb211c80319c", 16) and request.is_recording() is False
    assert spans[0].attributes == {"rrotor.program": "m", "rrotor.inputs": '{"a":1}'}
    quiet = Telemetry(res(), sampler({}), spans.append)
    with quiet.span("x") as span:
        quiet.payload(span, inputs={"a": 1})
    assert "rrotor.inputs" not in (spans[-1].attributes or {})
    t.gauge("rrotor.g", lambda: [(2.0, {"k": "v"})])
    t.gauge("rrotor.c", lambda: [(3, {})], counter=True)
    t.ended("m", "start", "done", 0.25)
    data = t.collect()
    assert data is not None
    names = {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert names == {"rrotor.runs", "rrotor.run.seconds", "rrotor.g", "rrotor.c"}
    t.shutdown()
    quiet.shutdown()


def test_attributes_of_an_empty_value():
    from opentelemetry.proto.common.v1.common_pb2 import KeyValue

    assert attributes_of([KeyValue(key="k")]) == {"k": None}
    assert {"client", "inputs", "data"} <= otel.NEVER_TO_GLYPHH

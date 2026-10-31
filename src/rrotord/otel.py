"""OpenTelemetry for the drains (docs/specs/cluster.md, 7): the resource every signal carries, the sampler, log lines
and audit events as OTLP log records, the metrics' instruments, the tracer for a span per API request and a run's
sandbox start and function, and the redaction of what goes to Glyphh. The sending is rrotord/drain.py's.

Every batch goes as its OTLP export request, serialized (`encoded`): this node's own records with the resource as it
stands when the batch goes (a node that joins a cluster becomes `rrotor.node.role` head or follower, with its
`rrotor.cluster.id`), and, on a cluster's head, the records its followers sent (`Wire`, `unwrapped`), each with its
own node's resource exactly as that node sent it.

Nothing here is on unless a drain or server-side logging is: without one, rrotor has no tracer or meter at all."""

from __future__ import annotations

import contextlib
import json
import logging
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.exporter.otlp.proto.common._internal import _encode_resource
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import KeyValue
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricsData
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider, sampling
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Span, SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from rrotord import VERSION
from rrotord.audit import clip, moment
from rrotord.log import mask

SCOPE = InstrumentationScope("rrotord", VERSION)
SEVERITY = {
    "DEBUG": SeverityNumber.DEBUG,
    "INFO": SeverityNumber.INFO,
    "WARNING": SeverityNumber.WARN,
    "ERROR": SeverityNumber.ERROR,
    "CRITICAL": SeverityNumber.FATAL,
}
LINE_ONLY = ("at", "level", "message", "trace_id", "span_id")  # carried by the record itself, not as attributes
NEVER_TO_GLYPHH = frozenset({"client", "inputs", "data", "rrotor.inputs", "rrotor.data"})
PROPAGATOR = TraceContextTextMapPropagator()

Observe = Callable[[], Iterable[tuple[float, Mapping[str, str]]]]


def resource(
    otel: Mapping[str, str],
    *,
    node_id: str,
    runtime_id: str | None,
    mode: str,
    role: str = "standalone",
    cluster_id: str | None = None,
) -> Resource:
    """`OTEL_RESOURCE_ATTRIBUTES` (key=value, comma-separated, percent-encoded), then rrotor's own: service.name
    (`OTEL_SERVICE_NAME`, default rrotor), service.version, service.instance.id (the node id), rrotor.runtime.id,
    rrotor.cluster.id (none while standalone), rrotor.node.role (standalone, head or follower) and rrotor.mode."""
    attributes: dict[str, str] = {}
    for pair in otel.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        key, eq, value = pair.partition("=")
        if eq and key.strip():
            attributes[key.strip()] = urllib.parse.unquote(value.strip())
    attributes["service.name"] = otel.get("OTEL_SERVICE_NAME") or attributes.get("service.name") or "rrotor"
    attributes |= {
        "service.version": VERSION,
        "service.instance.id": node_id,
        "rrotor.node.role": role,
        "rrotor.mode": mode,
    }
    if runtime_id:
        attributes["rrotor.runtime.id"] = runtime_id
    if cluster_id:
        attributes["rrotor.cluster.id"] = cluster_id
    return Resource(attributes)


def sampler(otel: Mapping[str, str]) -> sampling.Sampler:
    """`OTEL_TRACES_SAMPLER` (default parentbased_always_on) and its `OTEL_TRACES_SAMPLER_ARG` (config.py checks
    both)."""
    name = otel.get("OTEL_TRACES_SAMPLER", "parentbased_always_on")
    ratio = float(otel.get("OTEL_TRACES_SAMPLER_ARG", "1.0"))
    root: sampling.Sampler = {
        "always_on": sampling.ALWAYS_ON,
        "always_off": sampling.ALWAYS_OFF,
    }.get(name.removeprefix("parentbased_")) or sampling.TraceIdRatioBased(ratio)
    return sampling.ParentBased(root) if name.startswith("parentbased_") else root


def flat(value: Any, prefix: str = "") -> dict[str, Any]:
    """Attributes from a line's or an event's fields: nested objects as dotted keys, lists as JSON, strings masked,
    nothing for None."""
    out: dict[str, Any] = {}
    if isinstance(value, Mapping):
        for key, inner in value.items():
            out |= flat(inner, f"{prefix}.{key}" if prefix else str(key))
    elif value is None:
        pass
    elif isinstance(value, bool | int | float):
        out[prefix] = value
    elif isinstance(value, str):
        out[prefix] = mask(value)
    else:
        out[prefix] = mask(json.dumps(value, default=str))
    return out


def log_record(record: logging.LogRecord, line: Mapping[str, Any], res: Resource) -> ReadableLogRecord:
    """A log line (rrotord/log.py's fields) as an OTLP log record, in the span it was logged in; an audit event
    logged below Enterprise is named `rrotor.audit.<kind>`."""
    audit = record.name == "rrotor.audit"
    return ReadableLogRecord(
        LogRecord(
            timestamp=int(record.created * 1e9),
            observed_timestamp=int(record.created * 1e9),
            context=otel_context.get_current(),
            severity_text=record.levelname,
            severity_number=SEVERITY.get(record.levelname, SeverityNumber.INFO),
            body=line["message"],
            attributes=flat({k: v for k, v in line.items() if k not in LINE_ONLY}),
            event_name=f"rrotor.audit.{record.getMessage()}" if audit else None,
        ),
        res,
        SCOPE,
    )


def audit_record(event: Mapping[str, Any], res: Resource) -> ReadableLogRecord:
    """A kept audit event as an OTLP log record: `event.name = rrotor.audit.<kind>`, its fields as attributes. An
    event another node kept (the cluster's audit store, exported by its head) carries that node's identity:
    service.instance.id its node id, rrotor.node.role follower."""
    node = event.get("node_id")
    if isinstance(node, str) and node != res.attributes.get("service.instance.id"):
        res = Resource({**res.attributes, "service.instance.id": node, "rrotor.node.role": "follower"})
    at = int(moment(str(event.get("at")), "at").timestamp() * 1e9) if event.get("at") else None
    return ReadableLogRecord(
        LogRecord(
            timestamp=at,
            observed_timestamp=at,
            severity_text="INFO",
            severity_number=SeverityNumber.INFO,
            body=str(event.get("kind")),
            attributes=flat({k: v for k, v in event.items() if k != "at"}),
            event_name=f"rrotor.audit.{event.get('kind')}",
        ),
        res,
        SCOPE,
    )


def _strip(attributes: Any) -> None:
    kept = [kv for kv in attributes if kv.key not in NEVER_TO_GLYPHH]
    del attributes[:]
    attributes.extend(kept)


@dataclass(frozen=True, slots=True)
class Wire:
    """One record another node sent, as OTLP: a log record or a span with its resource and scope, or a metrics
    reading whole (its ResourceMetrics, `scope` None). Exported as it came, so it keeps its node's identity."""

    resource: Any
    scope: Any
    item: Any


def unwrapped(signal: str, body: bytes) -> list[Wire]:
    """An export request another node sent, as its records: what a head buffers for its destinations."""
    request = decoded("logs" if signal == "audit" else signal, body)
    if signal == "metrics":
        return [Wire(rm.resource, None, rm) for rm in request.resource_metrics]
    if signal == "traces":
        return [
            Wire(rs.resource, ss.scope, s) for rs in request.resource_spans for ss in rs.scope_spans for s in ss.spans
        ]
    return [
        Wire(rl.resource, sl.scope, r) for rl in request.resource_logs for sl in rl.scope_logs for r in sl.log_records
    ]


def _wired(request: Any, signal: str, wires: list[Wire]) -> None:
    """Records another node sent, added to a request: each under its own resource and scope, consecutive ones that
    share them grouped."""
    for wire in wires:
        if signal == "metrics":
            request.resource_metrics.append(wire.item)
            continue
        groups = request.resource_spans if signal == "traces" else request.resource_logs
        if not groups or groups[-1].resource != wire.resource:
            groups.add().resource.CopyFrom(wire.resource)
        scopes = groups[-1].scope_spans if signal == "traces" else groups[-1].scope_logs
        if not scopes or scopes[-1].scope != wire.scope:
            scopes.add().scope.CopyFrom(wire.scope)
        (scopes[-1].spans if signal == "traces" else scopes[-1].log_records).append(wire.item)


def encoded(signal: str, batch: list[Any], for_glyphh: bool, res: Resource | None = None) -> bytes:
    """A batch as its OTLP export request, serialized; for Glyphh, without client addresses or payloads. This node's
    spans and metrics carry `res`, the resource as it stands now (their providers keep the one they started with);
    its log records were made with it. Records from other nodes keep theirs."""
    wires = [item for item in batch if isinstance(item, Wire)]
    local = [item for item in batch if not isinstance(item, Wire)]
    if signal == "metrics":
        request: Any = ExportMetricsServiceRequest()
        for data in local:
            request.resource_metrics.extend(encode_metrics(data).resource_metrics)
        groups: Any = request.resource_metrics
    elif signal == "traces":
        request = encode_spans(local)
        groups = request.resource_spans
    else:
        request = encode_logs(local)
        groups = ()
    if res is not None:
        for group in groups:
            group.resource.CopyFrom(_encode_resource(res))
    _wired(request, signal, wires)
    if for_glyphh and signal != "metrics":
        records: Iterable[Any] = (
            (s for rs in request.resource_spans for ss in rs.scope_spans for s in ss.spans)
            if signal == "traces"
            else (r for rl in request.resource_logs for sl in rl.scope_logs for r in sl.log_records)
        )
        for each in records:
            _strip(each.attributes)
    return bytes(request.SerializeToString())


def decoded(signal: str, body: bytes) -> Any:
    """An export request read back (the tests' fake collector, and anyone checking what went)."""
    kinds: dict[str, Any] = {
        "logs": ExportLogsServiceRequest,
        "metrics": ExportMetricsServiceRequest,
        "traces": ExportTraceServiceRequest,
    }
    request = kinds[signal]()
    request.ParseFromString(body)
    return request


def attributes_of(values: Iterable[KeyValue]) -> dict[str, Any]:
    """OTLP attributes as a dict of their plain values."""
    out: dict[str, Any] = {}
    for kv in values:
        which = kv.value.WhichOneof("value")
        out[kv.key] = getattr(kv.value, which) if which else None
    return out


class Collect(SpanProcessor):
    """Each ended span handed on."""

    def __init__(self, take: Callable[[ReadableSpan], None]) -> None:
        self.take = take

    def on_end(self, span: ReadableSpan) -> None:
        self.take(span)


class Telemetry:
    """The tracer and the meter: `rrotor.runs` (counter: program, function, status), `rrotor.run.seconds` (histogram),
    and gauges added by what they watch (`gauge`): the link's state, the cluster's term and lag, the drain's drops."""

    def __init__(
        self, res: Resource, sample: sampling.Sampler, take: Callable[[ReadableSpan], None], payloads: bool = False
    ) -> None:
        self.payloads = payloads
        self.tracers = TracerProvider(sampler=sample, resource=res, shutdown_on_exit=False)
        self.tracers.add_span_processor(Collect(take))
        self.tracer = self.tracers.get_tracer("rrotord", VERSION)
        self.reader = InMemoryMetricReader()
        self.meters = MeterProvider(metric_readers=[self.reader], resource=res, shutdown_on_exit=False)
        self.meter = self.meters.get_meter("rrotord", VERSION)
        self.runs = self.meter.create_counter("rrotor.runs", unit="{run}", description="Runs ended")
        self.seconds = self.meter.create_histogram("rrotor.run.seconds", unit="s", description="A run's seconds")

    def gauge(self, name: str, observe: Observe, counter: bool = False, unit: str = "1") -> None:
        def callback(_: CallbackOptions) -> Iterable[Observation]:
            return [Observation(value, dict(attributes)) for value, attributes in observe()]

        if counter:
            self.meter.create_observable_counter(name, [callback], unit=unit)
        else:
            self.meter.create_observable_gauge(name, [callback], unit=unit)

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        parent: Any = None,
    ) -> Iterator[Span]:
        given = flat(attributes or {})
        with self.tracer.start_as_current_span(name, context=parent, kind=kind, attributes=given) as span:
            yield span

    def request_span(
        self, method: str, route: str, headers: Mapping[str, str]
    ) -> contextlib.AbstractContextManager[Span]:
        """A span for an API request, a child of the caller's `traceparent` when it sends one."""
        return self.span(
            f"{method} {route}",
            {"http.request.method": method, "http.route": route},
            kind=SpanKind.SERVER,
            parent=PROPAGATOR.extract(carrier=dict(headers)),
        )

    def payload(self, span: Span, **given: Any) -> None:
        """Inputs and Result.data on a run's span, 4 KB each, with RROTOR_LOG_PAYLOADS=on only."""
        if self.payloads:
            span.set_attributes({f"rrotor.{k}": clip(v) for k, v in given.items()})

    def ended(self, program: str, function: str, status: str, seconds: float) -> None:
        self.runs.add(1, {"program": program, "function": function, "status": status})
        self.seconds.record(seconds, {"program": program, "function": function})

    def collect(self) -> MetricsData | None:
        return self.reader.get_metrics_data()

    def shutdown(self) -> None:
        self.tracers.shutdown()
        self.meters.shutdown()

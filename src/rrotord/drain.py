"""The drains (docs/specs/cluster.md, 7): what rrotor logs, measures and traces, and its audit events, sent by OTLP
over HTTP with protobuf to the customer's own collector, never through Glyphh; and, opt-in, to Glyphh too.

- **The customer's collector** (`Collector`): OpenTelemetry's own OTLP/HTTP exporters, so its settings are
  OpenTelemetry's (`OTEL_EXPORTER_OTLP_*`). No endpoint, no drain; `RROTOR_DRAIN_SIGNALS` picks what goes. A
  standalone node is its own head. In a cluster, `RROTOR_DRAIN_AT=head` (the default): a follower sends its signals
  to its head (`Head`), which exports them with its own, each with its node's resource (rrotord/aggregate.py);
  `node`: each node exports itself.
- **Glyphh** (`Glyphh`, `RROTOR_SERVER_LOGS=on`): the same signals to `{RROTOR_SERVER}/v1/otlp/v1/{logs,metrics,
  traces}` with the runtime token, gzipped, never with client addresses or payloads, and never with the customer's
  OTLP headers or certificates (it is not their collector). Refused with the link off (config.py).
- **Buffer and drop:** logs, metrics and traces wait in memory, `RROTOR_DRAIN_BUFFER` records per signal per
  destination, sent every 5 s in batches, a failure tried again with backoff (5 s doubling to a minute); when full
  the **oldest** are dropped, counted (`rrotor.drain.dropped`, the health) and logged at most once a minute.
- **Audit events are never dropped** (Enterprise): each destination reads the audit store from its own cursor
  (`/data/audit/.cursor`, `.cursor-glyphh`) and moves it only when a batch is taken, so an endpoint down for a day
  gets the day's events when it is back, as long as retention still holds them. Below Enterprise they are log
  records and follow the log rule."""

from __future__ import annotations

import asyncio
import base64
import collections
import gzip
import logging
import threading
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import aiohttp
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan

from rrotord.audit import Audit
from rrotord.cluster.transport import ClusterError
from rrotord.config import SIGNALS, Config
from rrotord.log import masked
from rrotord.otel import Telemetry, audit_record, encoded, log_record, resource, sampler, unwrapped
from rrotord.store import read_json, write_json

log = logging.getLogger("rrotor.drain")
EVERY = 5.0
BACKOFF_MAX = 60.0
BATCH = 512
DROPS_EVERY = 60.0
STOP_WAIT = 15.0  # the last round's bound: an endpoint that is down does not hold up a stop
QUIET = ("opentelemetry", "urllib3", "requests")  # the exporters' own loggers: never fed back into a drain
WARNING = (
    "Server-side logging is on: this rrotor's logs, metrics, traces and audit events are sent to Glyphh ({host}) and "
    "leave your infrastructure. Set RROTOR_SERVER_LOGS=off to keep them here."
)


def warning(server: str) -> str:
    return WARNING.format(host=urllib.parse.urlparse(server).hostname)


class SendFailed(Exception):
    """A batch the destination did not take, and why."""


class Buffer:
    """Records waiting for one signal's destination: at most `size`, the oldest dropped (and counted) past it."""

    def __init__(self, size: int) -> None:
        self.size, self.dropped = size, 0
        self.items: collections.deque[Any] = collections.deque()
        self.lock = threading.Lock()  # logging can happen on any thread

    def __len__(self) -> int:
        return len(self.items)

    def add(self, item: Any) -> None:
        with self.lock:
            if len(self.items) >= self.size:
                self.items.popleft()
                self.dropped += 1
            self.items.append(item)

    def take(self, count: int) -> list[Any]:
        with self.lock:
            return [self.items.popleft() for _ in range(min(count, len(self.items)))]

    def give_back(self, batch: list[Any]) -> None:
        """A batch that did not go, put back in front: as much of its newest as there is room for, the rest dropped
        (they are the oldest)."""
        with self.lock:
            room = max(self.size - len(self.items), 0)
            kept = batch[len(batch) - room :] if room < len(batch) else batch
            self.dropped += len(batch) - len(kept)
            self.items.extendleft(reversed(kept))


class Sender(Protocol):
    async def send(self, signal: str, body: bytes) -> None: ...
    async def close(self) -> None: ...


class Collector:
    """The customer's collector, through OpenTelemetry's OTLP/HTTP exporters (protobuf): each signal's endpoint as
    config.py resolves it; headers, certificates, compression, timeout and retries from their `OTEL_EXPORTER_OTLP_*`
    variables, as OpenTelemetry reads them. Each batch goes as the export request `encoded` made (a head's holds its
    followers' records beside its own), through the exporter's own HTTP client; it blocks, so on a thread."""

    def __init__(self, endpoints: dict[str, str], timeout: float | None = None) -> None:
        kinds: dict[str, Any] = {"logs": OTLPLogExporter, "metrics": OTLPMetricExporter, "traces": OTLPSpanExporter}
        self.exporters = {signal: kinds[signal](endpoint=url, timeout=timeout) for signal, url in endpoints.items()}

    async def send(self, signal: str, body: bytes) -> None:
        exporter = self.exporters["logs" if signal == "audit" else signal]
        result = await asyncio.to_thread(exporter._client.export, body)
        if not result.success:
            raise SendFailed("the collector did not take the batch")

    async def close(self) -> None:
        for exporter in self.exporters.values():
            exporter.shutdown()


class Glyphh:
    """Glyphh's receiver for server-side logging: `POST {server}/v1/otlp/v1/<signal>`, OTLP protobuf, gzipped, the
    runtime token as the bearer; the batch encoded without client addresses and payloads."""

    def __init__(self, server: str, token: Callable[[], str | None], timeout: float = 10.0) -> None:
        self.base, self.token, self.timeout = server.rstrip("/") + "/v1/otlp/v1/", token, timeout
        self.session: aiohttp.ClientSession | None = None

    async def send(self, signal: str, body: bytes) -> None:
        token = self.token()
        if not token:
            raise SendFailed("no runtime token")
        kind = "logs" if signal == "audit" else signal
        headers = {
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "gzip",
            "Authorization": f"Bearer {token}",
        }
        if self.session is None:
            self.session = aiohttp.ClientSession()
        try:
            async with self.session.post(
                self.base + kind,
                data=gzip.compress(body),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as res:
                status = res.status
        except (aiohttp.ClientError, OSError, TimeoutError) as err:
            raise SendFailed(type(err).__name__) from None
        if status != 200:
            raise SendFailed(f"the server answered {status}")

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()


class Head:
    """A follower's way to its head (`RROTOR_DRAIN_AT=head`, and server-side logging, which a cluster's head sends for
    it): each batch as its export request, in a `telemetry` frame over the follower's connection, with where the head
    is to send it on (`to`: collector, glyphh). Taken once the head holds it; with no head, the batch waits here."""

    def __init__(
        self, channel: Callable[[], Any], to: dict[str, list[str]], term: Callable[[], int | None] = lambda: None
    ) -> None:
        self.channel, self.to, self.term = channel, to, term  # each frame carries the follower's term (fencing)

    async def send(self, signal: str, body: bytes) -> None:
        channel = self.channel()
        if channel is None:
            raise SendFailed("no head")
        try:
            await channel.request(
                "telemetry", signal=signal, to=self.to[signal], body=base64.b64encode(body).decode(), term=self.term()
            )
        except (ClusterError, TimeoutError, ConnectionError, aiohttp.ClientError) as err:
            raise SendFailed(f"the head did not take the batch: {getattr(err, 'code', type(err).__name__)}") from None

    async def close(self) -> None:
        pass


Store = Callable[[], tuple[Audit, Path] | None]


class Sink:
    """One destination: a buffer per signal it takes, the audit store it reads now (from its own cursor), its
    backoff. Each batch is encoded here, with the resource as it stands (`res`), stripped for Glyphh (`strip`)."""

    def __init__(
        self,
        name: str,
        sender: Sender,
        signals: tuple[str, ...],
        size: int,
        store: Store = lambda: None,
        res: Callable[[], Resource] = Resource.get_empty,
        strip: bool = False,
    ) -> None:
        self.name, self.sender, self.store, self.res, self.strip = name, sender, store, res, strip
        self.buffers = {signal: Buffer(size) for signal in signals}
        self.states: dict[str, dict[str, Any]] = {s: {"state": "waiting", "last_export_at": None} for s in signals}
        self.wait, self.next_at = EVERY, 0.0
        self.reported, self.reported_at = 0, -DROPS_EVERY

    def take(self, signal: str, item: Any) -> None:
        if signal in self.buffers:
            self.buffers[signal].add(item)

    @property
    def dropped(self) -> int:
        return sum(b.dropped for b in self.buffers.values())

    async def _send(self, signal: str, batch: list[Any]) -> bool:
        body = encoded("logs" if signal == "audit" else signal, batch, self.strip, self.res())
        try:
            await self.sender.send(signal, body)
        except SendFailed as err:
            self.states[signal] = {**self.states[signal], "state": "failing", "error": str(err)}
            return False
        self.states[signal] = {"state": "ok", "last_export_at": time.time()}
        return True

    async def flush(self) -> bool:
        """What each buffer held when the round began, sent batch by batch (what is logged meanwhile waits for the
        next round), then the audit store from the cursor; stops at the first failure (the batch put back) and
        answers whether everything went."""
        for signal, buffer in self.buffers.items():
            waiting = len(buffer)
            while waiting > 0:  # nothing else takes from a buffer, so it holds at least this many
                batch = buffer.take(min(BATCH, waiting))
                waiting -= len(batch)
                if not await self._send(signal, batch):
                    buffer.give_back(batch)
                    return False
        store = self.store() if "audit" in self.buffers else None
        if store is not None:
            return await self._store(*store)
        return True

    async def _store(self, audit: Audit, path: Path) -> bool:
        saved = read_json(path)
        cursor = (str(saved["file"]), int(saved["offset"])) if isinstance(saved, dict) else None
        while True:
            events, after = audit.read(cursor, BATCH)
            if events and not await self._send("audit", [audit_record(e, self.res()) for e in events]):
                return False
            if after is not None and after != cursor:
                write_json(path, {"file": after[0], "offset": after[1]})
                cursor = after
            if not events:
                return True

    def report(self, now: float) -> None:
        """What was dropped, logged at most once a minute."""
        dropped = self.dropped
        if dropped > self.reported and now - self.reported_at >= DROPS_EVERY:
            log.warning(
                "the drain dropped its oldest records: its endpoint is not taking them",
                extra={"drain": self.name, "dropped": {s: b.dropped for s, b in self.buffers.items()}},
            )
            self.reported, self.reported_at = dropped, now

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for signal in SIGNALS:
            if signal not in self.buffers:
                out[signal] = {"state": "off"}
                continue
            buffer = self.buffers[signal]
            out[signal] = {**self.states[signal], "buffered": len(buffer), "dropped": buffer.dropped}
        return out


class Handler(logging.Handler):
    """Every log line, as an OTLP log record, to each destination taking logs now (an audit event logged below
    Enterprise to those taking audit)."""

    def __init__(self, drain: Drain) -> None:
        super().__init__()
        self.drain = drain

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(QUIET):
            return
        try:
            item = log_record(record, masked(record), self.drain.res)
        except Exception:  # noqa: BLE001  (logging's own rule: a handler never raises into the caller)
            self.handleError(record)
            return
        signal = "audit" if record.name == "rrotor.audit" else "logs"
        for sink in self.drain.route():
            sink.take(signal, item)


Place = Callable[[], tuple[str | None, str]]


class Drain:
    """The drain of one node, and of a cluster at its head.

    - **Standalone, or a cluster's head:** this node's signals to the customer's collector and, opted in, to Glyphh;
      a head also takes what its followers send (`received`) and sends it on as it came. With `RROTOR_DRAIN_AT=head`
      its audit events are the cluster's audit store (rrotord/aggregate.py), each event with its node's identity.
    - **A follower:** with `RROTOR_DRAIN_AT=head` (the default) its logs, metrics and traces go to the head (`head`),
      buffered here while there is none, the oldest dropped and counted as for any endpoint; with `node` it exports
      them to the collector itself, its own audit store with them. Server-side logging always goes by the head: a
      follower never sends to Glyphh. Its kept audit events reach the cluster's store by rrotord/aggregate.py, never
      dropped, whatever the drain."""

    def __init__(
        self,
        config: Config,
        *,
        runtime_id: str | None,
        audit: Audit | None = None,
        token: Callable[[], str | None] = lambda: None,
        collector: Sender | None = None,
        glyphh: Sender | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        place: Place = lambda: (None, "standalone"),
        upstream: Callable[[], Any] | None = None,
        term: Callable[[], int | None] = lambda: None,
        head: Sender | None = None,
        gathered: Audit | None = None,
    ) -> None:
        self.config, self.clock, self.sleep, self.place = config, clock, sleep, place
        self.runtime_id, self.audit, self.gathered = runtime_id, audit, gathered
        self._res: tuple[tuple[str | None, str], Resource] | None = None
        chosen = tuple(s for s in config.drain_signals if config.endpoint("logs" if s == "audit" else s))
        self.collector: Sink | None = None
        self.glyphh: Sink | None = None
        self.head: Sink | None = None
        if chosen:
            endpoints = {s: url for s in ("logs", "metrics", "traces") if (url := config.endpoint(s))}
            self.collector = Sink(
                "collector",
                collector or Collector(endpoints),
                chosen,
                config.drain_buffer,
                lambda: self._store("collector", ".cursor"),
                lambda: self.res,
            )
        if config.server_logs:
            self.glyphh = Sink(
                "glyphh",
                glyphh or Glyphh(config.server, token),
                SIGNALS,
                config.drain_buffer,
                lambda: self._store("glyphh", ".cursor-glyphh"),
                lambda: self.res,
                strip=True,
            )
        to = {
            signal: [
                *(["collector"] if signal in chosen and config.drain_at == "head" else []),
                *(["glyphh"] if config.server_logs else []),
            ]
            for signal in SIGNALS
        }
        upward = tuple(signal for signal, where in to.items() if where)
        if config.cluster and upward and (head is not None or upstream is not None):
            self.head = Sink(
                "head",
                head or Head(upstream or (lambda: None), to, term),
                upward,
                config.drain_buffer,
                res=lambda: self.res,
            )
        self.sinks = [s for s in (self.collector, self.glyphh, self.head) if s is not None]
        self.telemetry = Telemetry(self.res, sampler(config.otel_env), self._span, config.log_payloads)
        self.telemetry.gauge(
            "rrotor.drain.dropped",
            lambda: [(b.dropped, {"drain": s.name, "signal": n}) for s in self.sinks for n, b in s.buffers.items()],
            counter=True,
        )
        self.handler = Handler(self)

    @staticmethod
    def wanted(config: Config) -> bool:
        """Whether there is anywhere to send anything: a collector for a chosen signal, or Glyphh."""
        return config.server_logs or any(config.endpoint("logs" if s == "audit" else s) for s in config.drain_signals)

    @property
    def res(self) -> Resource:
        """The resource as it stands: the node's role and cluster as they are now."""
        place = self.place()
        if self._res is None or self._res[0] != place:
            cluster_id, role = place
            made = resource(
                self.config.otel_env,
                node_id=self.config.node_id,
                runtime_id=self.runtime_id,
                mode=self.config.mode,
                role=role,
                cluster_id=cluster_id,
            )
            self._res = (place, made)
        return self._res[1]

    def route(self) -> list[Sink]:
        """Where this node's signals go now: a follower's to its head (and to the collector itself with
        RROTOR_DRAIN_AT=node), never to Glyphh; anyone else's to the collector and Glyphh."""
        if self.place()[1] != "follower":
            return [s for s in (self.collector, self.glyphh) if s is not None]
        mine = self.collector if self.config.drain_at == "node" else None
        return [s for s in (self.head, mine) if s is not None]

    def _store(self, sink: str, cursor: str) -> tuple[Audit, Path] | None:
        """The audit store a destination reads now: on a follower, its own only when it exports itself
        (RROTOR_DRAIN_AT=node, never to Glyphh); on a head, the cluster's (Glyphh's always, the collector's with
        RROTOR_DRAIN_AT=head); else this node's own."""
        role = self.place()[1]
        if role == "follower" and (sink == "glyphh" or self.config.drain_at == "head"):
            return None
        if role == "head" and self.gathered is not None and (sink == "glyphh" or self.config.drain_at == "head"):
            return self.gathered, self.gathered.folder / cursor
        if self.audit is None:
            return None
        return self.audit, self.audit.folder / cursor

    def received(self, signal: str, body: bytes, to: list[str]) -> int:
        """A follower's batch, at the head: its records buffered for each destination named that this head has,
        exported as they came (each keeps its node's resource), the oldest dropped and counted past the buffer.
        Answers how many records it held."""
        wires = unwrapped(signal, body)
        for name in to:
            sink = {"collector": self.collector, "glyphh": self.glyphh}.get(name)
            if sink is not None:
                for wire in wires:
                    sink.take(signal, wire)
        return len(wires)

    def _span(self, span: ReadableSpan) -> None:
        for sink in self.route():
            sink.take("traces", span)

    async def tick(self) -> None:
        """Every 5 s: the metrics read, then each destination whose backoff is over sent to (one no longer in use
        sends what it still holds)."""
        data = self.telemetry.collect()
        if data is not None:
            for sink in self.route():
                sink.take("metrics", data)
        now = self.clock()
        for sink in self.sinks:
            if now >= sink.next_at:
                ok = await sink.flush()
                sink.wait = EVERY if ok else min(sink.wait * 2, BACKOFF_MAX)
                sink.next_at = now + (0.0 if ok else sink.wait)
            sink.report(now)

    def start(self) -> None:
        logging.getLogger().addHandler(self.handler)

    async def run(self) -> None:
        while True:
            await self.sleep(EVERY)
            try:
                await self.tick()
            except Exception:
                log.exception("the drain failed a round")

    async def stop(self) -> None:
        """One last round, whatever the backoff, then the exporters closed."""
        logging.getLogger().removeHandler(self.handler)
        for sink in self.sinks:
            sink.next_at = 0.0
        try:
            await asyncio.wait_for(self.tick(), STOP_WAIT)
        except Exception:
            log.exception("the drain failed its last round")
        for sink in self.sinks:
            await sink.sender.close()
        self.telemetry.shutdown()

    def view(self) -> dict[str, Any]:
        """The health's `drains` (per signal, where this node sends it: the customer's collector, or its head,
        `via: head`) and `server_logs`."""
        off = {signal: {"state": "off"} for signal in SIGNALS}
        follower = self.place()[1] == "follower"
        upward = self.head.view() if self.head is not None else off
        upward = {s: v if v["state"] == "off" else {**v, "via": "head"} for s, v in upward.items()}
        if follower and self.config.drain_at == "head":
            drains = upward
        else:
            drains = self.collector.view() if self.collector is not None else off
        server: dict[str, Any] = {"on": False, "warning": None}
        if self.glyphh is not None:
            signals = upward if follower else self.glyphh.view()
            server = {"on": True, "warning": warning(self.config.server), "signals": signals}
        return {"drains": drains, "server_logs": server}


def view(drain: Drain | None) -> dict[str, Any]:
    if drain is not None:
        return drain.view()
    return {"drains": {signal: {"state": "off"} for signal in SIGNALS}, "server_logs": {"on": False, "warning": None}}

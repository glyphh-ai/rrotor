"""rrotord's settings, from its environment (set by whoever starts the container: the hosted
provisioning, a self-hosting package, or a local caller). Locally no secret comes through here: the runtime token is
handed in over the local API, and the local token arrives only as its SHA-256. A hosted rrotor's runtime id and token
are in its machine's secrets, and the parts it ships with are installed from the image at boot. A self-hosted one
(`self`) is a hosted one its owner runs: the same, at the one exact host it is given, with Glyphh's server as the
default and, air-gapped, no link at all.

What it logs and where that goes (docs/specs/cluster.md, 7 and 10): the audit store's retention, the drain (the
OpenTelemetry variables, as OpenTelemetry defines them: only OTLP over HTTP with protobuf), opt-in server-side
logging (never with the link off) and payloads in the customer's drain."""

from __future__ import annotations

import os
import re
import socket
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rrotord.errors import RrotorError

HEX64 = re.compile(r"^[0-9a-f]{64}$")
NODE_ID = re.compile(r"^[a-z0-9-]{1,63}$")
MODES = ("local", "hosted", "self", "doorman")
SIGNALS = ("logs", "metrics", "traces", "audit")
SAMPLERS = (
    "always_on",
    "always_off",
    "traceidratio",
    "parentbased_always_on",
    "parentbased_always_off",
    "parentbased_traceidratio",
)
PROTOCOLS = tuple(
    f"OTEL_EXPORTER_OTLP_{signal}PROTOCOL" for signal in ("", "LOGS_", "METRICS_", "TRACES_")
)  # each, when set, http/protobuf
ENDPOINTS = tuple(f"OTEL_EXPORTER_OTLP_{signal}ENDPOINT" for signal in ("", "LOGS_", "METRICS_", "TRACES_"))
SERVER = "https://api.glyphh.ai"  # Glyphh's server: a self-hosted rrotor's default


@dataclass(frozen=True, slots=True)
class Config:
    data: Path
    mode: str
    server: str  # the Cloud's base, http(s)://host[:port]
    local_port: int  # where the local API listens in the container
    host_port: int  # where it is published on the host's loopback (for the Host check)
    local_token_sha256: str | None
    sdk: Path  # the rrotor SDK package, mounted into every sandbox
    run_dir: Path  # sockets and scratch, on tmpfs
    release_key: str | None = None  # Glyphh's release public key: what signs a part
    runtime_id: str | None = None  # hosted: the runtime this machine is, from its secrets
    runtime_token: str | None = None  # hosted: its token for the link, from its secrets
    builtin_parts: Path = Path("/opt/parts-builtin")  # parts the image carries, installed at boot
    public_domain: str = "glyphh.app"  # hosted and doorman: the runtimes' addresses are <machine>.<domain>
    machine_id: str | None = None  # hosted: this machine's id (Fly's FLY_MACHINE_ID), its own address
    keys_sha256: tuple[str, ...] = ()  # hosted: the runtime keys' SHA-256s at first; the Cloud's list after
    public_host: str | None = None  # self (and hosted in development): the one exact Host it answers at
    link: bool = True  # off: no link and no heartbeat (air-gapped, self): the licence key from its variable or file
    licence_env: tuple[tuple[str, str], ...] = ()  # (variable, text): GLYPHH_LICENSE_KEY and its alias RROTOR_LICENCE
    node_id: str = "rrotor"  # this node's name (RROTOR_NODE_ID, else the hostname), sent in the heartbeat
    audit_retention_days: int = 90  # days of audit kept (Enterprise)
    audit_max_mb: int = 1024  # the audit folder's size, the oldest day deleted first
    drain_signals: tuple[str, ...] = SIGNALS  # what the drain sends
    drain_at: str = "head"  # head or node; a standalone node is its own head, so it exports itself
    drain_buffer: int = 10000  # records held per signal while an endpoint is down, the oldest dropped past it
    server_logs: bool = False  # on: the same signals to Glyphh too, never payloads or client addresses
    log_payloads: bool = False  # on: inputs and results in the customer's drain (run spans, run.end)
    otel: tuple[tuple[str, str], ...] = ()  # the OTEL_* variables as set: the drain's endpoint, headers, sampler

    @property
    def public(self) -> bool:
        """Reached by anyone with a key or a Glyphh sign-in, not on loopback only: hosted and self."""
        return self.mode in ("hosted", "self")

    @property
    def otel_env(self) -> dict[str, str]:
        return dict(self.otel)

    def endpoint(self, signal: str) -> str | None:
        """The customer's collector for a signal (`logs`, `metrics`, `traces`), as OpenTelemetry resolves it: the
        signal's own endpoint as given, else the general one with `/v1/<signal>`; None: no drain for it."""
        env = self.otel_env
        own = env.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
        if own:
            return own
        base = env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        return f"{base.rstrip('/')}/v1/{signal}" if base else None

    @property
    def link_url(self) -> str:
        return re.sub(r"^http", "ws", self.server.rstrip("/")) + "/v1/runtime/link"


def hostname() -> str:
    """The node's default name: the host's first label, lowercased, anything but a-z, 0-9 and - as -."""
    label = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".")[0].lower())[:63]
    return label or "rrotor"


def on_off(env: Mapping[str, str], name: str, default: str = "off") -> bool:
    value = env.get(name, default).strip().lower() or default
    if value not in ("on", "off"):
        raise RrotorError(f"{name} is on or off.", code="E_CONFIG")
    return value == "on"


def count(env: Mapping[str, str], name: str, default: int) -> int:
    """A whole number of at least 1."""
    text = env.get(name, "").strip()
    if not text:
        return default
    if not text.isdigit() or int(text) < 1:
        raise RrotorError(f"{name} is a whole number, 1 or more.", code="E_CONFIG")
    return int(text)


def otel(env: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """The OpenTelemetry variables, checked: OTLP over HTTP with protobuf only (gRPC needs grpcio: a collector takes
    HTTP and forwards gRPC), endpoints that are http(s) URLs, a sampler rrotor knows and its ratio."""
    found = tuple(sorted((k, v.strip()) for k, v in env.items() if k.startswith("OTEL_") and v.strip()))
    given = dict(found)
    for name in PROTOCOLS:
        protocol = given.get(name, "http/protobuf").lower()
        if protocol == "grpc":
            raise RrotorError(
                f"{name}=grpc: OTLP over gRPC is not offered. Use http/protobuf (port 4318); a collector takes HTTP "
                "and forwards gRPC if the backend wants it.",
                code="E_CONFIG",
            )
        if protocol != "http/protobuf":
            raise RrotorError(f"{name} is http/protobuf.", code="E_CONFIG")
    for name in ENDPOINTS:
        if name in given and not re.match(r"^https?://[^/\s]+(/\S*)?$", given[name]):
            raise RrotorError(f"{name} is the collector's URL, http(s)://host[:port][/path].", code="E_CONFIG")
    sampler = given.get("OTEL_TRACES_SAMPLER", "parentbased_always_on")
    if sampler not in SAMPLERS:
        raise RrotorError(f"OTEL_TRACES_SAMPLER is one of {', '.join(SAMPLERS)}.", code="E_CONFIG")
    if "OTEL_TRACES_SAMPLER_ARG" in given:
        try:
            ratio = float(given["OTEL_TRACES_SAMPLER_ARG"])
        except ValueError:
            ratio = -1.0
        if not 0 <= ratio <= 1:
            raise RrotorError("OTEL_TRACES_SAMPLER_ARG is a ratio, 0 to 1.", code="E_CONFIG")
    return found


def signals(env: Mapping[str, str]) -> tuple[str, ...]:
    text = env.get("RROTOR_DRAIN_SIGNALS", "").strip()
    if not text:
        return SIGNALS
    chosen = tuple(dict.fromkeys(s.strip().lower() for s in text.split(",") if s.strip()))
    if not chosen or any(s not in SIGNALS for s in chosen):
        raise RrotorError(
            "RROTOR_DRAIN_SIGNALS is a comma-separated list of logs, metrics, traces and audit.", code="E_CONFIG"
        )
    return chosen


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    mode = env.get("RROTOR_MODE", "local")
    if mode not in MODES:
        raise RrotorError("RROTOR_MODE must be local, hosted, self or doorman.", code="E_CONFIG")
    default = {"doorman": "http://doorman.invalid", "self": SERVER}.get(mode, "")
    server = env.get("RROTOR_SERVER") or default
    if not re.match(r"^https?://[^/\s]+$", server.rstrip("/")):
        raise RrotorError("RROTOR_SERVER must be the Glyphh server's address, http(s)://host[:port].", code="E_CONFIG")
    digest = env.get("RROTOR_LOCAL_TOKEN_SHA256") or None
    if digest is not None and not HEX64.match(digest):
        raise RrotorError("RROTOR_LOCAL_TOKEN_SHA256 must be 64 lowercase hex digits.", code="E_CONFIG")
    if mode == "local" and digest is None:
        raise RrotorError("A local rrotor needs RROTOR_LOCAL_TOKEN_SHA256 (its mod's local token).", code="E_CONFIG")
    runtime_id, runtime_token = env.get("RROTOR_RUNTIME_ID") or None, env.get("RROTOR_RUNTIME_TOKEN") or None
    if mode == "hosted" and (runtime_id is None) != (runtime_token is None):
        raise RrotorError("A hosted rrotor takes RROTOR_RUNTIME_ID and RROTOR_RUNTIME_TOKEN together.", code="E_CONFIG")
    if mode == "self" and (runtime_id is None or runtime_token is None):
        raise RrotorError(
            "A self-hosted rrotor needs RROTOR_RUNTIME_ID and RROTOR_RUNTIME_TOKEN (from registering it on the "
            "platform).",
            code="E_CONFIG",
        )
    if mode == "self":
        try:
            runtime_id = str(uuid.UUID(runtime_id))
        except ValueError:
            raise RrotorError("RROTOR_RUNTIME_ID is the runtime's UUID.", code="E_CONFIG") from None
    machine_id = env.get("RROTOR_MACHINE_ID") or env.get("FLY_MACHINE_ID") or None
    public_host = (env.get("RROTOR_PUBLIC_HOST") or "").strip().lower().rstrip(".") or None
    if mode == "self" and (public_host is None or not re.match(r"^[0-9a-z.-]+$", public_host)):
        raise RrotorError(
            "A self-hosted rrotor needs RROTOR_PUBLIC_HOST: the exact host it answers at (rrotor.example.com).",
            code="E_CONFIG",
        )
    link = env.get("RROTOR_LINK", "on").strip().lower()
    if link not in ("on", "off"):
        raise RrotorError("RROTOR_LINK is on or off.", code="E_CONFIG")
    if mode == "hosted" and machine_id is None and public_host is None:
        raise RrotorError(
            "A hosted rrotor needs its machine id (FLY_MACHINE_ID), or RROTOR_PUBLIC_HOST in development.",
            code="E_CONFIG",
        )
    node_id = env.get("RROTOR_NODE_ID") or None
    if node_id is not None and not NODE_ID.match(node_id):
        raise RrotorError("RROTOR_NODE_ID is this node's name: 1 to 63 of a-z, 0-9 and -.", code="E_CONFIG")
    node_id = node_id or hostname()
    drain_at = env.get("RROTOR_DRAIN_AT", "head").strip().lower() or "head"
    if drain_at not in ("head", "node"):
        raise RrotorError("RROTOR_DRAIN_AT is head or node.", code="E_CONFIG")
    server_logs = on_off(env, "RROTOR_SERVER_LOGS")
    if server_logs and link != "on":
        raise RrotorError(
            "RROTOR_SERVER_LOGS=on needs the link: an air-gapped rrotor (RROTOR_LINK=off) sends nothing to Glyphh.",
            code="E_CONFIG",
        )
    keys = tuple(k.strip().lower() for k in env.get("RROTOR_KEYS_SHA256", "").split(",") if k.strip())
    if any(not HEX64.match(k) for k in keys):
        raise RrotorError("RROTOR_KEYS_SHA256 is a comma-separated list of SHA-256 hex digests.", code="E_CONFIG")
    import rrotor

    port = int(env.get("RROTOR_LOCAL_PORT", "7777"))
    return Config(
        data=Path(env.get("RROTOR_DATA", "/data")),
        mode=mode,
        server=server.rstrip("/"),
        local_port=port,
        host_port=int(env.get("RROTOR_HOST_PORT", str(port))),
        local_token_sha256=digest,
        sdk=Path(rrotor.__file__).parent,
        run_dir=Path(env.get("RROTOR_RUN_DIR", "/tmp/rrotor")),  # noqa: S108  (tmpfs in the container)
        release_key=env.get("RROTOR_RELEASE_PUBLIC_KEY") or None,
        runtime_id=runtime_id,
        runtime_token=runtime_token,
        builtin_parts=Path(env.get("RROTOR_BUILTIN_PARTS", "/opt/parts-builtin")),
        public_domain=env.get("RROTOR_PUBLIC_DOMAIN", "glyphh.app").strip().lower().rstrip("."),
        machine_id=machine_id.lower() if machine_id else None,
        keys_sha256=keys,
        public_host=public_host,
        link=link == "on",
        licence_env=tuple(
            (name, env[name].strip()) for name in ("GLYPHH_LICENSE_KEY", "RROTOR_LICENCE") if env.get(name, "").strip()
        ),
        node_id=node_id,
        audit_retention_days=count(env, "RROTOR_AUDIT_RETENTION_DAYS", 90),
        audit_max_mb=count(env, "RROTOR_AUDIT_MAX_MB", 1024),
        drain_signals=signals(env),
        drain_at=drain_at,
        drain_buffer=count(env, "RROTOR_DRAIN_BUFFER", 10000),
        server_logs=server_logs,
        log_payloads=on_off(env, "RROTOR_LOG_PAYLOADS"),
        otel=otel(env),
    )

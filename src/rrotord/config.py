"""rrotord's settings, from its environment (set by whoever starts the container: the hosted
provisioning, a self-hosting package, or a local caller). Locally no secret comes through here: the runtime token is
handed in over the local API, and the local token arrives only as its SHA-256. A hosted rrotor's runtime id and token
are in its machine's secrets, and the parts it ships with are installed from the image at boot. A self-hosted one
(`self`) is a hosted one its owner runs: the same, at the one exact host it is given, with Glyphh's server as the
default and, air-gapped, no link at all.

What it logs and where that goes (docs/specs/cluster.md, 7 and 10): the audit store's retention, the drain (the
OpenTelemetry variables, as OpenTelemetry defines them: only OTLP over HTTP with protobuf), opt-in server-side
logging (never with the link off) and payloads in the customer's drain.

The cluster (docs/specs/cluster.md, 4, 6 and 10): `RROTOR_CLUSTER=on` (a self-hosted rrotor only), how the nodes find
each other (a static `RROTOR_CLUSTER_PEERS` or a Kubernetes headless Service's `RROTOR_CLUSTER_DNS`, one of them), the
listener's port (never the API's) and the address the others reach it at, the one secret its mutual TLS comes from
(and the previous one while a new one rolls out), the bootstrap head, the log kept for catch-up and how long a member
may go unseen. Any of them malformed stops rrotor at start with E_CONFIG, naming it, the cluster on or not."""

from __future__ import annotations

import base64
import binascii
import os
import re
import socket
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
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
CLUSTER_PORT = 7778
SECRET_MIN = 32  # the cluster secret's bytes, at least
HOST = re.compile(r"^(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)$")
SIZES = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30}
SPANS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


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
    builtin_clis: Path = Path("/usr/local/lib/rrotor-clis")  # the vendors' CLIs the image carries, deployed at boot
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
    cluster: bool = False  # on: join or make a cluster (and only with a key whose cluster_sync_enabled is true)
    cluster_peers: tuple[str, ...] = ()  # host:port of every member, the same list on every node
    cluster_dns: str | None = None  # a name whose A/AAAA records are the members (a headless Service)
    cluster_port: int = CLUSTER_PORT  # the cluster listener, node to node only: never the API's port
    cluster_advertise: str | None = None  # host:port the others reach this node at; None: <node id>:<port>
    cluster_secrets: tuple[bytes, ...] = field(default=(), repr=False)  # the secret, then the previous one if set
    cluster_head: str | None = None  # the node that makes the cluster at bootstrap; None: the lowest node id
    cluster_log_keep: int = 1 << 30  # bytes of replication log kept for catch-up (replication: not built yet)
    cluster_evict_after: float = 24 * 3600.0  # seconds a member may go unseen before the head removes it

    @property
    def public(self) -> bool:
        """Reached by anyone with a key or a Glyphh sign-in, not on loopback only: hosted and self."""
        return self.mode in ("hosted", "self")

    @property
    def address(self) -> str:
        """This rrotor's address as a browser reaches it: its one public host (self), its machine's (hosted), or
        the host's loopback (local)."""
        if self.public_host is not None:
            return f"https://{self.public_host}"
        if self.mode == "hosted" and self.machine_id:
            return f"https://{self.machine_id}.{self.public_domain}"
        return f"http://127.0.0.1:{self.host_port}"

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
    def advertise(self) -> str:
        """How the other nodes reach this one: RROTOR_CLUSTER_ADVERTISE, else `<node id>:<port>`."""
        return self.cluster_advertise or f"{self.node_id}:{self.cluster_port}"

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


def address(text: str, name: str) -> str:
    """host:port, the host a name or an address (an IPv6 one in brackets), the port 1 to 65535."""
    host, _, port = text.strip().rpartition(":")
    if not HOST.match(host) or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise RrotorError(f"{name} is host:port (rrotor-0:7778).", code="E_CONFIG")
    return f"{host}:{int(port)}"


def secret(env: Mapping[str, str], name: str) -> bytes | None:
    """The cluster secret: base64 (standard or URL-safe, padded or not) of 32 bytes or more."""
    text = env.get(name, "").strip()
    if not text:
        return None
    try:
        raw = base64.b64decode(text.replace("-", "+").replace("_", "/") + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) < SECRET_MIN:
        raise RrotorError(
            f"{name} is {SECRET_MIN} or more random bytes in base64 (openssl rand -base64 32).", code="E_CONFIG"
        )
    return raw


def amount(env: Mapping[str, str], name: str, units: dict[str, int], default: float, example: str) -> float:
    """A number with a unit (`1g`, `24h`): the number times the unit's size; at least 1."""
    text = env.get(name, "").strip().lower()
    if not text:
        return default
    match = re.match(r"^(\d+)([a-z]?)$", text)
    if match is None or match.group(2) not in units or int(match.group(1)) < 1:
        raise RrotorError(f"{name} is a whole number and a unit, as {example}.", code="E_CONFIG")
    return float(int(match.group(1)) * units[match.group(2)])


def cluster(env: Mapping[str, str], mode: str, node_id: str, local_port: int) -> dict[str, object]:
    """The cluster's settings, each checked whether the cluster is on or not."""
    on = on_off(env, "RROTOR_CLUSTER")
    if on and mode != "self":
        raise RrotorError("RROTOR_CLUSTER=on is for a self-hosted rrotor (RROTOR_MODE=self).", code="E_CONFIG")
    port_text = env.get("RROTOR_CLUSTER_PORT", "").strip() or str(CLUSTER_PORT)
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise RrotorError("RROTOR_CLUSTER_PORT is a port, 1 to 65535.", code="E_CONFIG")
    port = int(port_text)
    if on and port == local_port:
        raise RrotorError("RROTOR_CLUSTER_PORT is not the API's port: the cluster has its own.", code="E_CONFIG")
    peers = tuple(
        address(p, "RROTOR_CLUSTER_PEERS") for p in env.get("RROTOR_CLUSTER_PEERS", "").split(",") if p.strip()
    )
    dns = env.get("RROTOR_CLUSTER_DNS", "").strip().lower().rstrip(".") or None
    if dns is not None and not HOST.match(dns):
        raise RrotorError(
            "RROTOR_CLUSTER_DNS is a DNS name (rrotor-headless.rrotor.svc.cluster.local).", code="E_CONFIG"
        )
    if on and (not peers) == (dns is None):
        raise RrotorError(
            "A cluster needs one of RROTOR_CLUSTER_PEERS (host:port,...) and RROTOR_CLUSTER_DNS (a headless Service).",
            code="E_CONFIG",
        )
    advertise = env.get("RROTOR_CLUSTER_ADVERTISE", "").strip()
    current, previous = secret(env, "RROTOR_CLUSTER_SECRET"), secret(env, "RROTOR_CLUSTER_SECRET_PREVIOUS")
    if on and current is None:
        raise RrotorError(
            "A cluster needs RROTOR_CLUSTER_SECRET: 32 or more random bytes in base64, the same on every node.",
            code="E_CONFIG",
        )
    if previous is not None and current is None:
        raise RrotorError("RROTOR_CLUSTER_SECRET_PREVIOUS goes with RROTOR_CLUSTER_SECRET.", code="E_CONFIG")
    head = env.get("RROTOR_CLUSTER_HEAD", "").strip() or None
    if head is not None and not NODE_ID.match(head):
        raise RrotorError("RROTOR_CLUSTER_HEAD is a node id: 1 to 63 of a-z, 0-9 and -.", code="E_CONFIG")
    return {
        "cluster": on,
        "cluster_peers": peers,
        "cluster_dns": dns,
        "cluster_port": port,
        "cluster_advertise": address(advertise, "RROTOR_CLUSTER_ADVERTISE") if advertise else None,
        "cluster_secrets": tuple(s for s in (current, previous) if s is not None),
        "cluster_head": head,
        "cluster_log_keep": int(amount(env, "RROTOR_CLUSTER_LOG_KEEP", SIZES, 1 << 30, "1g or 512m")),
        "cluster_evict_after": amount(env, "RROTOR_CLUSTER_EVICT_AFTER", SPANS, 24 * 3600.0, "24h or 90m"),
    }


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
        builtin_clis=Path(env.get("RROTOR_BUILTIN_CLIS", "/usr/local/lib/rrotor-clis")),
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
        **cluster(env, mode, node_id, port),  # type: ignore[arg-type]
    )

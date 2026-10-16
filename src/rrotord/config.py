"""rrotord's settings, from its environment (set by whoever starts the container: the mod, or the hosted
provisioning). Locally no secret comes through here: the runtime token is handed in over the local API, and the
local token arrives only as its SHA-256. A hosted rrotor has no mod to hand anything in: its runtime id and token
are in its machine's secrets, and the parts it ships with are installed from the image at boot."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rrotord.errors import RrotorError

HEX64 = re.compile(r"^[0-9a-f]{64}$")


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
    licence_key: str | None = None  # Glyphh's licence public key: what signs the plan's licence
    runtime_id: str | None = None  # hosted: the runtime this machine is, from its secrets
    runtime_token: str | None = None  # hosted: its token for the link, from its secrets
    builtin_parts: Path = Path("/opt/parts-builtin")  # parts the image carries, installed at boot
    public_domain: str = "glyphh.app"  # hosted and doorman: the runtimes' addresses are <machine>.<domain>
    machine_id: str | None = None  # hosted: this machine's id (Fly's FLY_MACHINE_ID), its own address
    keys_sha256: tuple[str, ...] = ()  # hosted: the runtime keys' SHA-256s at first; the Cloud's list after
    public_host: str | None = None  # hosted, development: this exact Host instead of <machine>.<domain> (an ngrok name)

    @property
    def link_url(self) -> str:
        return re.sub(r"^http", "ws", self.server.rstrip("/")) + "/v1/runtime/link"


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    mode = env.get("RROTOR_MODE", "local")
    if mode not in ("local", "hosted", "doorman"):
        raise RrotorError("RROTOR_MODE must be local, hosted or doorman.", code="E_CONFIG")
    server = env.get("RROTOR_SERVER", "http://doorman.invalid" if mode == "doorman" else "")
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
    machine_id = env.get("RROTOR_MACHINE_ID") or env.get("FLY_MACHINE_ID") or None
    public_host = (env.get("RROTOR_PUBLIC_HOST") or "").strip().lower().rstrip(".") or None
    if mode == "hosted" and machine_id is None and public_host is None:
        raise RrotorError(
            "A hosted rrotor needs its machine id (FLY_MACHINE_ID), or RROTOR_PUBLIC_HOST in development.",
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
        licence_key=env.get("RROTOR_LICENCE_PUBLIC_KEY") or None,
        runtime_id=runtime_id,
        runtime_token=runtime_token,
        builtin_parts=Path(env.get("RROTOR_BUILTIN_PARTS", "/opt/parts-builtin")),
        public_domain=env.get("RROTOR_PUBLIC_DOMAIN", "glyphh.app").strip().lower().rstrip("."),
        machine_id=machine_id.lower() if machine_id else None,
        keys_sha256=keys,
        public_host=public_host,
    )

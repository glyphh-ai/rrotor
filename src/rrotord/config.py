"""rrotord's settings, from its environment (set by whoever starts the container: the mod, or the hosted
provisioning). Secrets never come through here: the runtime token is handed in over the local API, and the local
token arrives only as its SHA-256."""

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

    @property
    def link_url(self) -> str:
        return re.sub(r"^http", "ws", self.server.rstrip("/")) + "/v1/runtime/link"


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    mode = env.get("RROTOR_MODE", "local")
    if mode not in ("local", "hosted"):
        raise RrotorError("RROTOR_MODE must be local or hosted.", code="E_CONFIG")
    server = env.get("RROTOR_SERVER", "")
    if not re.match(r"^https?://[^/\s]+$", server.rstrip("/")):
        raise RrotorError("RROTOR_SERVER must be the Glyphh server's address, http(s)://host[:port].", code="E_CONFIG")
    digest = env.get("RROTOR_LOCAL_TOKEN_SHA256") or None
    if digest is not None and not HEX64.match(digest):
        raise RrotorError("RROTOR_LOCAL_TOKEN_SHA256 must be 64 lowercase hex digits.", code="E_CONFIG")
    if mode == "local" and digest is None:
        raise RrotorError("A local rrotor needs RROTOR_LOCAL_TOKEN_SHA256 (its mod's local token).", code="E_CONFIG")
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
    )

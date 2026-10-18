"""The heartbeat: how a self-hosted rrotor renews its licence over one plain HTTPS call, for networks that let
nothing else out. At boot and every 24 hours it posts `{server}/v1/license/verify` with its runtime token and exactly
`{machine, licence_id, version, active_runs}`, and keeps the licence the server answers (rrotord/licence.py). The
machine is a SHA-256 of the runtime id and /etc/machine-id (or, where there is none, a random id kept on /data):
never an IP address, a prompt, a model's inputs or outputs, or anyone's data. A failure is logged and tried again
with backoff, a minute doubling to an hour; it never stops rrotor (docs/architecture.md, 12)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rrotord.errors import RrotorError
from rrotord.introspect import Post
from rrotord.licence import Licence
from rrotord.link.credentials import Credentials

log = logging.getLogger("rrotor.heartbeat")

EVERY = 24 * 3600.0
RETRY_FIRST, RETRY_MAX = 60.0, 3600.0
MACHINE_ID = Path("/etc/machine-id")


def machine_hash(runtime_id: str, data: Path, machine_id: Path = MACHINE_ID) -> str:
    """SHA-256 hex of `<runtime id>:<machine id>`, the machine id from /etc/machine-id or one made and kept on /data."""
    found = ""
    for path in (machine_id, data / "machine-id"):
        with contextlib.suppress(OSError):
            found = path.read_text().strip()
        if found:
            break
    else:
        found = secrets.token_hex(16)
        with contextlib.suppress(OSError):
            (data / "machine-id").parent.mkdir(parents=True, exist_ok=True)
            (data / "machine-id").write_text(found + "\n")
    return hashlib.sha256(f"{runtime_id}:{found}".encode()).hexdigest()


class Heartbeat:
    def __init__(
        self,
        *,
        server: str,
        credentials: Credentials,
        licence: Licence,
        post: Post,
        active_runs: Callable[[], int],
        data: Path,
        version: str,
        machine_id: Path = MACHINE_ID,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.url = server.rstrip("/") + "/v1/license/verify"
        self.credentials, self.licence, self.post, self.active_runs = credentials, licence, post, active_runs
        self.data, self.version, self.machine_id, self.sleep = data, version, machine_id, sleep
        self.machines: dict[str, str] = {}  # runtime id: its machine hash, worked out once
        self.last: dict[str, Any] | None = None

    def body(self, runtime_id: str) -> dict[str, Any]:
        """Exactly what goes to the server, and nothing else."""
        if runtime_id not in self.machines:
            self.machines[runtime_id] = machine_hash(runtime_id, self.data, self.machine_id)
        return {
            "machine": self.machines[runtime_id],
            "licence_id": self.licence.licence_id,
            "version": self.version,
            "active_runs": self.active_runs(),
        }

    async def beat(self) -> bool:
        """One heartbeat; answers whether the server answered with a licence that checks out."""
        token, runtime_id = self.credentials.token, self.credentials.runtime_id
        if not token or not runtime_id:
            return self._done(False, "no runtime token")
        try:
            status, answer = await self.post(self.url, {"Authorization": f"Bearer {token}"}, self.body(runtime_id))
        except Exception as err:  # noqa: BLE001  (whatever the network did: tried again later)
            return self._done(False, type(err).__name__)
        signed = answer.get("licence") if status == 200 and isinstance(answer, dict) else None
        if not isinstance(signed, dict):
            return self._done(False, f"the server answered {status}")
        try:
            self.licence.set(signed.get("payload"), signed.get("signature"))
        except RrotorError as err:
            return self._done(False, err.message)
        return self._done(True, None)

    def _done(self, ok: bool, error: str | None) -> bool:
        self.last = {"at": time.time(), "ok": ok, "error": error}
        if not ok:
            log.warning("heartbeat failed", extra={"reason": error})
        return ok

    def view(self) -> dict[str, Any] | None:
        return self.last

    async def run(self) -> None:
        """At boot, then every 24 hours; a failure tried again after a minute, doubling to an hour."""
        retry = RETRY_FIRST
        while True:
            if await self.beat():
                wait, retry = EVERY, RETRY_FIRST
            else:
                wait, retry = retry, min(RETRY_MAX, retry * 2)
            await self.sleep(wait)

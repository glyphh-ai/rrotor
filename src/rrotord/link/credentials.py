"""The runtime's id and token: handed in by the mod over the local API (or by the hosted provisioning), kept in
/data/runtime.json (0600), never logged. A new token restarts the link's counter and its connection."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from pathlib import Path

from rrotord.errors import BadRequest
from rrotord.store import read_json, write_json

TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]{16,512}$")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Credentials:
    def __init__(self, data: Path) -> None:
        self.path = data / "runtime.json"
        saved = read_json(self.path) or {}
        self.runtime_id: str | None = saved.get("runtime_id")
        self._token: str | None = saved.get("token")
        self.changed = asyncio.Event()

    @property
    def token(self) -> str | None:
        return self._token

    def set(self, runtime_id: str, token: str) -> None:
        try:
            runtime_id = str(uuid.UUID(runtime_id))
        except (ValueError, AttributeError, TypeError):
            raise BadRequest("runtime_id is the runtime's UUID.", code="E_RUNTIME_ID") from None
        if not isinstance(token, str) or not TOKEN.match(token):
            raise BadRequest("token is the runtime token the server gave.", code="E_TOKEN")
        write_json(self.path, {"runtime_id": runtime_id, "token": token}, private=True)
        self.runtime_id, self._token = runtime_id, token
        self.changed.set()

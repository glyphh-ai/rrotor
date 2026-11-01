"""The vault: each program's secrets, on this rrotor only (docs/architecture.md, 5.5).

A secret is a value set by name for one program (`PUT /v1/programs/{name}/secrets/{secret}`, MCP `secret_set`),
read by that program as `ctx.secrets["NAME"]` when its manifest declares the name. Values are encrypted at rest with
AES-256-GCM under the rrotor's own key, `/data/vault/key` (32 random bytes, made 0600 on first use, never logged,
never sent anywhere); each program's are in `/data/vault/<program>.json`, `{name: base64(nonce || ciphertext)}`,
with the program and the name as the associated data, so a value moved to another name or program does not decrypt.

No route, tool, log line, run record or audit event carries a value: listing answers names, and the audit log says
which name was set or deleted. A value reaches only the run's worker, in its start message over its own socket.

Secrets are node-local: they never go into a cluster's replicated log or snapshot, so a cluster keeps them on its
head (a follower refuses to set or delete one, and every call of a program that declares secrets runs on the head).
A node promoted to head has its own vault: the secrets are set on it again."""

from __future__ import annotations

import base64
import binascii
import os
import re
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from rrotord.errors import BadRequest, NotFound, RrotorError
from rrotord.manifest import CAPS, NAME
from rrotord.store import read_json, write_json

KEY_BYTES = 32
NONCE_BYTES = 12
VALUE_MAX = 64 * 1024  # bytes of one secret's value
PROGRAM = re.compile(NAME)
SECRET_NAME = re.compile(CAPS)


def check_program(program: str) -> str:
    if not PROGRAM.match(program):
        raise BadRequest(f"{program!r} is not a program name.", code="E_BAD_REQUEST")
    return program


def check_name(name: str) -> str:
    if not SECRET_NAME.match(name):
        raise BadRequest(
            f"{name!r} is not a secret's name: capitals, digits and _, starting with a capital (ANALYTICS_KEY).",
            code="E_SECRET_NAME",
        )
    return name


class Vault:
    def __init__(self, data: Path) -> None:
        self.root = data / "vault"
        self._aead: AESGCM | None = None

    def _key(self) -> AESGCM:
        """The rrotor's key, read once, made on first use (0600, never replaced)."""
        if self._aead is not None:
            return self._aead
        path = self.root / "key"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raw = path.read_bytes()
        else:
            raw = os.urandom(KEY_BYTES)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
        if len(raw) != KEY_BYTES:
            raise RrotorError("The vault's key is not 32 bytes: /data/vault/key is damaged.", code="E_VAULT")
        self._aead = AESGCM(raw)
        return self._aead

    def _path(self, program: str) -> Path:
        return self.root / f"{check_program(program)}.json"

    def _sealed(self, program: str) -> dict[str, str]:
        found = read_json(self._path(program), {})
        return found if isinstance(found, dict) else {}

    @staticmethod
    def _aad(program: str, name: str) -> bytes:
        return f"{program}/{name}".encode()

    def set(self, program: str, name: str, value: str) -> None:
        check_name(name)
        raw = value.encode()
        if not raw or len(raw) > VALUE_MAX:
            raise BadRequest("A secret's value is a string of 1 byte to 64 KB.", code="E_SECRET_VALUE")
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._sealed(program)
        sealed[name] = base64.b64encode(nonce + self._key().encrypt(nonce, raw, self._aad(program, name))).decode()
        write_json(self._path(program), sealed, private=True)

    def delete(self, program: str, name: str) -> None:
        check_name(name)
        sealed = self._sealed(program)
        if name not in sealed:
            raise NotFound(f"{program} has no secret {name} here.", code="E_SECRET_MISSING", program=program)
        del sealed[name]
        if sealed:
            write_json(self._path(program), sealed, private=True)
        else:
            self._path(program).unlink()

    def names(self, program: str) -> list[str]:
        return sorted(self._sealed(program))

    def values(self, program: str, names: list[str]) -> tuple[dict[str, str], list[str]]:
        """The values of `names` set for `program`, and the names not set (in order)."""
        sealed = self._sealed(program)
        found: dict[str, str] = {}
        missing: list[str] = []
        for name in names:
            if name not in sealed:
                missing.append(name)
                continue
            try:
                blob = base64.b64decode(sealed[name], validate=True)
                plain = self._key().decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], self._aad(program, name))
            except (binascii.Error, ValueError, InvalidTag):
                raise RrotorError(
                    f"{program}'s secret {name} does not decrypt with this rrotor's key: set it again.",
                    code="E_VAULT",
                ) from None
            found[name] = plain.decode()
        return found, missing

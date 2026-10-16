"""The two public keys rrotor checks signatures with: Glyphh's release key (what signs a part) and its licence key
(what signs the plan's licence). Both come in as base64 or base64url of the raw 32 bytes; neither is a secret."""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from rrotord.errors import RrotorError


def public_key(text: str) -> Ed25519PublicKey:
    """A key from its base64 or base64url text; refused with E_CONFIG when it is not one."""
    clean = text.strip().replace("-", "+").replace("_", "/")
    try:
        raw = base64.b64decode(clean + "=" * (-len(clean) % 4), validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as err:
        raise RrotorError("Not an Ed25519 public key: base64 of its raw 32 bytes.", code="E_CONFIG") from err


def unb64(text: str) -> bytes:
    """Bytes from base64 or base64url, padded or not; refused as E_SIGNATURE when not base64."""
    clean = text.strip().replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(clean + "=" * (-len(clean) % 4), validate=True)
    except (ValueError, TypeError) as err:
        raise RrotorError("The signature is not base64.", code="E_SIGNATURE", status=400) from err


def verify(key: Ed25519PublicKey, signature: str, message: bytes) -> bool:
    try:
        key.verify(unb64(signature), message)
    except InvalidSignature:
        return False
    return True


def canonical(payload: dict[str, Any]) -> bytes:
    """The bytes a licence's signature is over: keys sorted at every level, no spaces, ASCII only (Python's
    json.dumps(sort_keys=True, separators=(",", ":")), as the server signs it)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

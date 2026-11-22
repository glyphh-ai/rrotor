"""The licence key: `<base64url payload>.<base64url signature>`, base64url without padding. The payload is UTF-8 JSON;
the signature is Ed25519 over the payload's exact bytes as decoded, checked with a public key this module carries
(never one from the environment: whoever sets the environment could then sign their own). A key is good when any of
`PUBLIC_KEYS` verifies it (the current key and, during a rotation, the next); tests patch the tuple.

A good key lifts Community's limits to its own until `expires_at`, and holds them `GRACE_DAYS` past it (rrotor's
constant, not a field: a key cannot grant itself more). Release N still takes the plan's legacy `{payload,
signature}` licence, mapped to a key (`legacy`); release N+1 drops it (docs/architecture.md, 9). A key's
`max_precedents` (an Ada Cloud limit the server still writes) is not read here: rrotor runs no Ada models."""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from rrotord.errors import RrotorError
from rrotord.keys import canonical, public_key, verify

GLYPHH = "vDD0qLKsfgWsqbhH9tMnht4egmnlph72QzVvvpzjvSs"  # Glyphh's production licence key, raw 32 bytes in base64url
PUBLIC_KEYS: tuple[str, ...] = (GLYPHH,)
GRACE_DAYS = 14
GRACE = timedelta(days=GRACE_DAYS)
TIERS = ("free", "pro", "team", "enterprise")
B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
FIELDS = (
    "license_id",
    "tier",
    "max_nodes",
    "cluster_sync_enabled",
    "issued_at",
    "expires_at",
    "customer_id",
)


@dataclass(frozen=True, slots=True)
class Plan:
    """What a key allows: the tier and its limits (None is unlimited)."""

    tier: str
    max_nodes: int | None
    cluster_sync_enabled: bool

    @property
    def ha(self) -> bool:
        """High availability, a cluster electing its own head: Enterprise's (Pro and Team fail over by command)."""
        return self.tier == "enterprise" and self.cluster_sync_enabled


COMMUNITY = Plan("free", 1, False)  # no key, or one that has stopped counting
PLANS = {  # the server's table, used only to map a legacy licence: a key's own numbers are what rrotor acts on
    "free": COMMUNITY,
    "pro": Plan("pro", 3, True),
    "team": Plan("team", 10, True),
    "enterprise": Plan("enterprise", None, True),
}


class Refused(Exception):
    """A key that does not count, and why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Key:
    """A good key (or a legacy licence mapped to one): what it allows, when, and what it came as (`raw`, kept as
    given so it checks again after a restart)."""

    license_id: str | None
    plan: Plan
    issued_at: datetime | None  # None: a legacy licence, which has no issue date
    expires_at: datetime
    customer_id: str | None
    raw: Any  # the key's text; a legacy licence's {payload, signature}

    @property
    def legacy(self) -> bool:
        return not isinstance(self.raw, str)

    def state(self, now: datetime) -> str:
        """live before `expires_at`, grace for GRACE_DAYS after it, then expired."""
        if now < self.expires_at:
            return "live"
        return "grace" if now < self.expires_at + GRACE else "expired"


def b64url(text: str) -> bytes:
    """Bytes from base64url without padding, strictly: standard base64, padding or stray bits are refused."""
    if not B64URL.match(text) or len(text) % 4 == 1:
        raise Refused("not base64url without padding")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != text:
        raise Refused("not base64url without padding")
    return raw


def _verified(payload: bytes, signature: bytes) -> bool:
    for text in PUBLIC_KEYS:
        try:
            Ed25519PublicKey.from_public_bytes(b64url(text)).verify(signature, payload)
        except InvalidSignature:
            continue
        return True
    return False


def _when(data: dict[str, Any], field: str) -> datetime:
    value = data[field]
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    except ValueError:
        when = None
    if when is None or when.utcoffset() != timedelta(0):
        raise Refused(f"{field} is not an RFC 3339 UTC time")
    return when


def _count(data: dict[str, Any], field: str, least: int) -> int | None:
    value = data[field]
    if value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= least):
        return value
    raise Refused(f"{field} is not a whole number of at least {least}, or null")


def signed(text: str) -> dict[str, Any]:
    """What Glyphh signed, from `<payload>.<signature>`: the payload's JSON object, once its signature verifies with
    one of `PUBLIC_KEYS`; refused (Refused, with why) otherwise. A licence key is one (`parse`); so is a work ticket
    (rrotord/ticket.py)."""
    parts = text.strip().split(".")
    if len(parts) != 2:
        raise Refused("not <payload>.<signature>")
    payload, signature = b64url(parts[0]), b64url(parts[1])
    if not _verified(payload, signature):
        raise Refused("the signature does not verify")
    try:
        data = json.loads(payload.decode())
    except (UnicodeDecodeError, ValueError):
        raise Refused("the payload is not UTF-8 JSON") from None
    if not isinstance(data, dict):
        raise Refused("the payload is not a JSON object")
    return data


def parse(text: str) -> Key:
    """A key from its text: refused (Refused, with why) unless it is `<payload>.<signature>`, the signature verifies
    and every known field is there with its type. Unknown fields are ignored."""
    data = signed(text)
    missing = [f for f in FIELDS if f not in data]
    if missing:
        raise Refused(f"{', '.join(missing)} missing")
    try:
        license_id = str(uuid.UUID(data["license_id"])) if isinstance(data["license_id"], str) else None
    except ValueError:
        license_id = None
    if license_id is None:
        raise Refused("license_id is not a UUID")
    if data["tier"] not in TIERS:
        raise Refused(f"tier is not one of {', '.join(TIERS)}")
    if not isinstance(data["cluster_sync_enabled"], bool):
        raise Refused("cluster_sync_enabled is not true or false")
    if not isinstance(data["customer_id"], str):
        raise Refused("customer_id is not a string")
    plan = Plan(
        data["tier"],
        _count(data, "max_nodes", 1),
        data["cluster_sync_enabled"],
    )
    return Key(license_id, plan, _when(data, "issued_at"), _when(data, "expires_at"), data["customer_id"], text.strip())


def legacy(payload: Any, signature: Any, runtime_id: str | None) -> Key:
    """Release N's mapping of the plan's legacy licence, `{payload, signature}` signed over the payload's canonical
    JSON: `plan` as the tier (unknown: free), the limits from the plan table, `features.sync` as
    cluster_sync_enabled, `expires_at` plus its `grace_days` as expires_at. Refused if it does not verify, names
    another runtime, or its dates do not read."""
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise Refused("not {payload, signature}")
    try:
        good = any(verify(public_key(text), signature, canonical(payload)) for text in PUBLIC_KEYS)
    except RrotorError:
        good = False
    if not good:
        raise Refused("the signature does not verify")
    named = payload.get("runtime_id")
    if named is not None and runtime_id is not None and str(named) != runtime_id:
        raise Refused("the licence is for another runtime")
    grace = payload.get("grace_days", 0)
    try:
        expires = _when({"expires_at": payload.get("expires_at")}, "expires_at")
    except Refused:
        expires = None
    if expires is None or not isinstance(grace, int) or isinstance(grace, bool) or grace < 0:
        raise Refused("its expires_at or grace_days do not read")
    table = PLANS.get(str(payload.get("plan", "")).lower(), COMMUNITY)
    features = payload.get("features")
    sync = isinstance(features, dict) and features.get("sync") is True
    named_id, customer = payload.get("licence_id"), payload.get("org_id")
    return Key(
        str(named_id) if named_id is not None else None,
        Plan(table.tier, table.max_nodes, sync),
        None,
        expires + timedelta(days=grace),
        str(customer) if customer is not None else None,
        {"payload": payload, "signature": signature},
    )


def unpack(token: str) -> tuple[Any, Any]:
    """A legacy offline token (base64url of {payload, signature}, padded or not) as its two halves."""
    clean = token.strip().replace("-", "+").replace("_", "/")
    try:
        signed = json.loads(base64.b64decode(clean + "=" * (-len(clean) % 4), validate=True))
    except (binascii.Error, ValueError):
        signed = None
    if not isinstance(signed, dict):
        raise Refused("neither a key (<payload>.<signature>) nor a legacy licence token")
    return signed.get("payload"), signed.get("signature")

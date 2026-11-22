"""A work ticket: Glyphh's signature, for this runtime, for a person, until it expires; and only the work routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from rrotord import ticket
from tests.fakes import b64url, keys

RUNTIME = "0b0e3a52-6a55-4d0b-9d52-0f6f4f1c2a11"
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def signed(drop: tuple[str, ...] = (), **fields: Any) -> str:
    payload = {"kind": "work", "runtime_id": RUNTIME, "sub": "user_1", "expires_at": "2026-11-22T12:10:00Z"} | fields
    raw = json.dumps({k: v for k, v in payload.items() if k not in drop}).encode()
    return f"{b64url(raw)}.{b64url(keys().private.sign(raw))}"


def test_a_ticket_glyphh_signed_for_this_runtime_names_its_person_until_it_expires():
    assert ticket.subject(signed(), RUNTIME, NOW) == "user_1"
    assert ticket.subject(signed(expires_at="2999-01-01T00:00:00+00:00"), RUNTIME) == "user_1"  # by the clock
    assert ticket.subject(signed(), RUNTIME, datetime(2026, 11, 22, 12, 10, tzinfo=UTC)) is None


def test_anything_else_is_nobody():
    assert ticket.subject(signed(), None, NOW) is None  # a rrotor with no runtime id takes none
    assert ticket.subject(signed(), "another-runtime", NOW) is None
    assert ticket.subject("not-a-ticket", RUNTIME, NOW) is None
    forged = signed().rsplit(".", 1)[0] + "." + b64url(b"\0" * 64)
    assert ticket.subject(forged, RUNTIME, NOW) is None
    assert ticket.subject(signed(kind="licence"), RUNTIME, NOW) is None
    assert ticket.subject(keys().key(), RUNTIME, NOW) is None  # a licence key is no ticket
    for bad in ({"sub": ""}, {"sub": 7}, {"expires_at": 7}, {"expires_at": "soon"}, {"expires_at": "2999-01-01T00:00"}):
        assert ticket.subject(signed(**bad), RUNTIME, NOW) is None, bad
    assert ticket.subject(signed(drop=("expires_at",)), RUNTIME, NOW) is None
    assert ticket.subject(signed(expires_at="2999-01-01T00:00:00+02:00"), RUNTIME, NOW) is None  # UTC only


def test_a_ticket_opens_the_work_routes_and_the_folder_picker_and_nothing_else():
    assert ticket.covers("/v1/work") and ticket.covers("/v1/work/acme__crew/tree") and ticket.covers("/v1/folders")
    for path in ("/v1/workers", "/v1/runs", "/v1/folders/x", "/v1/folders-x", "/mcp", "/v1/work-x"):
        assert not ticket.covers(path), path

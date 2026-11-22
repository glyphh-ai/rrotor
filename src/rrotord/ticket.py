"""A work ticket: what lets a person's browser, on a page of Glyphh's platform, read this rrotor's work (the crew's
files, diffs and command log: rrotord/work.py) straight from the machine. Glyphh's server knows who may use this
runtime, so it signs a short-lived ticket for them, and nothing more: what the ticket opens is read from here by the
browser itself, and the server neither carries nor keeps any of it.

A ticket is `<base64url payload>.<base64url signature>`, signed as a licence key is (rrotord/licence_key.py: Ed25519
over the payload's bytes, checked with the public keys the code carries). Its payload:

    {"kind": "work", "runtime_id": "<this runtime's id>", "sub": "<the person>", "expires_at": "<RFC 3339, UTC>"}

It is good while the signature verifies, the kind is `work`, the runtime is this one (a rrotor that has no runtime id
yet takes none) and it has not expired. The gate takes one on `/v1/work` and under it, and on `GET /v1/folders` (the
folders of the machine a bot may be given, for the same page's picker), and nowhere else (rrotord/api/gate.py);
whoever it lets in is the sign-in the audit log names."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rrotord.licence_key import Refused, signed

KIND = "work"
PATH = "/v1/work"
FOLDERS = "/v1/folders"  # the folder picker's listing (rrotord/folders.py): folders' names, read by the same page


def covers(path: str) -> bool:
    """Whether a path is one a ticket opens: the work routes and the folder picker's listing, nothing else."""
    return path in (PATH, FOLDERS) or path.startswith(PATH + "/")


def subject(text: str, runtime_id: str | None, now: datetime | None = None) -> str | None:
    """Who a ticket is for, when it is good for this runtime now; None for anything else."""
    if runtime_id is None:
        return None
    try:
        data = signed(text)
    except Refused:
        return None
    sub, until = data.get("sub"), data.get("expires_at")
    if data.get("kind") != KIND or data.get("runtime_id") != runtime_id or not isinstance(sub, str) or not sub:
        return None
    try:
        expires = datetime.fromisoformat(until.replace("Z", "+00:00")) if isinstance(until, str) else None
    except ValueError:
        expires = None
    if expires is None or expires.utcoffset() != timedelta(0):
        return None
    return sub if (now or datetime.now(UTC)) < expires else None

"""Protocol 1's messages, as the server builds them (glyphh-ai/server, ops/link/protocol.py): JSON, one per frame,
each with `type` and `id`."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

PROTOCOL = 1

REFUSED, BAD_HELLO, SILENT, REPLACED, UPGRADE = 4401, 4400, 4408, 4409, 4426
FAILED, RESTART = 1011, 1012
# Closes after which rrotor does not reconnect by itself, and the state it shows.
STOPS: dict[int, str] = {REFUSED: "refused", REPLACED: "replaced", UPGRADE: "upgrade_needed", BAD_HELLO: "bad_hello"}

Id = Annotated[str, Field(min_length=1, max_length=64)]
Name = Annotated[str, Field(min_length=1, max_length=200)]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


def new_id() -> str:
    return uuid.uuid4().hex


class Message(BaseModel):
    id: Id = Field(default_factory=new_id)


# Cloud → rrotor


class Limits(BaseModel):
    runtime_hours: int | None = None


class Signed(BaseModel):
    """The plan's legacy licence as the server signed it (release N still takes it, mapped to a key)."""

    payload: dict[str, Any]
    signature: str


class Welcome(Message):
    type: Literal["welcome"]
    accepted: bool = True
    limits: Limits = Limits()
    licence: Signed | None = None
    license_key: str | None = None  # the licence key, preferred to `licence`
    keys: list[str] | None = None  # hosted: the runtime keys' SHA-256s, the whole list


class Keys(Message):
    """The runtime keys changed: the whole list of their SHA-256s (one revoked is one gone)."""

    type: Literal["keys"]
    hashes: list[str]


class Pause(Message):
    """The plan's hosted hours are spent: refuse every call (402) until `resume`."""

    type: Literal["pause"]
    reason: str | None = None


class Resume(Message):
    type: Literal["resume"]


class Call(Message):
    type: Literal["call"]
    run_id: uuid.UUID
    model: Name
    version: int
    function: Name
    inputs: dict[str, Any]
    caller: Literal["routine", "claude", "panel", "test"]
    clearance: float = Field(1.0, ge=0, le=1)


class Continue(Message):
    type: Literal["continue"]
    run_id: uuid.UUID
    answer: Any = None


class Cancel(Message):
    type: Literal["cancel"]
    run_id: uuid.UUID


class Deploy(Message):
    type: Literal["deploy", "activate", "remove"]
    model: Name
    version: int
    repo: str | None = None
    commit_sha: Sha
    token: str | None = None


class Unbuilt(Message):
    """Messages rrotor does not handle yet (the vault): logged, skipped."""

    type: Literal["secret_set"]


class Ping(Message):
    type: Literal["ping"] = "ping"


class Pong(Message):
    type: Literal["pong"] = "pong"


class Ack(Message):
    type: Literal["ack"] = "ack"
    of: Id


Down = Annotated[
    Welcome | Call | Continue | Cancel | Deploy | Keys | Pause | Resume | Unbuilt | Ping | Pong | Ack,
    Field(discriminator="type"),
]
DOWN: TypeAdapter[Down] = TypeAdapter(Down)


# rrotor → Cloud


def hello(version: str, mode: str, runtime_id: str, deployed: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "hello",
        "id": new_id(),
        "protocol": PROTOCOL,
        "version": version,
        "mode": mode,
        "runtime_id": runtime_id,
        "deployed": deployed,
    }


def result(seq: int, run_id: str, outcome: dict[str, Any], seconds: float) -> dict[str, Any]:
    keep = {k: outcome.get(k) for k in ("status", "data", "step", "prompt")}
    return {"type": "result", "id": new_id(), "seq": seq, "run_id": run_id, "result": keep, "seconds": int(seconds)}


def event(seq: int, model: str, kind: str, data: Any) -> dict[str, Any]:
    return {"type": "event", "id": new_id(), "seq": seq, "model": model, "kind": kind, "data": data}


def deployed(
    model: str, version: int, commit_sha: str, repo: str | None = None, manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    """After every deploy, however it was asked for: `repo` only for a deploy from a repo."""
    return {
        "type": "deployed",
        "id": new_id(),
        "model": model,
        "version": version,
        "commit_sha": commit_sha,
        "repo": repo,
        "manifest": manifest,
    }


def usage(seq: int, keys: list[dict[str, Any]], precedents: int | None = None) -> dict[str, Any]:
    """Each runtime key's calls since the last report: `{key_sha256, calls, last_used_at}`, never the key; and the
    runtime's precedents across its Ada models, the total now, whenever one has been read."""
    message: dict[str, Any] = {"type": "usage", "id": new_id(), "seq": seq, "keys": keys}
    if precedents is not None:
        message["precedents"] = precedents
    return message

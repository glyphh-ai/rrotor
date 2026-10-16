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
    """The plan's licence as the server signed it: what the mod hands a local rrotor over its local API, and what a
    hosted one, with no mod, gets in its welcome."""

    payload: dict[str, Any]
    signature: str


class Welcome(Message):
    type: Literal["welcome"]
    accepted: bool = True
    limits: Limits = Limits()
    licence: Signed | None = None


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


Down = Annotated[Welcome | Call | Continue | Cancel | Deploy | Unbuilt | Ping | Pong | Ack, Field(discriminator="type")]
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


def deployed(model: str, version: int, commit_sha: str) -> dict[str, Any]:
    return {"type": "deployed", "id": new_id(), "model": model, "version": version, "commit_sha": commit_sha}

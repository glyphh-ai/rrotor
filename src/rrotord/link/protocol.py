"""Protocol 3's messages, as the server builds them (glyphh-ai/server, ops/link/protocol.py): JSON, one per frame,
each with `type` and `id`. Protocol 3 (rrotor 0.3.0) is protocol 2 with the catalogue's names changed: the `app`
message for `artifact`, and `apps` in the welcome for `artifacts`; the old names are still read here (the same `App`
message, the same list), so a server speaking protocol 2 is understood, though none is expected. Protocol 1 named a
program `model`; `hello` says 3, so a server of protocol 1 closes the link with 4426 (`upgrade_needed`) rather than
misreading it, and a protocol 1 message that still reaches rrotor fails validation and is skipped. The server never
sets a program's secrets (they are set on the runtime itself, rrotord/vault.py): an earlier server's `secret_set` is
an unknown type, and skipped the same way."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, TypeAdapter

PROTOCOL = 3  # 2 said `artifact` where 3 says `app`; 1 named a program `model`

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


class Reviewed(BaseModel):
    """What the catalogue says of one of this rrotor's apps (docs/architecture.md, 5.8)."""

    program: Name
    version: int = Field(ge=1)
    state: Literal["draft", "approved", "rejected", "retired"]
    roles: list[str] = []


class Welcome(Message):
    model_config = ConfigDict(populate_by_name=True)
    type: Literal["welcome"]
    accepted: bool = True
    limits: Limits = Limits()
    licence: Signed | None = None
    license_key: str | None = None  # the licence key, preferred to `licence`
    keys: list[str] | None = None  # hosted: the runtime keys' SHA-256s, the whole list
    # The catalogue's whole word on this rrotor's apps (None: an older server); protocol 2 called it `artifacts`.
    apps: list[Reviewed] | None = Field(None, validation_alias=AliasChoices("apps", "artifacts"))
    reviewers: list[str] | None = None  # the groups that review the org's apps, beside its admin


class App(Reviewed, Message):
    """An app's review changed: approved (the version named is served, and activated), rejected or retired.
    Protocol 2 typed the same message `artifact`: read as this one."""

    type: Literal["app", "artifact"]


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
    program: Name
    version: int | None = None  # None: the active version (a built-in program's, which the hello does not list)
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
    program: Name
    version: int
    repo: str | None = None
    commit_sha: Sha
    token: str | None = None


class Ping(Message):
    type: Literal["ping"] = "ping"


class Pong(Message):
    type: Literal["pong"] = "pong"


class Ack(Message):
    type: Literal["ack"] = "ack"
    of: Id


Down = Annotated[
    Welcome | Call | Continue | Cancel | Deploy | Keys | Pause | Resume | App | Ping | Pong | Ack,
    Field(discriminator="type"),
]
DOWN: TypeAdapter[Down] = TypeAdapter(Down)


# rrotor → Cloud


def hello(
    version: str,
    mode: str,
    runtime_id: str,
    deployed: list[dict[str, Any]],
    node_id: str | None = None,
    cluster_id: str | None = None,
    role: str = "standalone",
) -> dict[str, Any]:
    """The link's first message; a node of a cluster (only its head links) names itself, its cluster and its role."""
    return {
        "type": "hello",
        "id": new_id(),
        "protocol": PROTOCOL,
        "version": version,
        "mode": mode,
        "runtime_id": runtime_id,
        "deployed": deployed,
        "node_id": node_id,
        "cluster_id": cluster_id,
        "role": role,
    }


def result(seq: int, run_id: str, outcome: dict[str, Any], seconds: float) -> dict[str, Any]:
    keep = {k: outcome.get(k) for k in ("status", "data", "step", "prompt")}
    return {"type": "result", "id": new_id(), "seq": seq, "run_id": run_id, "result": keep, "seconds": int(seconds)}


def event(seq: int, program: str, kind: str, data: Any) -> dict[str, Any]:
    return {"type": "event", "id": new_id(), "seq": seq, "program": program, "kind": kind, "data": data}


def deployed(
    program: str,
    version: int,
    commit_sha: str,
    repo: str | None = None,
    manifest: dict[str, Any] | None = None,
    by: str | None = None,
) -> dict[str, Any]:
    """After every deploy, however it was asked for: `repo` only for a deploy from a repo; `by` the Glyphh subject
    who published an app (null for a key, the local token or a deploy from a repo)."""
    return {
        "type": "deployed",
        "id": new_id(),
        "program": program,
        "version": version,
        "commit_sha": commit_sha,
        "repo": repo,
        "manifest": manifest,
        "by": by,
    }


def usage(seq: int, keys: list[dict[str, Any]]) -> dict[str, Any]:
    """Each runtime key's calls since the last report: `{key_sha256, calls, last_used_at}`, never the key."""
    return {"type": "usage", "id": new_id(), "seq": seq, "keys": keys}

"""`ctx`: what a run was granted, and nothing else (docs/architecture.md, 5.3)."""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import shlex
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, overload

from rrotor.errors import (
    CliError,
    DatabaseUndeclared,
    NotJson,
    SecretMissing,
    SecretUndeclared,
    StepUndeclared,
    Wait,
)
from rrotor.graph import Graph
from rrotor.http import Http
from rrotor.result import Result


@dataclass(slots=True)
class RunState:
    """What a run keeps between calls: approvals given per step, inputs answered per key, checkpoints per key."""

    approvals: dict[str, bool] = field(default_factory=dict)
    answers: dict[str, Any] = field(default_factory=dict)
    checkpoints: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"approvals": self.approvals, "answers": self.answers, "checkpoints": self.checkpoints}

    def record(self, pending: dict[str, str], answer: Any) -> None:
        """Keep a continue's answer where the function reads it: an approval by its step ({"approved": bool}), an
        input by its key; a Result the function returned itself reads `ctx.answer` instead."""
        if pending.get("kind") == "approval":
            self.approvals[pending["key"]] = bool(isinstance(answer, dict) and answer.get("approved") is True)
        elif pending.get("kind") == "input":
            self.answers[pending["key"]] = answer

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> RunState:
        data = data or {}
        return cls(dict(data.get("approvals", {})), dict(data.get("answers", {})), dict(data.get("checkpoints", {})))


def _json_value(value: Any, what: str) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as err:
        raise NotJson(f"{what} must be a JSON value: {err}") from None
    return value


Sink = Callable[[str, dict[str, Any]], None]
Request = Callable[[dict[str, Any]], dict[str, Any]]  # a CLI's command, run by rrotor: its Result as JSON


def cli_step(name: str, args: list[str], stdin: str | None) -> str:
    """The approval step (and the checkpoint) of one command of a CLI a program runs: the CLI's name and a digest of
    the command, so each command is approved for itself."""
    digest = hashlib.sha256(json.dumps([name, args, stdin]).encode()).hexdigest()[:16]
    return f"cli:{name}:{digest}"


@dataclass(frozen=True, slots=True)
class CliResult:
    """What a CLI's command answered `ctx.cli`: its exit code, and the head of its stdout and stderr (64 KB each)."""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_json(self) -> dict[str, Any]:
        return {"exit_code": self.exit_code, "stdout": self.stdout, "stderr": self.stderr, "truncated": self.truncated}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CliResult:
        return cls(int(data["exit_code"]), str(data["stdout"]), str(data["stderr"]), bool(data.get("truncated")))


def _no_cli(_: dict[str, Any]) -> dict[str, Any]:
    return {"status": "failed", "data": {"code": "E_CLI_MISSING", "reason": "No CLI runs here."}}


class Secrets:
    """`ctx.secrets`: the values of the secrets the manifest declares, by name. A name it does not declare is refused
    (E_SECRET_UNDECLARED); rrotor stops a run with a declared one unset before the function runs."""

    __slots__ = ("_declared", "_values")

    def __init__(self, declared: Iterable[str] = (), values: Mapping[str, str] | None = None) -> None:
        self._declared = frozenset(declared)
        self._values = dict(values or {})

    def __getitem__(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise SecretMissing(name)
        return value

    @overload
    def get(self, name: str) -> str | None: ...

    @overload
    def get(self, name: str, default: str) -> str: ...

    def get(self, name: str, default: str | None = None) -> str | None:
        if name not in self._declared:
            raise SecretUndeclared(name)
        return self._values.get(name, default)

    def __contains__(self, name: object) -> bool:
        return name in self._declared and name in self._values

    def __repr__(self) -> str:
        return f"Secrets({sorted(self._declared)})"  # the names, never a value


class Ctx:
    """The run's context. Programs read `inputs`, `answer`, `files`, `data`, `home`, `clearance`, `caller`, `db` and
    `graph`, and call the methods below."""

    def __init__(
        self,
        *,
        inputs: dict[str, Any],
        approvals: frozenset[str],
        state: RunState,
        files: Path,
        http: Http,
        answer: Any = None,
        sink: Sink | None = None,
        today: Callable[[], dt.date] | None = None,
        data: Path | None = None,
        home: Path | None = None,
        clearance: float = 1.0,
        caller: str = "claude",
        readonly: bool = False,
        secrets: Secrets | None = None,
        database: Path | None = None,
        clis: Mapping[str, Mapping[str, Any] | None] | None = None,
        request: Request | None = None,
        graph: Graph | None = None,
    ) -> None:
        self.data, self.home, self.clearance, self.caller = data, home, clearance, caller
        # the rrotor's graph (rrotor/graph.py): a manifest that does not name it gets one that refuses every call
        self.graph: Graph = graph or Graph()
        self._clis = dict(clis or {})  # each CLI the manifest names: its allow and approval patterns (None: not here)
        self._request = request or _no_cli
        self._database = database  # the SQLite file, when the manifest asks for one
        self._db: sqlite3.Connection | None = None  # opened on first use
        self._secrets = secrets or Secrets()
        self.readonly = readonly  # a cluster follower's run: its folders read-only
        self.inputs: dict[str, Any] = inputs
        self.answer: Any = answer
        self.files: Path = files
        self.http: Http = http
        self._approvals, self._state = approvals, state
        self._sink: Sink = sink or (lambda _op, _data: None)
        self._today = today or (lambda: dt.datetime.now(dt.UTC).date())

    def ask_approval(self, step: str, prompt: str) -> bool:
        """True once the person approved `step`, False once they declined; until then the run waits there."""
        if step not in self._approvals:
            raise StepUndeclared(step)
        if step in self._state.approvals:
            return self._state.approvals[step]
        raise Wait(Result.needs_approval(step, prompt), {"kind": "approval", "key": step})

    def needs_input(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        options: list[Any] | None = None,
        key: str | None = None,
    ) -> Any:
        """The answer once the caller gave it; until then the run waits for it. `key` names the question (the
        prompt by default), so a function can ask several."""
        name = key or prompt
        if name in self._state.answers:
            return self._state.answers[name]
        raise Wait(Result.needs_input(prompt, schema, options), {"kind": "input", "key": name})

    def checkpoint(self, key: str, fn: Callable[[], Any]) -> Any:
        """Run `fn` once per run: a resumed run reads the value it stored."""
        if key in self._state.checkpoints:
            return self._state.checkpoints[key]
        value = _json_value(fn(), f"The checkpoint {key!r}")
        self._state.checkpoints[key] = value
        self._sink("checkpoint", {"key": key, "value": value})
        return value

    def cli(self, name: str, args: Iterable[str], stdin: str | None = None) -> CliResult:
        """One command of a CLI on this rrotor (built in or deployed, named in the manifest's `clis`), run by rrotor in
        that CLI's own sandbox, with its private HOME (its sign-in), its hosts and its secrets: its exit code, stdout
        and stderr. Once per run: a resumed run reads what the command answered. Its arguments must be ones the CLI
        allows (E_CLI_DENIED); one its approval patterns name waits for the person's approval first, as a step of this
        run (declined: E_CLI_DECLINED). A command that cannot run fails with its code (E_CLI_…, E_SECRET_MISSING, …)."""
        argv = [str(a) for a in args]
        if name not in self._clis:
            raise CliError.undeclared(name)
        patterns = self._clis[name]
        if patterns is None:
            raise CliError.missing(name)
        line = shlex.join(argv)
        allow = list(patterns.get("allow") or [])
        if allow and not any(fnmatch.fnmatchcase(line, p) for p in allow):
            raise CliError(
                f"{name} does not allow `{line}`: its allow patterns are {', '.join(allow)}.", "E_CLI_DENIED"
            )
        step = cli_step(name, argv, stdin)
        if step in self._state.checkpoints:
            return CliResult.from_json(self._state.checkpoints[step])
        approval = next((p for p in patterns.get("approvals") or [] if fnmatch.fnmatchcase(line, p)), None)
        if approval is not None:
            if step not in self._state.approvals:
                prompt = f"Run `{name} {line}`? It matches {name}'s approval pattern {approval!r}."
                raise Wait(Result.needs_approval(step, prompt), {"kind": "approval", "key": step})
            if not self._state.approvals[step]:
                raise CliError(f"The person declined `{name} {line}`.", "E_CLI_DECLINED")
        answer = self._request({"cli": name, "args": argv, "stdin": stdin, "step": step})
        data: dict[str, Any] = answer["data"] if isinstance(answer.get("data"), dict) else {}
        if answer.get("status") != "done":
            code = str(data.get("code") or "E_CLI")
            raise CliError(f"{name} {line}: {data.get('reason') or answer.get('status')}", code)
        return CliResult.from_json(self.checkpoint(step, lambda: CliResult.from_json(data).to_json()))

    def log(self, message: str, **detail: Any) -> None:
        self._sink("log", {"message": str(message), "detail": _json_value(detail, "A log line's detail")})

    def today(self) -> str:
        """Today's date (UTC), as YYYY-MM-DD."""
        return self._today().isoformat()

    def yesterday(self) -> str:
        """Yesterday's date (UTC), as YYYY-MM-DD."""
        return (self._today() - dt.timedelta(days=1)).isoformat()

    @property
    def db(self) -> sqlite3.Connection:
        """The program's SQLite database (`"databases": ["sqlite"]`), a file in its data folder, opened on first use
        and closed when the run ends: commit with `with ctx.db:` (what is not committed is rolled back). Read-only on
        a cluster's follower. Not declared: E_DATABASE_UNDECLARED."""
        if self._database is None:
            raise DatabaseUndeclared
        if self._db is None:
            if self.readonly:
                self._db = sqlite3.connect(f"{self._database.as_uri()}?mode=ro", uri=True, timeout=5.0)
            else:
                self._db = sqlite3.connect(self._database, timeout=5.0)
        return self._db

    def close(self) -> None:
        """The database closed, if it was opened (the run's end)."""
        if self._db is not None:
            self._db.close()
            self._db = None

    @property
    def secrets(self) -> Secrets:
        """The secrets the manifest declares, set on this rrotor: `ctx.secrets["NAME"]`, `ctx.secrets.get("NAME")`."""
        return self._secrets

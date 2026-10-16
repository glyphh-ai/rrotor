"""`ctx`: what a run was granted, and nothing else (docs/architecture.md, 5.3)."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from rrotor.errors import ModelError, NotJson, PartMissing, StepUndeclared, Wait
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


class Ctx:
    """The run's context. Models read `inputs`, `answer` and `files`, and call the methods below."""

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
    ) -> None:
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

    def log(self, message: str, **detail: Any) -> None:
        self._sink("log", {"message": str(message), "detail": _json_value(detail, "A log line's detail")})

    def today(self) -> str:
        """Today's date (UTC), as YYYY-MM-DD."""
        return self._today().isoformat()

    def yesterday(self) -> str:
        """Yesterday's date (UTC), as YYYY-MM-DD."""
        return (self._today() - dt.timedelta(days=1)).isoformat()

    @property
    def db(self) -> NoReturn:
        raise PartMissing("Databases (ctx.db)", "database")

    @property
    def secrets(self) -> NoReturn:
        err = ModelError("Secrets (ctx.secrets) come with rrotor's vault, which is not built yet.")
        err.code = "E_NOT_BUILT"
        raise err

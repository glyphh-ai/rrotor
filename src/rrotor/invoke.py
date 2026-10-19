"""One call of a model's function, turned into a Result: the same in the sandbox's worker and in a model's own
tests (`rrotor.testing`). Every call answers a Result, never an exception (docs/architecture.md, 5.4)."""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rrotor.ada import LimitExceeded
from rrotor.ctx import Ctx
from rrotor.errors import ModelError, Wait
from rrotor.result import Result

Pending = dict[str, str]


@dataclass(frozen=True, slots=True)
class Outcome:
    result: Result
    pending: Pending | None = None  # what a waiting run waits for: {kind: approval|input|result, key}


def failure(step: str, code: str, reason: str) -> Result:
    return Result("failed", {"reason": reason, "code": code}, step=step)


def crash(step: str, err: BaseException) -> Result:
    """A model's exception: its type and message, and where in the model it was raised."""
    frames = [f for f in traceback.extract_tb(err.__traceback__) if f.filename.startswith("/model/")]
    where = f" (model.py line {frames[-1].lineno})" if frames else ""
    return failure(step, "E_CRASH", f"{type(err).__name__}: {err}{where}")


def invoke(step: str, fn: Callable[..., Any], ctx: Ctx, *args: Any) -> Outcome:
    try:
        value = fn(ctx, *args)
    except Wait as wait:
        waited = wait.result if isinstance(wait.result, Result) else failure(step, "E_BAD_RESULT", "bad wait")
        return Outcome(waited, wait.pending)
    except LimitExceeded as full:  # the precedent cap: a finished run that indexed nothing, never a failure
        return Outcome(Result.done(full.data))
    except ModelError as err:
        return Outcome(failure(step, err.code, err.message))
    except Exception as err:  # noqa: BLE001  (a model's crash is a failed Result, never an exception)
        return Outcome(crash(step, err))
    if not isinstance(value, Result):
        return Outcome(failure(step, "E_BAD_RESULT", f"{step} returned {type(value).__name__}, not a Result."))
    try:
        json.dumps(value.data)
    except (TypeError, ValueError) as err:
        return Outcome(failure(step, "E_NOT_JSON", f"The Result's data must be a JSON value: {err}"))
    return Outcome(value, {"kind": "result", "key": value.step or ""} if value.waiting else None)


def lifecycle(step: str, fn: Callable[..., Any] | None, ctx: Ctx) -> Result | None:
    """`load` or `end`: optional, answer nothing; a failure is a failed Result."""
    if fn is None:
        return None
    outcome = invoke(step, lambda c: fn(c) or Result.done(), ctx)
    return outcome.result if outcome.result.status == "failed" else None

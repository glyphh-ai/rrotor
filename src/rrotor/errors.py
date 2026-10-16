"""What the SDK raises. A model never needs to catch these: the worker turns each into a `failed` Result with
its stable code."""

from __future__ import annotations


class ModelError(Exception):
    """An error with a stable code, shown in the run's `failed` Result."""

    code: str = "E_MODEL"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PartMissing(ModelError):
    """Something a model asked for comes with a part this rrotor does not have (docs/architecture.md, 10)."""

    code = "E_PART_MISSING"

    def __init__(self, what: str, mod: str) -> None:
        super().__init__(f"{what} comes with the {mod} mod's part, which this rrotor does not have.")
        self.what, self.mod = what, mod


class StepUndeclared(ModelError):
    """A step asked approval for is not named in the function's `approvals` in the manifest."""

    code = "E_STEP_UNDECLARED"

    def __init__(self, step: str) -> None:
        super().__init__(f"The step {step!r} is not in this function's approvals in glyphh.json.")
        self.step = step


class NotJson(ModelError):
    """A checkpoint's value or a Result's data that JSON cannot carry."""

    code = "E_NOT_JSON"


class Wait(BaseException):
    """Raised by `ctx.ask_approval` and `ctx.needs_input` when the answer is not there yet: the function stops, and
    the run answers the Result it carries. A BaseException, so a model's `except Exception` does not swallow it."""

    def __init__(self, result: object, pending: dict[str, str]) -> None:
        super().__init__("waiting")
        self.result, self.pending = result, pending

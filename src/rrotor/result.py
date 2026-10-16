"""A run's Result: the status tells the caller what is next (docs/architecture.md, 5.3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

STATUSES: frozenset[str] = frozenset({"done", "needs_input", "needs_approval", "failed"})
WAITING: frozenset[str] = frozenset({"needs_input", "needs_approval"})


@dataclass(frozen=True, slots=True)
class Result:
    """What every call of a function returns. Build one with the class methods, never directly."""

    status: str
    data: Any = None
    step: str | None = None
    prompt: str | None = None

    @classmethod
    def done(cls, data: Any = None) -> Result:
        """Finished: `data` is any JSON value."""
        return cls("done", data)

    @classmethod
    def needs_input(cls, prompt: str, schema: dict[str, Any] | None = None, options: list[Any] | None = None) -> Result:
        """A value is missing, or a choice: the caller answers with `continue`."""
        return cls("needs_input", {"schema": schema, "options": options}, prompt=prompt)

    @classmethod
    def needs_approval(cls, step: str, prompt: str) -> Result:
        """About to act at `step`: the person approves or declines, and `continue` brings the answer."""
        return cls("needs_approval", None, step=step, prompt=prompt)

    @classmethod
    def failed(cls, step: str, reason: str) -> Result:
        """Stopped at `step`, with why."""
        return cls("failed", {"reason": reason}, step=step)

    @property
    def waiting(self) -> bool:
        return self.status in WAITING

    def to_json(self) -> dict[str, Any]:
        """The Outcome protocol 1 carries: status, data, step, prompt."""
        return {"status": self.status, "data": self.data, "step": self.step, "prompt": self.prompt}

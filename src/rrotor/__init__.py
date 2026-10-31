"""The rrotor SDK: what a program imports. Standard library only.

    from rrotor import Ctx, Result

A program is a module with `load(ctx)`, its functions `fn(ctx, inputs) -> Result` and `end(ctx)`; rrotor runs it in
its sandbox (docs/architecture.md, section 5)."""

from rrotor.ctx import Ctx, Secrets
from rrotor.errors import PartMissing, ProgramError, SecretMissing, SecretUndeclared, StepUndeclared
from rrotor.http import Http, Response
from rrotor.result import STATUSES, Result

__all__ = [
    "STATUSES",
    "Ctx",
    "Http",
    "PartMissing",
    "ProgramError",
    "Response",
    "Result",
    "SecretMissing",
    "SecretUndeclared",
    "Secrets",
    "StepUndeclared",
]

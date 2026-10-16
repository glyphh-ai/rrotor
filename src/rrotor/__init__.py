"""The rrotor SDK: what a model imports. Standard library only.

    from rrotor import Ctx, Result

A model is a module with `load(ctx)`, its functions `fn(ctx, inputs) -> Result` and `end(ctx)`; rrotor runs it in
its sandbox (docs/architecture.md, section 5)."""

from rrotor.ctx import Ctx
from rrotor.errors import ModelError, PartMissing, StepUndeclared
from rrotor.http import Http, Response
from rrotor.result import STATUSES, Result

__all__ = ["STATUSES", "Ctx", "Http", "ModelError", "PartMissing", "Response", "Result", "StepUndeclared"]

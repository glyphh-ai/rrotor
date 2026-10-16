"""Ada models on rrotor: the spec (`spec.json` beside `model.py`), the precedent store (`precedents.sqlite` in the
model's data folder) and the kernel, which the Ada part brings (`"needs": ["ada"]` in glyphh.json; the part is
mounted at /opt/parts/ada). Standard library here; the kernel is imported when a model opens.

    from rrotor import Result, ada

    def load(ctx):
        ada.open(ctx)                      # the model, kept on ctx.ada

    def decide(ctx, inputs):
        return Result.done(ctx.ada.query(inputs["data"], clearance=ctx.clearance).to_json())

A changed encoding (the spec's `seed` or `dimension`) means the store's glyphs no longer match; rrotor re-encodes
the store from its recorded situations when such a version is deployed (`reencode`), or refuses the deploy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rrotor.ctx import Ctx
from rrotor.errors import ModelError, PartMissing

SPEC = "spec.json"
STORE = "precedents.sqlite"
REENCODE = "__ada_reencode__"  # the maintenance operation rrotor runs in the sandbox on a changed encoding


class AdaError(ModelError):
    code = "E_ADA"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


def encoding_of(spec: dict[str, Any]) -> dict[str, Any]:
    """What a spec fixes about the store's glyphs: a change here needs a re-encode."""
    return {"seed": spec.get("seed", 42), "dimension": spec.get("dimension", 4096)}


def read_spec(home: Path) -> dict[str, Any]:
    path = home / SPEC
    try:
        spec = json.loads(path.read_text())
    except OSError as err:
        raise AdaError(f"An Ada model has {SPEC} beside model.py: {err.strerror}.") from None
    except ValueError as err:
        raise AdaError(f"{SPEC} is not JSON: {err}.") from None
    if not isinstance(spec, dict):
        raise AdaError(f"{SPEC} is a JSON object.")
    for key in ("seed", "dimension"):
        if key in spec and (not isinstance(spec[key], int) or isinstance(spec[key], bool) or spec[key] < 0):
            raise AdaError(f"{SPEC}'s {key} is a whole number.")
    return spec


def _kernel() -> tuple[Any, Any]:
    try:
        import glyphh_kernel  # type: ignore[import-not-found]
        import glyphh_spec  # type: ignore[import-not-found]
    except ImportError:
        raise PartMissing("The Ada kernel (rrotor.ada)", "ada") from None
    return glyphh_kernel, glyphh_spec


def _store(ctx: Ctx) -> Path:
    if ctx.data is None:
        raise AdaError('An Ada model keeps its precedents in its data folder: set "data": true in glyphh.json.')
    return ctx.data / STORE


def open(ctx: Ctx, *, author: str | None = None, k: int = 15, min_sim: float = 0.10, tau: float | None = None) -> Any:
    """The model: its spec read, its store opened (made on first use), kept on `ctx.ada` and returned."""
    _, glyphh_spec = _kernel()
    home = ctx.home if ctx.home is not None else Path.cwd()
    store = _store(ctx)
    try:
        spec = glyphh_spec.Spec.from_json(read_spec(home))
    except glyphh_spec.SpecError as err:
        raise AdaError(f"{SPEC} is refused: {err}") from None
    model = glyphh_spec.Model(spec, k=k, min_sim=min_sim, author=author or ctx.caller, path=str(store), tau=tau)
    _cap(model, ctx.limits.get("precedents"))
    ctx.ada = model
    return model


def _cap(model: Any, precedents: Any) -> None:
    """The plan's `precedents` limit: a record past it is refused (E_LIMIT_PRECEDENTS); null or absent is unlimited."""
    if not isinstance(precedents, int) or isinstance(precedents, bool):
        return
    record = model.record

    def capped(*args: Any, **kwargs: Any) -> Any:
        if len(model) >= precedents:
            raise AdaError(
                f"This plan allows {precedents} precedents in a model; this one holds {len(model)}.",
                code="E_LIMIT_PRECEDENTS",
            )
        return record(*args, **kwargs)

    model.record = capped


def reencode(ctx: Ctx) -> int:
    """Every precedent's glyph rebuilt under the spec's encoding; rows re-encoded. Nothing to do without a store."""
    glyphh_kernel, _ = _kernel()
    home = ctx.home if ctx.home is not None else Path.cwd()
    spec = read_spec(home)
    store = _store(ctx)
    if not store.is_file():
        return 0
    enc = encoding_of(spec)
    return int(glyphh_kernel.reencode_store(str(store), dim=enc["dimension"], seed=enc["seed"]))

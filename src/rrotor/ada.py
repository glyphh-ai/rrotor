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
import sqlite3
from pathlib import Path
from typing import Any

from rrotor.ctx import Ctx
from rrotor.errors import ModelError, PartMissing

SPEC = "spec.json"
STORE = "precedents.sqlite"
REENCODE = "__ada_reencode__"  # the maintenance operation rrotor runs in the sandbox on a changed encoding


class LimitExceeded(Exception):
    """A record, or a merge, that would grow the node's precedents past the licence key's cap. A model may catch it;
    uncaught, the run ends `done` with `data`, never `failed`: `{status: limit_exceeded, indexed: false, message,
    precedents, cap}`."""

    def __init__(self, held: int, cap: int, plan: str) -> None:
        message = (
            f"Precedent store capacity limit reached ({cap:,} max for the {plan} plan). "
            "Upgrade the key to index further precedents."
        )
        super().__init__(message)
        self.data: dict[str, Any] = {
            "status": "limit_exceeded",
            "indexed": False,
            "message": message,
            "precedents": held,
            "cap": cap,
        }


class AdaError(ModelError):
    code = "E_ADA"


class KernelTooOld(AdaError):
    code = "E_ADA_KERNEL"


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


def open(
    ctx: Ctx,
    *,
    author: str | None = None,
    k: int | None = None,
    min_sim: float | None = None,
    tau: float | None = None,
    refresh_every: int | None = None,
) -> Any:
    """The model: its spec read, its store opened (made on first use), kept on `ctx.ada` and returned. Its levers
    (k, min_sim, refresh_every and the learning and calibration settings) come from the spec's `settings`, then
    from what `ctx.ada.configure()` kept with the store, then from the arguments given here.

    On a cluster's follower (`ctx.readonly`) the store is opened with the kernel's read-only mode (kernel 3.10:
    `mode=ro`, nothing made or written, a restatement kept in memory): reads as ever, and a write raises the
    kernel's ReadOnlyError, which the run answers as E_READ_ONLY. An older kernel cannot open it so: E_ADA_KERNEL."""
    _, glyphh_spec = _kernel()
    home = ctx.home if ctx.home is not None else Path.cwd()
    store = _store(ctx)
    try:
        spec = glyphh_spec.Spec.from_json(read_spec(home))
    except glyphh_spec.SpecError as err:
        raise AdaError(f"{SPEC} is refused: {err}") from None
    levers: dict[str, Any] = {"k": k, "min_sim": min_sim, "tau": tau, "refresh_every": refresh_every}
    if ctx.readonly:
        levers["readonly"] = True
    try:
        model = glyphh_spec.Model(spec, author=author or ctx.caller, path=str(store), **levers)
    except TypeError as err:
        if not ctx.readonly or "readonly" not in str(err):
            raise
        raise KernelTooOld(
            "This node is a cluster's follower and opens the store read-only, which needs the Ada kernel 3.10 or later."
        ) from None
    _cap(model, store, ctx.limits)
    ctx.ada = model
    return model


def _whole(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ids(store: Path) -> set[str]:
    """The precedent ids a store holds, read without writing; none when it is not made yet."""
    if not store.is_file():
        return set()
    db = sqlite3.connect(f"{store.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        return {str(row[0]) for row in db.execute("SELECT id FROM precedents")}
    finally:
        db.close()


def _held(store: Path, rid: str) -> bool:
    """Whether the store already holds a precedent with this id (a record of it replaces the row)."""
    if not store.is_file():
        return False
    db = sqlite3.connect(f"{store.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        return db.execute("SELECT 1 FROM precedents WHERE id = ?", (rid,)).fetchone() is not None
    finally:
        db.close()


def _cap(model: Any, store: Path, limits: dict[str, Any]) -> None:
    """The licence key's `precedents` cap, on the node's total across its Ada models: this model's own, counted
    live, and the others' (`precedents_elsewhere`), as rrotord counted them when the run started. Only what grows
    the store is checked, on the kernel's two paths to it: a record whose id is new (one whose id the store holds
    replaces its row, and goes at the cap), and a merge, all or nothing. Past the cap, LimitExceeded; null or
    absent is unlimited. learn, calibrate, refresh, configure and a re-encode add nothing and are never checked."""
    cap = _whole(limits.get("precedents"))
    if cap is None:
        return
    others = _whole(limits.get("precedents_elsewhere")) or 0
    named = limits.get("plan")
    plan = named if isinstance(named, str) else "Community"
    space = model.space
    record, merge = space.record, space.merge

    def held() -> int:
        return others + len(space)

    def capped_record(*args: Any, **kwargs: Any) -> Any:
        now = held()
        if now >= cap:
            rid = kwargs.get("rid", args[3] if len(args) > 3 else None)
            if rid is None or not _held(store, str(rid)):
                raise LimitExceeded(now, cap, plan)
        return record(*args, **kwargs)

    def capped_merge(other_path: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            adds = len(_ids(Path(str(other_path)).expanduser()) - _ids(store))
        except sqlite3.Error:
            adds = 0  # not a store: the kernel's merge says so
        now = held()
        if adds and now + adds > cap:
            raise LimitExceeded(now, cap, plan)
        return merge(other_path, *args, **kwargs)

    space.record, space.merge = capped_record, capped_merge


def count(store: Path) -> int:
    """The precedents a store holds, the rows the cap counts as the model's length, read without the kernel and
    without writing: what rrotord reports up the link. A store not made yet holds none; one that does not read
    raises sqlite3.Error."""
    if not store.is_file():
        return 0
    db = sqlite3.connect(f"{store.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        return int(db.execute("SELECT COUNT(*) FROM precedents").fetchone()[0])
    finally:
        db.close()


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

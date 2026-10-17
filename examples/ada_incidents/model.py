"""incidents: an Ada model on rrotor. An app's errors, remembered. Each incident recorded with the fix that resolved
it becomes a precedent; a new error (from an alert, a log, Sentry) is answered from the incidents closest to it: the
fix that worked, how sure, and the nearest precedent's cause, owner and runbook for a caller cleared to see them.
The spec beside this file (spec.json) says what an incident looks like; the Ada part brings the kernel."""

from __future__ import annotations

from typing import Any

from rrotor import Result, ada

CONFIDENT = 0.6  # below this, decide() asks rather than guesses
INTERNAL = ("cause", "owner", "runbook")


def load(ctx: Any) -> None:
    ada.open(ctx, min_sim=0.35)  # several of an incident's attributes must agree before a precedent counts


def start(ctx: Any, inputs: dict[str, Any]) -> Result:
    return Result.done({"incidents": len(ctx.ada)})


def record(ctx: Any, inputs: dict[str, Any]) -> Result:
    rid = ctx.ada.record(inputs["data"], inputs.get("outcome"))
    return Result.done({"id": rid, "incidents": len(ctx.ada)})


def query(ctx: Any, inputs: dict[str, Any]) -> Result:
    return Result.done(ctx.ada.query(inputs["data"], clearance=ctx.clearance).to_json())


def facts(ctx: Any, inputs: dict[str, Any]) -> Result:
    return Result.done(ctx.ada.facts(inputs["data"], clearance=ctx.clearance, top=inputs.get("top")))


def learn(ctx: Any, inputs: dict[str, Any]) -> Result:
    """The weights learned from the store's own graded precedents and the temperature calibrated, both kept with the
    store: every role's weight is how much it tells about the outcome. Needs ten graded precedents of two outcomes."""
    graded = ctx.ada.space.graded()
    if len(graded) < 10 or len({outcome for _, outcome in graded}) < 2:
        return Result.failed("learn", f"learn needs ten precedents with an outcome, of two outcomes; {len(graded)} graded")
    out = ctx.ada.refresh(sample=int(inputs.get("sample", 200)), clearance=ctx.clearance)
    roles = {path: found["similarity_weight"] for path, found in out["learned"].items()}
    return Result.done({"roles": roles, "tau": out["tau"], "precedents": len(graded)})


def probabilities(ctx: Any, inputs: dict[str, Any]) -> Result:
    spec, space = ctx.ada.spec, ctx.ada.space
    situation = spec.situation(inputs["data"])
    return Result.done(space.probabilities(situation, weights=spec.weights(ctx.clearance, situation)))


def _node(tree: dict[str, Any], key: str) -> dict[str, Any] | None:
    return next((c for c in tree.get("children", []) if c.get("key") == key), None)


def _resolution(tree: dict[str, Any]) -> dict[str, Any]:
    """The nearest precedent's cause, owner and runbook, as the fact tree shows them to this caller: the recorded
    values under resolution.fix, none at all below the resolution layer's clearance."""
    layer = _node(tree, "resolution")
    fix = _node(layer, "fix") if layer else None
    if fix is None:
        return {}
    leaves = {c["key"]: c.get("record") for c in fix.get("children", [])}
    return {name: leaves[name] for name in INTERNAL if leaves.get(name) is not None}


def decide(ctx: Any, inputs: dict[str, Any]) -> Result:
    answer = ctx.ada.query(inputs["data"], clearance=ctx.clearance)
    if answer.sufficient and answer.confidence >= CONFIDENT:
        nearest = ctx.ada.facts(inputs["data"], clearance=ctx.clearance, top=1)
        last_time = _resolution(nearest[0]["tree"]) if nearest else {}
        return Result.done(
            {"fix": answer.top, "confidence": round(answer.confidence, 4), "from": answer.count, "last_time": last_time}
        )
    fix = ctx.needs_input("No past incident is close enough. How was this one fixed?")
    rid = ctx.ada.record(inputs["data"], str(fix))
    return Result.done({"fix": str(fix), "confidence": None, "from": 0, "last_time": {}, "recorded": rid})

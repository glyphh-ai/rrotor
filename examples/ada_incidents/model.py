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
    ada.open(ctx)  # its levers are the spec's settings (min_sim 0.35: several attributes must agree)


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
    store: every role's weight is how much it tells about the outcome, every word of a text role its weight (`words`:
    how many were learned per role), and the pairs of roles that tell more together (interactions). Needs ten graded
    precedents of two outcomes."""
    graded = ctx.ada.space.graded()
    if len(graded) < 10 or len({outcome for _, outcome in graded}) < 2:
        return Result.failed(
            "learn", f"learn needs ten precedents with an outcome, of two outcomes; {len(graded)} graded"
        )
    out = ctx.ada.refresh(
        sample=int(inputs.get("sample", 200)), clearance=ctx.clearance, by=str(inputs.get("by", "loss"))
    )
    learned = out["learned"]
    roles = {path: f["similarity_weight"] for path, f in learned.items() if "similarity_weight" in f}
    words = {
        path: len(f["value_weights"])
        for path, f in learned.items()
        if not path.startswith("~") and "similarity_weight" not in f
    }
    pairs = [
        {"name": i["name"], "roles": i["roles"], "weight": i["similarity_weight"]}
        for i in learned.get("~interactions", [])
    ]
    return Result.done(
        {"roles": roles, "words": words, "interactions": pairs, "tau": out["tau"], "precedents": len(graded)}
    )


def settings(ctx: Any, inputs: dict[str, Any]) -> Result:
    """The model's levers: every setting as it stands, after putting in `changes` (checked by name and range; kept
    with the store over the spec's `settings`; a learning lever moved relearns there and then)."""
    changes = inputs.get("changes") or {}
    return Result.done({"settings": ctx.ada.configure(**changes) if changes else ctx.ada.settings.to_json(all=True)})


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

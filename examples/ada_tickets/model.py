"""tickets: an Ada model on rrotor. Each ticket recorded with how it was handled becomes a precedent; a new ticket is
answered from the precedents closest to it, cut to what the caller may see (`ctx.clearance`). The spec beside this
file (spec.json) says what a ticket looks like; the Ada part brings the kernel."""

from __future__ import annotations

from typing import Any

from rrotor import Result, ada

CONFIDENT = 0.6  # below this, decide() asks rather than guesses


def load(ctx: Any) -> None:
    ada.open(ctx, min_sim=0.35)  # three of a ticket's attributes must agree before a precedent counts


def start(ctx: Any, inputs: dict[str, Any]) -> Result:
    return Result.done({"precedents": len(ctx.ada)})


def record(ctx: Any, inputs: dict[str, Any]) -> Result:
    rid = ctx.ada.record(inputs["data"], inputs.get("outcome"))
    return Result.done({"id": rid, "precedents": len(ctx.ada)})


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


def probabilities(ctx: Any, inputs: dict[str, Any]) -> Result:
    spec, space = ctx.ada.spec, ctx.ada.space
    situation = spec.situation(inputs["data"])
    return Result.done(space.probabilities(situation, weights=spec.weights(ctx.clearance, situation)))


def decide(ctx: Any, inputs: dict[str, Any]) -> Result:
    answer = ctx.ada.query(inputs["data"], clearance=ctx.clearance)
    if answer.sufficient and answer.confidence >= CONFIDENT:
        return Result.done({"outcome": answer.top, "confidence": round(answer.confidence, 4), "from": answer.count})
    outcome = ctx.needs_input("No precedent is close enough. How should this ticket be handled?")
    rid = ctx.ada.record(inputs["data"], str(outcome))
    return Result.done({"outcome": str(outcome), "confidence": None, "from": 0, "recorded": rid})

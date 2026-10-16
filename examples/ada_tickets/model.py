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

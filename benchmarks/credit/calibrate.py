"""Calibration in the kernel, without gaming: everything is fitted on the 800 learned cases only, and the 200 held
out are queried once at the end, with the one rule those fits produce.

Four steps, each decided before the hold-out is touched:
1. `Spec.learned(cases)`: every role's weight is its information gain about the outcome over the 800, and every
   value's weight how far it moves the outcome from the base rate (both scaled to 0..1), plus the pairs of roles
   that tell more together than apart (interactions, read as one more layer). The hand-set segment weights are
   dropped. The same spec with the pairs taken out is scored beside it.
2. `k` and `min_sim` chosen by five-fold cross-validation inside the 800, by the bank's cost of `decide`.
3. `Model.calibrate()`: the kernel fits its temperature on held-out precedents of the store itself.
4. The temperature by log-loss, and once more by the bank's cost (`calibrate(by="cost")`), both reported.
5. The rule from the costs alone, in the spec: `output.costs = {good: {bad: 5}, bad: {good: 1}}`; `decide` picks
   the outcome with the least expected cost, and asks a person instead when `output.ask` (the price of asking,
   set here to 0.5, half a refused good loan, before any run) is below every outcome's expected cost.

Then the 200: record all 800 with the learned spec, calibrate, `decide` each held-out case. Last, the same model
built the way a runtime would: an unlearned spec, the 800 recorded in order with `refresh_every=100`, so the weights
and the temperature are learned from the store as it fills; the 200 decided once more."""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import run as base  # noqa: E402  (the data, the spec and the scorer; the same split)

glyphh_spec = base.glyphh_spec

COSTS = {"good": {"bad": 5.0}, "bad": {"good": 1.0}}  # deciding good when bad costs 5; bad when good costs 1
ASK = 0.5  # the price of asking a person, in the same units: half a refused good loan
GRID = [(15, 0.35), (25, 0.25), (25, 0.15), (40, 0.15), (9, 0.45)]


def spec_with_costs() -> glyphh_spec.Spec:
    obj = json.loads(json.dumps(base.SPEC))
    obj["output"] = {"report": [], "costs": COSTS, "ask": ASK}
    for layer in obj["layers"]:
        for segment in layer["segments"]:
            segment.pop("similarity_weight", None)
    return glyphh_spec.Spec.from_json(obj)


def model_of(spec: glyphh_spec.Spec, cases: list[dict], k: int, min_sim: float, **kw) -> glyphh_spec.Model:
    m = glyphh_spec.Model(spec, k=k, min_sim=min_sim, author="eval", path=None, **kw)
    for c in cases:
        m.record(base.situation(c), c["decision"])
    return m


def cost_of(decisions: list[str], truth: list[str]) -> int:
    return sum(5 for d, t in zip(decisions, truth) if d == "good" and t == "bad") + sum(
        1 for d, t in zip(decisions, truth) if d == "bad" and t == "good"
    )


def main() -> None:
    rows = base.load()
    random.Random(7).shuffle(rows)
    train, test = rows[:800], rows[800:]
    truth = [c["decision"] for c in test]

    t0 = time.time()
    spec = spec_with_costs().learned([(base.situation(c), c["decision"]) for c in train])
    roles = {
        r["name"]: r["similarity_weight"] for l in spec.to_json()["layers"] for s in l["segments"] for r in s["roles"]
    }
    print("learned role weights:", dict(sorted(roles.items(), key=lambda kv: -kv[1])))
    checking = next(
        r for l in spec.to_json()["layers"] for s in l["segments"] for r in s["roles"] if r["name"] == "checking"
    )
    print("learned value weights, checking:", checking.get("value_weights"))
    print("learned interactions:", [(i.name, i.similarity_weight) for i in spec.interactions])
    alone = spec.with_weights({"~interactions": []})  # the same weights, the pairs taken out
    print(f"learned in {time.time() - t0:.1f}s")

    # k and min_sim by cross-validated cost inside the 800
    folds = [train[i::5] for i in range(5)]
    t0 = time.time()
    best = None
    for k, min_sim in GRID:
        cost = 0
        for i, fold in enumerate(folds):
            others = [c for j, f in enumerate(folds) if j != i for c in f]
            m = model_of(spec, others, k, min_sim)
            m.calibrate(sample=len(others))
            cost += cost_of(
                [m.decide(base.situation(c))["outcome"] or "good" for c in fold], [c["decision"] for c in fold]
            )
        print(f"  k {k:2d} min_sim {min_sim}: cross-validated cost {cost} over the 800")
        if best is None or cost < best[0]:
            best = (cost, k, min_sim)
    _, k, min_sim = best
    print(f"chosen k {k}, min_sim {min_sim}, in {time.time() - t0:.0f}s")

    # the 200, once
    t0 = time.time()
    m = model_of(spec, train, k, min_sim)
    tau = m.calibrate(sample=800)
    build = time.time() - t0
    single = model_of(alone, train, k, min_sim)
    single.calibrate(sample=800)
    base.score(
        "Ada, learned spec, no interactions", [single.query(base.situation(c)).top or "good" for c in test], truth, 0
    )
    base.score(
        "  the same, calibrated, decide",
        [single.decide(base.situation(c))["outcome"] or "good" for c in test],
        truth,
        0,
        "decide(): least expected cost",
    )
    t0 = time.time()
    plain, decided, abstain = [], [], []
    for c in test:
        a = m.query(base.situation(c))
        plain.append(a.top if a.top else "good")
        d = m.decide(base.situation(c))
        decided.append(d["outcome"] or "good")
        abstain.append("ask" if d["ask"] else decided[-1])
    per = (time.time() - t0) / len(test) / 2
    print(f"build + calibrate {build:.1f}s (tau {tau}), {per:.3f}s per query")
    base.score("Ada, learned spec", plain, truth, build + per * len(test))
    base.score(
        "Ada, learned, calibrated, costs in spec",
        decided,
        truth,
        build + per * len(test),
        "decide(): least expected cost",
    )
    asked = abstain.count("ask")
    sure = [(d, t) for d, t in zip(abstain, truth) if d != "ask"]
    base.score(
        f"  the same, asking when every answer costs over {ASK}",
        [d for d, _ in sure],
        [t for _, t in sure],
        0,
        f"answered {len(sure)} of {len(test)}, asked {asked}",
    )
    tau_cost = m.calibrate(sample=800, by="cost")
    base.score(
        "  the same, tau fitted on the cost",
        [m.decide(base.situation(c))["outcome"] or "good" for c in test],
        truth,
        0,
        f"tau {tau_cost} (by loss: {tau})",
    )
    m.calibrate(sample=800)

    # the runtime's way: nothing learned up front, the store teaches the model as it fills
    t0 = time.time()
    live = model_of(spec_with_costs(), train, k, min_sim, refresh_every=100)
    build = time.time() - t0
    online = [live.decide(base.situation(c))["outcome"] or "good" for c in test]
    print(
        f"learned from the store as it filled: 8 refreshes in {build:.1f}s, tau {live.space.tau}; same decisions as above: {online == decided}"
    )
    base.score("Ada, learned from its own store (refresh_every=100)", online, truth, build, "decide()")


if __name__ == "__main__":
    main()

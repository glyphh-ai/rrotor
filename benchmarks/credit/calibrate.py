"""Calibration without gaming: everything is fitted on the 800 learned cases only, by cross-validation inside them,
and the 200 held out are queried once at the end, with the one rule those fits produce.

Three steps, each decided before the hold-out is touched:
1. Role weights from the 800 alone: how well each role's values separate good from bad (information gain), scaled
   to 0..1 and written into the spec as similarity_weight (the hand-set segment weights are dropped).
2. Five-fold cross-validation over the 800: each fold's cases queried against a model of the other four; the
   vote share for "bad" is paired with the truth, and an isotonic (monotone) map from vote share to P(bad) is fitted.
3. The decision rule from the cost matrix alone: refuse when 5 x P(bad) > 1 x P(good), i.e. P(bad) > 1/6.

Then the 200: record all 800 with the learned weights, query each held-out case, calibrate its vote, apply the rule.
"""

from __future__ import annotations

import collections
import json
import math
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import run as base  # noqa: E402  (the data, the spec and the scorer; the same split)

glyphh_spec = base.glyphh_spec

NUMERIC_BINS = {
    "amount": 1000,
    "duration_months": 6,
    "instalment_rate": 1,
    "age": 10,
    "resident_since": 1,
    "existing_credits": 1,
    "dependants": 1,
}


def entropy(counts: collections.Counter) -> float:
    n = sum(counts.values())
    return -sum(c / n * math.log2(c / n) for c in counts.values() if c)


def information_gain(cases: list[dict], role: str) -> float:
    """How much knowing the role's value (its bin, for a number) tells about the decision."""
    before = entropy(collections.Counter(c["decision"] for c in cases))
    groups: dict[object, collections.Counter] = collections.defaultdict(collections.Counter)
    for c in cases:
        v = c[role]
        if role in NUMERIC_BINS:
            v = int(v) // NUMERIC_BINS[role]
        groups[v][c["decision"]] += 1
    after = sum(sum(g.values()) / len(cases) * entropy(g) for g in groups.values())
    return before - after


def learned_spec(train: list[dict]) -> tuple[dict, dict[str, float]]:
    """The spec with every role's similarity_weight its information gain, scaled so the best role weighs 1."""
    gains = {role: information_gain(train, role) for role in base.FIELDS}
    top = max(gains.values())
    weights = {role: round(g / top, 3) for role, g in gains.items()}
    spec = json.loads(json.dumps(base.SPEC))
    for layer in spec["layers"]:
        for segment in layer["segments"]:
            segment.pop("similarity_weight", None)
            for role in segment["roles"]:
                if role["name"] in weights:
                    role["similarity_weight"] = weights[role["name"]]
    return spec, weights


def model_of(spec: dict, cases: list[dict], min_sim: float, k: int) -> glyphh_spec.Model:
    m = glyphh_spec.Model(glyphh_spec.Spec.from_json(spec), k=k, min_sim=min_sim, author="eval", path=None)
    for c in cases:
        m.record(base.situation(c), c["decision"])
    return m


def vote_bad(model: glyphh_spec.Model, c: dict) -> float:
    a = model.query(base.situation(c))
    return float(a.outcomes.get("bad", 0.0))


def isotonic(points: list[tuple[float, int]]) -> list[tuple[float, float]]:
    """Pool-adjacent-violators: a monotone map from vote share to P(bad), as (share, p) steps."""
    points = sorted(points)
    blocks = [[x, float(y), 1] for x, y in points]  # x, mean y, count
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][1] > blocks[i + 1][1]:
            a, b = blocks[i], blocks[i + 1]
            merged = [b[0], (a[1] * a[2] + b[1] * b[2]) / (a[2] + b[2]), a[2] + b[2]]
            blocks[i : i + 2] = [merged]
            i = max(i - 1, 0)
        else:
            i += 1
    return [(x, p) for x, p, _ in blocks]


def calibrated(curve: list[tuple[float, float]], share: float) -> float:
    p = curve[0][1]
    for x, value in curve:
        if share >= x:
            p = value
        else:
            break
    return p


def main() -> None:
    min_sim, k = 0.25, 25
    rows = base.load()
    random.Random(7).shuffle(rows)
    train, test = rows[:800], rows[800:]
    truth = [c["decision"] for c in test]

    spec, weights = learned_spec(train)
    print("learned weights:", dict(sorted(weights.items(), key=lambda kv: -kv[1])))

    # cross-validation inside the 800, for the calibration curve only
    t0 = time.time()
    folds = [train[i::5] for i in range(5)]
    points: list[tuple[float, int]] = []
    for i, fold in enumerate(folds):
        others = [c for j, f in enumerate(folds) if j != i for c in f]
        m = model_of(spec, others, min_sim, k)
        points += [(vote_bad(m, c), 1 if c["decision"] == "bad" else 0) for c in fold]
    curve = isotonic(points)
    print(f"calibrated on {len(points)} cross-validated votes in {time.time() - t0:.0f}s; curve has {len(curve)} steps")
    cv_right = sum((calibrated(curve, s) > 0.5) == bool(y) for s, y in points)
    print(f"cross-validation inside the 800: accuracy {cv_right / len(points):.1%} (what the 800 say about themselves)")

    # the 200, once
    t0 = time.time()
    m = model_of(spec, train, min_sim, k)
    build = time.time() - t0
    t0 = time.time()
    shares = [vote_bad(m, c) for c in test]
    per = (time.time() - t0) / len(test)
    plain = ["bad" if s > 0.5 else "good" for s in shares]
    ruled = ["bad" if calibrated(curve, s) > 1 / 6 else "good" for s in shares]
    print(f"build {build:.1f}s, {per:.2f}s per query")
    base.score("Ada, learned weights", plain, truth, build + per * len(test))
    base.score(
        "Ada, learned weights, calibrated, cost rule",
        ruled,
        truth,
        build + per * len(test),
        "refuse when calibrated P(bad) > 1/6",
    )


if __name__ == "__main__":
    main()

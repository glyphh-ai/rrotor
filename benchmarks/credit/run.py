"""German Credit, the same split for everyone: 800 to learn from, 200 held out. Writes train.jsonl, holdout.jsonl
(labels withheld) and holdout_labels.json for the LLM judge; runs Ada and a logistic regression here."""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

import numpy as np

RROTOR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RROTOR / "tests" / "kernel"))
sys.path.insert(0, str(RROTOR / "src"))
import glyphh_spec  # noqa: E402

HERE = Path(__file__).parent
LEGEND = {
    "checking": {"A11": "negative", "A12": "under 200", "A13": "200 or more", "A14": "no account"},
    "history": {
        "A30": "no credits",
        "A31": "all paid at this bank",
        "A32": "paid duly so far",
        "A33": "delays in the past",
        "A34": "critical or other bank",
    },
    "purpose": {
        "A40": "new car",
        "A41": "used car",
        "A42": "furniture",
        "A43": "radio/tv",
        "A44": "appliances",
        "A45": "repairs",
        "A46": "education",
        "A47": "vacation",
        "A48": "retraining",
        "A49": "business",
        "A410": "other",
    },
    "savings": {"A61": "under 100", "A62": "100-500", "A63": "500-1000", "A64": "1000 or more", "A65": "unknown/none"},
    "employed_since": {
        "A71": "unemployed",
        "A72": "under 1 year",
        "A73": "1-4 years",
        "A74": "4-7 years",
        "A75": "7 years or more",
    },
    "status": {
        "A91": "male divorced",
        "A92": "female",
        "A93": "male single",
        "A94": "male married",
        "A95": "female single",
    },
    "other_debtors": {"A101": "none", "A102": "co-applicant", "A103": "guarantor"},
    "property": {"A121": "real estate", "A122": "savings agreement/insurance", "A123": "car or other", "A124": "none"},
    "other_plans": {"A141": "bank", "A142": "stores", "A143": "none"},
    "housing": {"A151": "rent", "A152": "own", "A153": "free"},
    "job": {
        "A171": "unskilled non-resident",
        "A172": "unskilled resident",
        "A173": "skilled",
        "A174": "management/self-employed",
    },
    "telephone": {"A191": "none", "A192": "yes"},
    "foreign": {"A201": "yes", "A202": "no"},
}
FIELDS = [
    "checking",
    "duration_months",
    "history",
    "purpose",
    "amount",
    "savings",
    "employed_since",
    "instalment_rate",
    "status",
    "other_debtors",
    "resident_since",
    "property",
    "age",
    "other_plans",
    "housing",
    "existing_credits",
    "job",
    "dependants",
    "telephone",
    "foreign",
]
NUMERIC = {"duration_months", "amount", "instalment_rate", "resident_since", "age", "existing_credits", "dependants"}


def load() -> list[dict]:
    rows = []
    for i, line in enumerate(open(HERE / "german.data")):
        parts = line.split()
        case = {"id": f"c{i:04d}"}
        for name, raw in zip(FIELDS, parts[:20]):
            case[name] = int(raw) if name in NUMERIC else LEGEND[name][raw]
        case["decision"] = "good" if parts[20] == "1" else "bad"
        rows.append(case)
    return rows


SPEC = {
    "name": "credit",
    "seed": 3,
    "dimension": 4096,
    "layers": [
        {
            "name": "loan",
            "segments": [
                {
                    "name": "ask",
                    "roles": [
                        {"name": "purpose"},
                        {"name": "amount", "numeric_config": {"min_value": 0, "max_value": 20000, "bin_width": 1000}},
                        {
                            "name": "duration_months",
                            "numeric_config": {"min_value": 0, "max_value": 72, "bin_width": 6},
                        },
                        {"name": "instalment_rate", "numeric_config": {"min_value": 1, "max_value": 4, "bin_width": 1}},
                        {"name": "id", "key_part": True, "similarity_weight": 0},
                    ],
                },
            ],
        },
        {
            "name": "applicant",
            "segments": [
                {
                    "name": "money",
                    "similarity_weight": 1.0,
                    "roles": [
                        {"name": "checking"},
                        {"name": "savings"},
                        {"name": "history"},
                        {"name": "other_plans"},
                        {"name": "other_debtors"},
                    ],
                },
                {
                    "name": "life",
                    "similarity_weight": 0.6,
                    "roles": [
                        {"name": "employed_since"},
                        {"name": "job"},
                        {"name": "housing"},
                        {"name": "property"},
                        {"name": "age", "numeric_config": {"min_value": 18, "max_value": 78, "bin_width": 10}},
                        {"name": "status"},
                        {"name": "resident_since", "numeric_config": {"min_value": 1, "max_value": 4, "bin_width": 1}},
                        {
                            "name": "existing_credits",
                            "numeric_config": {"min_value": 1, "max_value": 4, "bin_width": 1},
                        },
                        {"name": "dependants", "numeric_config": {"min_value": 1, "max_value": 2, "bin_width": 1}},
                        {"name": "telephone"},
                        {"name": "foreign"},
                    ],
                },
            ],
        },
    ],
}


def situation(c: dict) -> dict:
    ask = {k: c[k] for k in ("purpose", "amount", "duration_months", "instalment_rate", "id")}
    money = {k: c[k] for k in ("checking", "savings", "history", "other_plans", "other_debtors")}
    life = {
        k: c[k]
        for k in (
            "employed_since",
            "job",
            "housing",
            "property",
            "age",
            "status",
            "resident_since",
            "existing_credits",
            "dependants",
            "telephone",
            "foreign",
        )
    }
    return {"loan": {"ask": ask}, "applicant": {"money": money, "life": life}}


def score(name: str, guesses: list[str], truth: list[str], seconds: float, note: str = "") -> None:
    n = len(truth)
    right = sum(g == t for g, t in zip(guesses, truth))
    bad_approved = sum(g == "good" and t == "bad" for g, t in zip(guesses, truth))
    good_refused = sum(g == "bad" and t == "good" for g, t in zip(guesses, truth))
    cost = 5 * bad_approved + good_refused  # the dataset's own cost matrix
    print(
        f"{name:28s} accuracy {right / n:.1%}   bad loans approved {bad_approved:3d}   good refused {good_refused:3d}   cost {cost:4d}   {seconds:7.1f}s  {note}"
    )


def main() -> None:
    rows = load()
    random.Random(7).shuffle(rows)
    train, test = rows[:800], rows[800:]
    (HERE / "train.jsonl").write_text("".join(json.dumps(c) + "\n" for c in train))
    (HERE / "holdout.jsonl").write_text(
        "".join(json.dumps({k: v for k, v in c.items() if k != "decision"}) + "\n" for c in test)
    )
    (HERE / "holdout_labels.json").write_text(json.dumps({c["id"]: c["decision"] for c in test}))
    truth = [c["decision"] for c in test]
    print(
        f"train {len(train)} (good {sum(c['decision'] == 'good' for c in train)}), hold out {len(test)} (good {truth.count('good')})"
    )
    score("always good", ["good"] * len(test), truth, 0)

    # Ada
    for min_sim, k in ((0.25, 25),):
        spec = glyphh_spec.Spec.from_json(SPEC)
        model = glyphh_spec.Model(spec, k=k, min_sim=min_sim, author="eval", path=None)
        t0 = time.time()
        for c in train:
            model.record(situation(c), c["decision"])
        guesses, careful, answered = [], [], 0
        t1 = time.time()
        for c in test:
            a = model.query(situation(c))
            answered += a.sufficient
            guesses.append(a.top if a.top else "good")
            careful.append("bad" if a.outcomes.get("bad", 0) > 1 / 6 else "good")
        per = (time.time() - t1) / len(test)
        score(
            f"Ada min_sim {min_sim} k {k}",
            guesses,
            truth,
            time.time() - t0,
            f"answered {answered / len(test):.0%}, {per:.2f}s per query",
        )
        score("Ada, cost-aware", careful, truth, time.time() - t0, "refuse when P(bad) > 1/6 in outcomes")

    # logistic regression, numpy
    cats = [f for f in FIELDS if f not in NUMERIC]
    values = {f: sorted({c[f] for c in rows}) for f in cats}

    def vec(c: dict) -> list[float]:
        x = [float(c[f]) for f in sorted(NUMERIC)]
        for f in cats:
            x += [1.0 if c[f] == v else 0.0 for v in values[f]]
        return x

    t0 = time.time()
    X = np.array([vec(c) for c in train])
    y = np.array([1.0 if c["decision"] == "bad" else 0.0 for c in train])
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Xn = np.c_[(X - mu) / sd, np.ones(len(X))]
    w = np.zeros(Xn.shape[1])
    for _ in range(3000):
        p = 1 / (1 + np.exp(-Xn @ w))
        w -= 0.05 * (Xn.T @ (p - y) / len(y) + 0.01 * np.r_[w[:-1], 0])
    Xt = np.c_[(np.array([vec(c) for c in test]) - mu) / sd, np.ones(len(test))]
    pt = 1 / (1 + np.exp(-Xt @ w))
    score("logistic regression", ["bad" if p > 0.5 else "good" for p in pt], truth, time.time() - t0)
    score(
        "logistic regression, cost-aware",
        ["bad" if p > 1 / 6 else "good" for p in pt],
        truth,
        time.time() - t0,
        "refuse when P(bad) > 1/6",
    )


if __name__ == "__main__":
    main()

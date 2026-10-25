"""Bots at the door: is a web session a person or a robot, from how it behaves? The same split for everyone:
10,000 sessions to learn from, 2,000 held out (plus a 200 sample with opaque ids for the LLM judge). Runs Ada
(learned weights, calibrated, `decide` under the gate's costs), a rule, and a logistic regression here; writes
train.jsonl, holdout.jsonl and the labels for the judge; writes edge_spec.json, the spec the live gate uses.

Data: `fetch.sh` (Zenodo 3477932, CC BY 4.0). Two feature files joined by session id, 67,352 sessions, 20% robots.
"""

from __future__ import annotations

import csv
import json
import math
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
DATA = HERE / "data"
RESULTS = HERE / "results"
TRAIN, HOLDOUT, SAMPLE = 10_000, 2_000, 200

# The gate's costs, in units of one bot let through: blocking a person costs five bots let in; a challenge (a
# proof of browser, a CAPTCHA) costs half a bot. decide() picks the least expected cost and asks (challenges)
# where both answers are dearer than that.
COSTS = {"bot": {"human": 5.0}, "human": {"bot": 1.0}}
ASK = 0.5

# The dataset's columns, the role each becomes, and how it is read. log: log10(1 + x), binned; rate: 0..1 in
# tenths; raw: as is. edge: the live gate at glyphh.ai can compute it from the requests alone (no response
# status, no page content, no byte counts).
FEATURES: dict[str, tuple[str, str, bool]] = {
    # column: (layer.segment, kind, edge)
    "NUMBER_OF_REQUESTS": ("pace.volume", "log", True),
    "TOTAL_DURATION": ("pace.volume", "log", True),
    "AVERAGE_TIME": ("pace.rhythm", "log", True),
    "STANDARD_DEVIATION": ("pace.rhythm", "log", True),
    "MAX_BARRAGE": ("pace.rhythm", "log", True),
    "CONSECUTIVE": ("pace.rhythm", "rate", False),
    "NIGHT": ("pace.rhythm", "rate", True),
    "REPEATED_REQUESTS": ("pace.rhythm", "rate", True),
    "GET_METHOD": ("shape.methods", "rate", True),
    "POST_METHOD": ("shape.methods", "rate", True),
    "HEAD_METHOD": ("shape.methods", "rate", True),
    "OTHER_METHOD": ("shape.methods", "rate", True),
    "IMAGES": ("shape.content", "rate", True),
    "TOTAL_HTML": ("shape.content", "rate", True),
    "HTML_TO_IMAGE": ("shape.content", "log", True),
    "HTML_TO_CSS": ("shape.content", "log", False),
    "HTML_TO_JS": ("shape.content", "log", False),
    "WIDTH": ("shape.walk", "log", True),
    "DEPTH": ("shape.walk", "raw", True),
    "STD_DEPTH": ("shape.walk", "log", True),
    "HTTP_RESPONSE_2XX": ("server.status", "rate", False),
    "HTTP_RESPONSE_3XX": ("server.status", "rate", False),
    "HTTP_RESPONSE_4XX": ("server.status", "rate", False),
    "HTTP_RESPONSE_5XX": ("server.status", "rate", False),
    "DATA": ("server.bytes", "log", False),
    "PPI": ("server.bytes", "log", False),
    "UNASSIGNED": ("server.referrer", "rate", False),
    "SF_REFERRER": ("server.referrer", "rate", False),
    "SF_FILETYPE": ("server.referrer", "rate", False),
    "PENALTY": ("server.traps", "log", False),
    "TOTAL_TOPICS": ("semantic.pages", "log", False),
    "UNIQUE_TOPICS": ("semantic.pages", "log", False),
    "PAGE_SIMILARITY": ("semantic.pages", "log", False),
    "PAGE_VARIANCE": ("semantic.pages", "log", False),
    "BOOLEAN_PAGE_VARIANCE": ("semantic.pages", "rate", False),
}
RANGES = {"log": (0.0, 6.0, 0.25), "rate": (0.0, 1.0, 0.1), "raw": (0.0, 10.0, 1.0)}


def role_name(column: str) -> str:
    return column.lower()


def value(column: str, raw: str) -> float | None:
    try:
        x = float(raw)
    except ValueError:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    kind = FEATURES[column][1]
    if kind == "log":
        return round(math.log10(1 + max(x, 0.0)), 4)
    return round(x, 4)


def load() -> list[dict]:
    """Every session: id, its outcome and its features (None where the file says NaN)."""
    semantic = {r["ID"]: r for r in csv.DictReader(open(DATA / "semantic_features.csv"))}
    rows = []
    for r in csv.DictReader(open(DATA / "simple_features.csv")):
        s = semantic[r["ID"]]
        case = {"id": r["ID"], "outcome": "bot" if r["ROBOT"] == "1" else "human"}
        for column in FEATURES:
            case[role_name(column)] = value(column, r[column] if column in r else s[column])
        case["requests"] = int(float(r["NUMBER_OF_REQUESTS"]))
        rows.append(case)
    return rows


def spec_json(edge: bool) -> dict:
    """The spec: one role per feature, in layers by what it says of the session; edge keeps the live gate's."""
    layers: dict[str, dict[str, list]] = {}
    for column, (where, kind, at_edge) in FEATURES.items():
        if edge and not at_edge:
            continue
        layer, segment = where.split(".")
        lo, hi, width = RANGES[kind]
        layers.setdefault(layer, {}).setdefault(segment, []).append(
            {"name": role_name(column), "numeric_config": {"min_value": lo, "max_value": hi, "bin_width": width}}
        )
    return {
        "name": "bots-edge" if edge else "bots",
        "seed": 3,
        "dimension": 4096,
        "layers": [
            {"name": layer, "segments": [{"name": seg, "roles": roles} for seg, roles in segs.items()]}
            for layer, segs in layers.items()
        ],
        "output": {"report": [], "costs": COSTS, "ask": ASK},
    }


def situation(case: dict, edge: bool) -> dict:
    out: dict[str, dict[str, dict[str, float]]] = {}
    for column, (where, _, at_edge) in FEATURES.items():
        if edge and not at_edge:
            continue
        v = case[role_name(column)]
        if v is None:
            continue
        layer, segment = where.split(".")
        out.setdefault(layer, {}).setdefault(segment, {})[role_name(column)] = v
    return out


def cost_of(decisions: list[str], truth: list[str]) -> float:
    return sum(COSTS.get(d, {}).get(t, 0.0) if d != "ask" else ASK for d, t in zip(decisions, truth))


def score(name: str, decisions: list[str], truth: list[str], note: str = "") -> dict:
    n = len(truth)
    asked = decisions.count("ask")
    sure = [(d, t) for d, t in zip(decisions, truth) if d != "ask"]
    right = sum(d == t for d, t in sure)
    blocked_people = sum(d == "bot" and t == "human" for d, t in sure)
    bots_in = sum(d == "human" and t == "bot" for d, t in sure)
    bots = truth.count("bot")
    caught = sum(d == "bot" and t == "bot" for d, t in sure)
    row = {
        "name": name,
        "accuracy": right / max(len(sure), 1),
        "challenged": asked / n,
        "people_blocked": blocked_people,
        "bots_in": bots_in,
        "bots_caught": caught / max(bots, 1),
        "cost": cost_of(decisions, truth),
        "note": note,
    }
    print(
        f"{name:52s} right {row['accuracy']:6.1%}  challenged {row['challenged']:5.1%}  people blocked {blocked_people:4d}"
        f"  bots in {bots_in:4d}  bots caught {row['bots_caught']:6.1%}  cost {row['cost']:7.1f}  {note}"
    )
    return row


def split(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    rows = list(rows)
    random.Random(7).shuffle(rows)
    return rows[:TRAIN], rows[TRAIN : TRAIN + HOLDOUT], rows[TRAIN : TRAIN + SAMPLE]


def write_judge_files(train: list[dict], test: list[dict], sample: list[dict]) -> None:
    """The judge's files: features as the dataset names them (no outcome in the hold-out), ids opaque."""
    fields = [role_name(c) for c in FEATURES]
    (HERE / "train.jsonl").write_text(
        "".join(json.dumps({"id": c["id"], **{f: c[f] for f in fields}, "outcome": c["outcome"]}) + "\n" for c in train)
    )
    opaque = [f"s{i:03d}" for i in range(len(sample))]
    random.Random(11).shuffle(opaque)
    (HERE / "holdout.jsonl").write_text(
        "".join(json.dumps({"id": o, **{f: c[f] for f in fields}}) + "\n" for o, c in zip(opaque, sample))
    )
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "sample_labels.json").write_text(
        json.dumps(
            {
                "labels": {o: c["outcome"] for o, c in zip(opaque, sample)},
                "ids": dict(zip(opaque, [c["id"] for c in sample])),
            }
        )
    )
    (HERE / "edge_spec.json").write_text(json.dumps(spec_json(edge=True), indent=2) + "\n")


def model_of(spec: glyphh_spec.Spec, cases: list[dict], edge: bool, **kw) -> glyphh_spec.Model:
    m = glyphh_spec.Model(spec, author="eval", path=None, **kw)
    for c in cases:
        m.record(situation(c, edge), c["outcome"])
    return m


def ada_decisions(m: glyphh_spec.Model, cases: list[dict], edge: bool) -> tuple[list[str], list[str], list[dict]]:
    """Each case's plain vote, its decision (ask where decide says so) and the full answer."""
    plain, decided, full = [], [], []
    for c in cases:
        d = m.decide(situation(c, edge))
        p = d["probabilities"]
        plain.append(max(p, key=p.get) if p else "human")
        decided.append("ask" if d["ask"] else (d["outcome"] or "human"))
        full.append({"id": c["id"], **d})
    return plain, decided, full


def logistic(train: list[dict], test: list[dict], truth: list[str], edge: bool) -> list[dict]:
    fields = [role_name(c) for c, (_, _, at_edge) in FEATURES.items() if at_edge or not edge]

    def vec(c: dict) -> list[float]:
        return [0.0 if c[f] is None else c[f] for f in fields] + [1.0 if c[f] is None else 0.0 for f in fields]

    X = np.array([vec(c) for c in train])
    y = np.array([1.0 if c["outcome"] == "bot" else 0.0 for c in train])
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Xn = np.c_[(X - mu) / sd, np.ones(len(X))]
    w = np.zeros(Xn.shape[1])
    for _ in range(3000):
        p = 1 / (1 + np.exp(-Xn @ w))
        w -= 0.05 * (Xn.T @ (p - y) / len(y) + 0.01 * np.r_[w[:-1], 0])
    Xt = np.c_[(np.array([vec(c) for c in test]) - mu) / sd, np.ones(len(test))]
    pt = 1 / (1 + np.exp(-Xt @ w))
    tag = "edge features" if edge else "every feature"
    out = [score(f"logistic regression, {tag}", ["bot" if p > 0.5 else "human" for p in pt], truth)]
    out.append(
        score(
            f"logistic regression, {tag}, cost-aware",
            ["bot" if p > 1 / 6 else "human" for p in pt],
            truth,
            "block when P(bot) > 1/6",
        )
    )
    return out


def in_flight(test: list[dict], decisions: list[str], truth: list[str], name: str) -> dict:
    """The same decisions on the sessions of five requests or more: the ones a gate meets while they run."""
    keep = [i for i, c in enumerate(test) if c["requests"] >= 5]
    return score(
        f"  {name}, sessions of 5+ requests ({len(keep)})", [decisions[i] for i in keep], [truth[i] for i in keep]
    )


def main() -> None:
    rows = load()
    train, test, sample = split(rows)
    truth = [c["outcome"] for c in test]
    write_judge_files(train, test, sample)
    print(
        f"sessions {len(rows)}: learn from {len(train)} (bots {sum(c['outcome'] == 'bot' for c in train)}), "
        f"hold out {len(test)} (bots {truth.count('bot')}), judge sample {len(sample)}; "
        f"single-request sessions in the hold-out {sum(c['requests'] == 1 for c in test)}"
    )
    table: list[dict] = []
    table.append(score("always human", ["human"] * len(test), truth))
    table.append(score("rule: one request and no gap", ["bot" if c["requests"] <= 1 else "human" for c in test], truth))
    table += logistic(train, test, truth, edge=False)
    table += logistic(train, test, truth, edge=True)

    for edge in (False, True):
        tag = "edge features" if edge else "every feature"
        t0 = time.time()
        spec = glyphh_spec.Spec.from_json(spec_json(edge)).learned([(situation(c, edge), c["outcome"]) for c in train])
        roles = {
            r["name"]: round(r["similarity_weight"], 2)
            for l in spec.to_json()["layers"]
            for s in l["segments"]
            for r in s["roles"]
        }
        print(f"learned role weights ({tag}): {dict(sorted(roles.items(), key=lambda kv: -kv[1]))}")
        print(f"learned interactions: {[(i.name, round(i.similarity_weight, 2)) for i in spec.interactions]}")
        m = model_of(spec, train, edge)
        tau = m.calibrate(sample=1000)
        built = time.time() - t0
        t0 = time.time()
        plain, decided, full = ada_decisions(m, test, edge)
        per = (time.time() - t0) / len(test)
        print(f"built and calibrated in {built:.1f}s (tau {tau}), {per * 1000:.1f} ms per decision")
        table.append(score(f"Ada, {tag}, plain vote", plain, truth))
        table.append(score(f"Ada, {tag}, decide (costs, ask {ASK})", decided, truth))
        table.append(in_flight(test, decided, truth, f"Ada, {tag}, decide"))
        forced = [d if d != "ask" else p for d, p in zip(decided, plain)]
        table.append(score(f"  Ada, {tag}, forced to answer (no challenge)", forced, truth))
        RESULTS.mkdir(exist_ok=True)
        (RESULTS / f"ada_{'edge' if edge else 'full'}.json").write_text(json.dumps({"tau": tau, "decisions": full}))
        if edge:
            # the growing store: the edge model as the live gate meets it, cases arriving one at a time, learned
            # and calibrated at each checkpoint, scored on the same hold-out before it grows further
            print("the growing store (edge features): the gate as it fills")
            fresh = glyphh_spec.Model(glyphh_spec.Spec.from_json(spec_json(edge)), author="eval", path=None)
            curve = []
            arrived = 0
            for stop in (30, 100, 300, 1000, 3000, 10000):
                for c in train[arrived:stop]:
                    fresh.record(situation(c, edge), c["outcome"])
                arrived = stop
                fresh.learn()
                fresh.calibrate(sample=min(stop, 1000))
                _, d, _ = ada_decisions(fresh, test, edge)
                row = score(f"  {stop:5d} precedents", d, truth)
                curve.append({"precedents": stop, **row})
            (RESULTS / "growing.json").write_text(json.dumps(curve, indent=1))
    (RESULTS / "table.json").write_text(json.dumps(table, indent=1))


if __name__ == "__main__":
    main()

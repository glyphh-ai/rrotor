"""A hold-out test of an Ada model on labelled GitHub issues: record a share, query the rest, score.

    python holdout.py <issues.jsonl> <task> [--holdout 0.2] [--min-sim 0.35] [--seed 1]

task: `kind` (vscode's triage outcome), `sig` (kubernetes' sig/* area), `k8s-kind` (kubernetes' kind/*).
Runs the kernel in-process, exactly as the runtime does (rrotor.ada over rrotor/tests/kernel)."""

from __future__ import annotations

import argparse
import collections
import json
import random
import re
import sys
import time
from pathlib import Path

RROTOR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RROTOR / "tests" / "kernel"))
sys.path.insert(0, str(RROTOR / "src"))

import glyphh_spec  # noqa: E402  (the vendored kernel)

SPEC = {
    "name": "issues",
    "seed": 11,
    "dimension": 4096,
    "layers": [
        {
            "name": "issue",
            "segments": [
                {
                    "name": "text",
                    "roles": [
                        {"name": "title", "text_encoding": "bag_of_words"},
                        {"name": "body", "text_encoding": "bag_of_words"},
                        {"name": "number", "key_part": True, "similarity_weight": 0},
                    ],
                },
                {
                    "name": "shape",
                    "similarity_weight": 0.4,
                    "roles": [
                        {"name": "author"},
                        {"name": "comments", "numeric_config": {"min_value": 0, "max_value": 50, "bin_width": 5}},
                        {"name": "reactions", "numeric_config": {"min_value": 0, "max_value": 50, "bin_width": 5}},
                        {"name": "template"},
                    ],
                },
            ],
        }
    ],
}

VSCODE_KIND = ["spam", "*duplicate", "info-needed", "feature-request", "bug", "invalid", "*question", "*as-designed"]
WORD = re.compile(r"[a-z][a-z0-9_.-]{2,}")


def outcome(row: dict, task: str) -> str | None:
    labels = row["labels"]
    if task == "kind":
        return next((k for k in VSCODE_KIND if k in labels), None)
    prefix = "sig/" if task == "sig" else "kind/"
    found = [l for l in labels if l.startswith(prefix)]
    return found[0] if len(found) == 1 else None


def words(text: str, limit: int) -> str:
    """The body's words, without code blocks, URLs and the template's boilerplate, cut to `limit` words."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    return " ".join(WORD.findall(text.lower())[:limit])


def case(row: dict) -> dict:
    body = row["body"] or ""
    template = "issue-form" if "###" in body else ("checklist" if "- [" in body else "free")
    return {
        "issue": {
            "text": {"title": words(row["title"], 40), "body": words(body, 80), "number": str(row["number"])},
            "shape": {
                "author": row["author_association"],
                "comments": min(row["comments"], 50),
                "reactions": min(row["reactions"], 50),
                "template": template,
            },
        }
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("task", choices=["kind", "sig", "k8s-kind"])
    ap.add_argument("--holdout", type=float, default=0.2)
    ap.add_argument("--min-sim", type=float, default=0.35)
    ap.add_argument("--k", type=int, default=15)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--min-class", type=int, default=20)
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.file)]
    cases = [(case(r), outcome(r, args.task)) for r in rows]
    cases = [(c, o) for c, o in cases if o]
    counts = collections.Counter(o for _, o in cases)
    keep = {o for o, n in counts.items() if n >= args.min_class}
    cases = [(c, o) for c, o in cases if o in keep]
    random.Random(args.seed).shuffle(cases)
    cut = int(len(cases) * (1 - args.holdout))
    train, test = cases[:cut], cases[cut:]
    print(
        f"{len(cases)} labelled issues, {len(keep)} outcomes: {dict(collections.Counter(o for _, o in cases).most_common())}"
    )
    print(f"record {len(train)}, hold out {len(test)}")

    model = glyphh_spec.Model(
        glyphh_spec.Spec.from_json(SPEC), k=args.k, min_sim=args.min_sim, author="eval", path=None
    )
    t0 = time.time()
    for c, o in train:
        model.record(c, o)
    print(f"recorded in {time.time() - t0:.1f}s")

    majority = collections.Counter(o for _, o in train).most_common(1)[0][0]
    answered = right = right_any = 0
    confusion: collections.Counter = collections.Counter()
    t0 = time.time()
    for c, o in test:
        a = model.query(c)
        guess = a.top if a.top is not None else majority
        right_any += guess == o
        if a.sufficient:
            answered += 1
            right += a.top == o
            confusion[(o, a.top)] += 1
    n = len(test)
    print(f"queried {n} in {time.time() - t0:.1f}s")
    print(f"majority-class baseline ({majority}): {sum(o == majority for _, o in test) / n:.1%}")
    print(
        f"Ada, answering only when sufficient: answered {answered / n:.1%} of cases, right on {right / max(answered, 1):.1%} of those"
    )
    print(f"Ada, forced to answer every case (its top, else the majority): {right_any / n:.1%}")
    print("most confused (true -> said):", [(f"{t} -> {s}", k) for (t, s), k in confusion.most_common(6) if t != s][:5])


if __name__ == "__main__":
    main()

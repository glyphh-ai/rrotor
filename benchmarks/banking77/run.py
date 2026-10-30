"""Banking77: route a customer's message to one of 77 intents from nothing but its text. 10,003 messages to learn
from, 3,080 held out (the set's own split; PolyAI, CC BY 4.0). Ada with one bag-of-words role, from the same 10,003
the LLM judges are given; a 200-message sample of the 3,080 (seed 7) is written out for the judges, and Ada is
scored on it too."""

from __future__ import annotations

import csv
import hashlib
import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
KERNEL = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent.parent / "tests" / "kernel"
sys.path.insert(0, str(KERNEL))
import glyphh_spec  # noqa: E402

SPEC = {
    "name": "banking77",
    "seed": 7,
    "dimension": 4096,
    "layers": [
        {
            "name": "message",
            "segments": [{"name": "body", "roles": [{"name": "text", "text_encoding": "bag_of_words"}]}],
        }
    ],
}


def load(name: str) -> list[tuple[str, str]]:
    with open(HERE / name, newline="") as f:
        return [(r["text"], r["category"]) for r in csv.DictReader(f)]


def score(name: str, guesses: list[str], truth: list[str], seconds: float, note: str = "") -> float:
    right = sum(g == t for g, t in zip(guesses, truth))
    acc = right / len(truth)
    print(f"{name:52s} accuracy {acc:6.1%}  ({right}/{len(truth)})  {seconds:7.1f}s  {note}")
    return acc


# ── Ada ─────────────────────────────────────────────────────────────────


def ada(train, test, k=15, min_sim=0.10, spec=None):
    spec = spec or glyphh_spec.Spec.from_json(SPEC)
    model = glyphh_spec.Model(spec, k=k, min_sim=min_sim, author="eval")
    t0 = time.time()
    for i, (text, label) in enumerate(train):
        model.record({"message": {"body": {"text": text}}}, label)
    build = time.time() - t0
    t0 = time.time()
    guesses, sure = [], 0
    for text, _ in test:
        a = model.query({"message": {"body": {"text": text}}})
        guesses.append(a.top or "")
        sure += bool(a.sufficient)
    per = (time.time() - t0) / len(test)
    return guesses, build, per, sure


def tune_ada(train):
    """Ada's k and min_sim, by accuracy on the same tenth of the training messages; never on the 3,080."""
    rows = list(train)
    random.Random(11).shuffle(rows)
    fit, held = rows[1000:], rows[:1000]
    best = None
    for k, min_sim in [(15, 0.10), (5, 0.10), (40, 0.10), (15, 0.05), (15, 0.20), (3, 0.05), (1, 0.05)]:
        guesses, _, _, _ = ada(fit, held, k, min_sim)
        acc = sum(g == t for g, (_, t) in zip(guesses, held)) / len(held)
        print(f"  Ada: k {k} min_sim {min_sim}: {acc:.1%} on 1,000 held inside the training set")
        if best is None or acc > best[0]:
            best = (acc, k, min_sim)
    return best[1:]


def main() -> None:
    train, test = load("train.csv"), load("test.csv")
    labels = sorted({label for _, label in train})
    print(f"{len(train)} to learn from, {len(test)} held out, {len(labels)} intents")
    truth = [label for _, label in test]
    sample = sorted(random.Random(7).sample(range(len(test)), 200))
    sample_truth = [truth[i] for i in sample]
    (HERE / "results").mkdir(exist_ok=True)
    # the judges' file: opaque ids in shuffled order (the test file is sorted by intent, so a row number leaks it)
    opaque = {i: hashlib.sha256(f"banking77-sample-{i}".encode()).hexdigest()[:8] for i in sample}
    order = list(sample)
    random.Random(2026).shuffle(order)
    json.dump(
        {"intents": labels, "sample": [{"id": opaque[i], "text": test[i][0]} for i in order]},
        open(HERE / "results" / "sample.json", "w"),
        indent=1,
    )
    json.dump(
        {"labels": {opaque[i]: truth[i] for i in order}, "rows": {opaque[i]: i for i in order}},
        open(HERE / "results" / "sample_labels.json", "w"),
        indent=1,
    )

    guesses, build, per, sure = ada(train, test)
    score(
        "Ada, one bag-of-words role (k 15, min_sim 0.10)",
        guesses,
        truth,
        build + per * len(test),
        f"build {build:.1f}s, {per * 1000:.1f} ms per query, sufficient on {sure}",
    )
    score("  the same, the 200 sample", [guesses[i] for i in sample], sample_truth, 0)
    k, min_sim = tune_ada(train)
    guesses, build, per, sure = ada(train, test, k, min_sim)
    score(
        f"Ada, k and min_sim tuned inside the training set (k {k}, min_sim {min_sim})",
        guesses,
        truth,
        build + per * len(test),
        f"sufficient on {sure}",
    )
    score("  the same, the 200 sample", [guesses[i] for i in sample], sample_truth, 0)

    # learned word weights (kernel 3.6.0): Spec.learned over the 10,003 only
    t0 = time.time()
    spec = glyphh_spec.Spec.from_json(SPEC).learned(
        [({"message": {"body": {"text": text}}}, label) for text, label in train]
    )
    table = dict(spec.layers[0].segments[0].roles[0].value_weights)
    top = sorted(table.items(), key=lambda kv: -kv[1])[:8]
    low = sorted(table.items(), key=lambda kv: kv[1])[:8]
    print(f"learned word weights in {time.time() - t0:.1f}s: {len(table)} words; heaviest {top}; lightest {low}")
    guesses, build, per, sure = ada(train, test, k, min_sim, spec)
    first = guesses
    score(
        "Ada, learned word weights (Spec.learned: no evidence to carry)",
        guesses,
        truth,
        build + per * len(test),
        f"build {build:.1f}s, {per * 1000:.1f} ms per query, sufficient on {sure}",
    )
    score("  the same, the 200 sample", [guesses[i] for i in sample], sample_truth, 0)
    json.dump({"guesses": guesses}, open(HERE / "results" / "ada_words.json", "w"))
    for name, changes in (("no lexicon", {"lexicon": False}), ("word pairs on", {"word_pairs": True})):
        other = glyphh_spec.Spec.from_json({**SPEC, "settings": changes}).learned(
            [({"message": {"body": {"text": text}}}, label) for text, label in train]
        )
        guesses, build, per, sure = ada(train, test, k, min_sim, other)
        score(f"  the same, {name}", guesses, truth, build + per * len(test), f"{per * 1000:.1f} ms per query")
        score("  the same, the 200 sample", [guesses[i] for i in sample], sample_truth, 0)

    # the runtime's way: the store teaches the model, then rewrites itself
    t0 = time.time()
    live = glyphh_spec.Model(glyphh_spec.Spec.from_json(SPEC), k=k, min_sim=min_sim, author="eval")
    for text, label in train:
        live.record({"message": {"body": {"text": text}}}, label)
    live.learn()
    build = time.time() - t0
    live.configure(word_evidence=0.0)  # the store's evidence is what Spec.learned above cannot carry: off, the same
    online = [live.query({"message": {"body": {"text": text}}}).top or "" for text, _ in test]
    score(
        "Ada, learned from its own store (learn(), the store rewritten), word evidence 0",
        online,
        truth,
        build,
        f"build + learn {build:.1f}s; same guesses as the learned spec: {online == first}",
    )
    score("  the same, the 200 sample", [online[i] for i in sample], sample_truth, 0)
    live.configure(word_evidence=2.0)  # the default: what each word says of each outcome weighs on the vote
    t0 = time.time()
    evidenced = [live.query({"message": {"body": {"text": text}}}).top or "" for text, _ in test]
    per = (time.time() - t0) / len(test)
    score(
        "Ada, learned from its own store, word evidence 2 (kernel 3.9.0)",
        evidenced,
        truth,
        per * len(test),
        f"{per * 1000:.1f} ms per query",
    )
    score("  the same, the 200 sample", [evidenced[i] for i in sample], sample_truth, 0)
    json.dump({"guesses": evidenced}, open(HERE / "results" / "ada_evidence.json", "w"))
    score("  the same, the 200 sample", [guesses[i] for i in sample], sample_truth, 0)
    json.dump({"guesses": guesses}, open(HERE / "results" / "ada.json", "w"))


if __name__ == "__main__":
    main()

"""The 200 sample: the LLM judge's answers (results/llm_runN.json) against Ada's on the same sessions, from the
decisions run.py wrote; and Ada handing the sessions it challenges to the judge."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run as base  # noqa: E402

HERE = Path(__file__).parent
labels = json.load(open(HERE / "results/sample_labels.json"))
truth = {opaque: label for opaque, label in labels["labels"].items()}
real_id = labels["ids"]  # opaque → the dataset's id
order = list(truth)
t = [truth[o] for o in order]
requests = {c["id"]: c["requests"] for c in base.load()}
flight = [i for i, o in enumerate(order) if requests[real_id[o]] >= 5]
print(f"the 200 sample: {t.count('bot')} bots; {len(flight)} sessions of 5+ requests")

ada = {}
for name in ("full", "edge"):
    by_id = {d["id"]: d for d in json.load(open(HERE / f"results/ada_{name}.json"))["decisions"]}
    ada[name] = ["ask" if by_id[real_id[o]]["ask"] else (by_id[real_id[o]]["outcome"] or "human") for o in order]
    base.score(
        f"Ada, {'every' if name == 'full' else 'edge'} feature{'s' if name == 'full' else ''}, decide", ada[name], t
    )
    base.score(
        f"  the same, sessions of 5+ requests ({len(flight)})", [ada[name][i] for i in flight], [t[i] for i in flight]
    )

for path in sys.argv[1:] or ["results/llm_run1.json"]:
    judge = json.load(open(HERE / path))
    j = [judge[o] for o in order]
    base.score(f"LLM judge ({path})", j, t)
    base.score(f"  the same, sessions of 5+ requests ({len(flight)})", [j[i] for i in flight], [t[i] for i in flight])
    for name in ("full", "edge"):
        routed = [a if a != "ask" else b for a, b in zip(ada[name], j)]
        base.score(
            f"Ada ({name}) decides, the judge takes what it challenges",
            routed,
            t,
            f"judge saw {ada[name].count('ask')} of 200",
        )

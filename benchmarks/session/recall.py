"""Which earlier turns of a session does a new turn need? After the fact we know: the turns that touched the files
this turn edits or reads. Before the fact a retriever has only the new request, the folder and the branch. Recall@k
of four retrievers over every real session on this machine with 10+ turns: the last k turns (what compaction keeps),
TF-IDF over the request text, Ada (a precedent per turn: the request, where, what it touched), and the oracle."""

import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tests" / "kernel"))
import glyphh_spec as S  # noqa: E402

K = int(sys.argv[1]) if len(sys.argv) > 1 else 5
WORD = re.compile(r"[a-z0-9_]+")
SPEC = {
    "name": "session",
    "seed": 3,
    "dimension": 4096,
    "layers": [
        {
            "name": "ask",
            "segments": [{"name": "text", "roles": [{"name": "request", "text_encoding": "bag_of_words"}]}],
        },
        {"name": "where", "segments": [{"name": "place", "roles": [{"name": "cwd"}, {"name": "branch"}]}]},
        {
            "name": "touched",
            "segments": [
                {"name": "files", "roles": [{"name": "paths"}, {"name": "dirs"}]},
                {"name": "ran", "roles": [{"name": "commands"}]},
            ],
        },
    ],
    "settings": {"k": K, "min_sim": 0.0, "word_evidence": 0.0},
}


def paths_of(turn):
    found = set()
    for kind, target in turn["finds"]:
        if kind == "Read" and target.startswith("/"):
            found.add(target)
        if kind == "Bash":
            found.update(p for p in re.findall(r"(/[\w./-]+\.[a-z]{1,5})", target))
    found.update(p for p in turn["edits"] if p)
    return {p for p in found if "/tmp/" not in p and "/.claude/" not in p}


def data_of(turn, with_touched):
    d = {
        "ask": {"text": {"request": turn["request"][:1500]}},
        "where": {"place": {"cwd": turn["cwd"] or "?", "branch": turn["branch"] or "?"}},
    }
    if with_touched:
        paths = sorted(paths_of(turn))
        if paths or turn["commands"]:
            d["touched"] = {
                "files": {
                    "paths": [Path(p).name for p in paths][:40],
                    "dirs": sorted({str(Path(p).parent.name) for p in paths})[:20],
                },
                "ran": {"commands": sorted({c.split()[0] for c in turn["commands"] if c.strip()})[:20]},
            }
    return d


sessions = defaultdict(list)
for line in open(Path(__file__).parent / "turns.jsonl"):
    t = json.loads(line)
    if not t["request"].startswith("<") and not t["request"].startswith("[Request interrupted"):
        sessions[t["session"]].append(t)
sessions = {s: ts for s, ts in sessions.items() if len(ts) >= 10}

hits = Counter()
total = Counter()
cases = 0
sizes = []
for sid, turns in sessions.items():
    model = S.Model(S.Spec.from_json(SPEC), author="eval")
    docs = []  # for tf-idf: bags of the prior turns' request words
    for i, t in enumerate(turns):
        mine = paths_of(t)
        needed = set()  # for each file this turn touches, the last earlier turn that touched it
        for f in mine:
            last = max((j for j in range(i) if f in paths_of(turns[j])), default=None)
            if last is not None:
                needed.add(last)
        if needed and i >= 3:
            cases += 1
            total["all"] += len(needed)
            recent = set(range(max(0, i - K), i))
            hits["last k"] += len(needed & recent)
            # tf-idf over request words
            q = Counter(WORD.findall(t["request"].lower()))
            df = Counter(w for d in docs for w in set(d))
            scores = []
            for j, d in enumerate(docs):
                s = sum(q[w] * d[w] * math.log(len(docs) / (1 + df[w])) for w in q if w in d)
                scores.append((s, j))
            top = {j for s, j in sorted(scores, reverse=True)[:K] if s > 0}
            hits["tf-idf"] += len(needed & top)
            # Ada: query with what is known before the turn is worked
            a = model.query(data_of(t, with_touched=False))
            got = {int(r[0][1:]) for r in a.receipts[:K]}
            hits["Ada"] += len(needed & got)
            # Ada + the last 2 turns (recency as a given)
            hits["Ada + last 2"] += len(needed & (got | set(range(max(0, i - 2), i))))
            hits["oracle"] += min(len(needed), K)
        # record this turn as a precedent (its outcome: the folder it worked in)
        model.record(data_of(t, with_touched=True), None, ts=float(i))
        model.space._ids[-1] = f"t{i}"  # the receipt names the turn
        docs.append(Counter(WORD.findall(t["request"].lower())))
        sizes.append(len(json.dumps(data_of(t, True))) / 4)
print(f"sessions {len(sessions)}, turns that needed earlier turns {cases}, needed turns {total['all']}, k={K}")
for name in ("last k", "tf-idf", "Ada", "Ada + last 2", "oracle"):
    print(f"  {name:14s} recall@{K} {hits[name] / total['all']:.1%}")
print(f"a precedent carried into context: median {int(np.median(sizes))} tokens, p90 {int(np.percentile(sizes, 90))}")

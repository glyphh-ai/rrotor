"""Ada decides where it is sure, Claude where it is not: the accuracy and the LLM share on the 200 sample, from
the judge answers already on disk, at each confidence bar."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run as base  # noqa: E402

S = base.glyphh_spec
train, test = base.load("train.csv"), base.load("test.csv")
sample = json.load(open("results/sample.json"))["sample"]
truth = list(json.load(open("results/sample_labels.json"))["labels"].values())
sample_ids = [s["id"] for s in sample]
truth = [json.load(open("results/sample_labels.json"))["labels"][i] for i in sample_ids]
judges = sys.argv[1:] or ["results/llm_run2.json"]
model = S.Model(S.Spec.from_json(base.SPEC), author="eval")
for text, label in train:
    model.record({"message": {"body": {"text": text}}}, label)
model.learn()
model.calibrate(sample=1000)
probs = [model.probabilities({"message": {"body": {"text": s["text"]}}}) for s in sample]
ada = [max(p, key=p.get) if p else "" for p in probs]
conf = [max(p.values()) if p else 0.0 for p in probs]
print(f"Ada alone {sum(a == t for a, t in zip(ada, truth)) / 2:.1f}%")
for name in judges:
    claude = json.load(open(name))
    print(f"== {name}: alone {sum(claude[s['id']] == t for s, t in zip(sample, truth)) / 2:.1f}%")
    for bar in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        routed = [a if c >= bar else claude[s["id"]] for a, c, s in zip(ada, conf, sample)]
        to_llm = sum(c < bar for c in conf)
        right = sum(r == t for r, t in zip(routed, truth))
        ada_right = sum(a == t for a, c, t in zip(ada, conf, truth) if c >= bar)
        print(
            f"bar {bar:<5} Ada takes {200 - to_llm:3d} ({ada_right / max(200 - to_llm, 1):.1%} right), LLM {to_llm:3d}: together {right / 2:.1f}% right, LLM share {to_llm / 2:.0f}%"
        )

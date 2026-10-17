"""Score an answers file ({"i": intent}) against the 200-message sample: accuracy and what was missing."""

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sample = json.loads((HERE / "results" / "sample.json").read_text())
truth = dict(
    zip(
        (str(s["i"]) for s in sample["sample"]),
        json.loads((HERE / "results" / "sample_labels.json").read_text())["labels"],
    )
)
intents = set(sample["intents"])
for name in sys.argv[1:]:
    answers = json.loads(Path(name).read_text())
    right = sum(answers.get(i) == t for i, t in truth.items())
    off = sum(a not in intents for a in answers.values())
    print(
        f"{name}: accuracy {right / len(truth):.1%} ({right}/{len(truth)})  missing {sum(i not in answers for i in truth)}  not an intent {off}"
    )

"""Score an answers file ({id: "good"|"bad"}) against the hold-out: accuracy and the bank's cost."""

import json
import sys
from pathlib import Path

truth = json.loads((Path(__file__).parent / "results" / "holdout_labels.json").read_text())
for name in sys.argv[1:]:
    answers = json.loads(Path(name).read_text())
    n = len(truth)
    right = sum(answers.get(i) == t for i, t in truth.items())
    bad_approved = sum(answers.get(i) == "good" and t == "bad" for i, t in truth.items())
    good_refused = sum(answers.get(i) == "bad" and t == "good" for i, t in truth.items())
    print(
        f"{name}: accuracy {right / n:.1%}  bad approved {bad_approved}  good refused {good_refused}  cost {5 * bad_approved + good_refused}  missing {sum(i not in answers for i in truth)}"
    )

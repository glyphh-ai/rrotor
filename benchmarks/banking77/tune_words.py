"""How a learned word weight becomes its voice in the bag's vote: voice = floor + (1 - floor) * weight ** power,
chosen by accuracy on 1,000 messages held inside the 10,003 (never the 3,080)."""

import random
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import run as base  # noqa: E402

glyphh_spec = base.glyphh_spec


def voiced(spec, floor, power):
    obj = spec.to_json()
    role = obj["layers"][0]["segments"][0]["roles"][0]
    role["value_weights"] = {w: round(floor + (1 - floor) * v**power, 4) for w, v in role["value_weights"].items()}
    return glyphh_spec.Spec.from_json(obj)


train = base.load("train.csv")
rows = list(train)
random.Random(11).shuffle(rows)
fit, held = rows[1000:], rows[:1000]
learned = glyphh_spec.Spec.from_json(base.SPEC).learned([({"message": {"body": {"text": t}}}, l) for t, l in fit])
plain, _, _, _ = base.ada(fit, held)
print(f"plain words: {sum(g == t for g, (_, t) in zip(plain, held)) / 10:.1f}%")
for floor in (0.02, 0.1, 0.2, 0.35, 0.5):
    for power in (1.0, 0.5, 0.25):
        t0 = time.time()
        guesses, _, _, _ = base.ada(fit, held, spec=voiced(learned, floor, power))
        acc = sum(g == t for g, (_, t) in zip(guesses, held)) / len(held)
        print(f"floor {floor:<5} power {power:<5}: {acc:.1%}   ({time.time() - t0:.0f}s)", flush=True)

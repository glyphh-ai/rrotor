"""word_evidence (how much a query's words, by what each says of every outcome, weigh on the vote): chosen by
accuracy on 1,000 messages held inside the 10,003 (never the 3,080), learned word weights and the lexicon on."""

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run as base  # noqa: E402

glyphh_spec = base.glyphh_spec

train = base.load("train.csv")
rows = list(train)
random.Random(11).shuffle(rows)
fit, held = rows[1000:], rows[:1000]
spec = glyphh_spec.Spec.from_json(base.SPEC)
model = glyphh_spec.Model(spec, author="eval")
for text, label in fit:
    model.record({"message": {"body": {"text": text}}}, label)
model.learn()
for strength in (0.0, 0.25, 0.5, 1.0, 2.0, 3.0):
    t0 = time.time()
    model.configure(word_evidence=strength)
    guesses = [model.query({"message": {"body": {"text": text}}}).top or "" for text, _ in held]
    acc = sum(g == t for g, (_, t) in zip(guesses, held)) / len(held)
    print(f"word_evidence {strength:<4}: {acc:.1%}   ({time.time() - t0:.0f}s)", flush=True)

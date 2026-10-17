"""Lexicon on or off, word pairs on or off: chosen by accuracy on 1,000 messages held inside the 10,003 (never
the 3,080), with the learned word weights on throughout."""

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
for lexicon in (False, True):
    for pairs in (False, True):
        t0 = time.time()
        spec = glyphh_spec.Spec.from_json({**base.SPEC, "settings": {"lexicon": lexicon, "word_pairs": pairs}})
        learned = spec.learned([({"message": {"body": {"text": t}}}, l) for t, l in fit])
        guesses, _, _, _ = base.ada(fit, held, spec=learned)
        acc = sum(g == t for g, (_, t) in zip(guesses, held)) / len(held)
        n_pairs = sum("+" in k for k, _ in learned.layers[0].segments[0].roles[0].value_weights)
        print(
            f"lexicon {str(lexicon):<5} word pairs {str(pairs):<5}: {acc:.1%}  ({n_pairs} pairs, {time.time() - t0:.0f}s)",
            flush=True,
        )

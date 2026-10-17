# Banking77: routing a customer's message from its text alone

The counterpart to [`../credit`](../credit/README.md). There the cases were typed fields and Ada came first. Here
the case is nothing but free text, which is where a developer reaches for a language model today, and the question
is the same: can a precedent store route a message as well as Claude, deterministically, for nothing per call?

**The answer, honestly: closer than it was, not there.** On this data Claude routes 97% right, a classic text
model 90%, and Ada 83% once it learned which words matter (77% before it could). This page is kept because a
benchmark that only shows wins is not a benchmark; the second half of it is what the first half taught the kernel.

## The test

- **Data:** [Banking77](https://github.com/PolyAI-LDN/task-specific-datasets) (PolyAI, CC BY 4.0): 13,083 real
  messages from a bank's customers, each labelled with one of 77 intents (`card_arrival`, `lost_or_stolen_card`,
  `exchange_rate`, `top_up_failed`, …). Short, informal, often ambiguous between near-neighbour intents.
- **Split:** the set's own, 10,003 to learn from and 3,080 held out. Every contestant sees the same 10,003 and is
  scored on the same 3,080; a 200-message sample of the 3,080 (seed 7) is what the LLM judges route, and every
  model is scored on that sample too.
- **Ada:** one bag-of-words role (`text_encoding: "bag_of_words"`), the 10,003 recorded as precedents, each
  held-out message answered by the vote of its nearest precedents. `k` and `min_sim` were put to a grid on 1,000
  messages held inside the 10,003; the defaults (15, 0.10) won. Then, since kernel 3.6.0, the same with **learned
  word weights**: `Spec.learned` over the 10,003 gives every word a weight, and the bag is bundled by them.
- **Classic models:** multinomial naive Bayes, and TF-IDF with softmax regression (numpy, no library), its
  epochs, learning rate and L2 chosen on the same 1,000 inside the 10,003.
- **The LLM:** two fresh Claude Fable 5.1 agents (effort medium), each given the 10,003 as a file and the 77
  names, told to route the 200 by judgement alone (`judge_prompt.md`), no code allowed.

Nothing was tuned on the 3,080 or on the 200.

## Results

| | 3,080 held out | The 200 sample |
|---|---|---|
| Ada, one bag-of-words role, every word the same | 77.2% | 77.0% |
| Multinomial naive Bayes | 78.8% | 78.5% |
| **Ada, learned word weights** (kernel 3.6.0) | **83.1%** | 87.5% |
| Ada, the same, learned from its own store (`learn()`, the store rewritten) | 83.1% | 87.5% |
| TF-IDF + softmax regression, tuned inside the training set | 90.3% | 92.0% |
| Claude, run 1 (read ~8,100 of the 10,003) | — | 97.0% |
| Claude, run 2 (read all 10,003) | — | 97.5% |

The two Claude runs agreed with each other on 199 of the 200. The published state of the art on this set, fine-tuned
BERT-class models, is around 93% on the 3,080; Claude's 97% is on a 200 sample and reads a little high for the same
reason every sample does, but the gap to everything else is real.

## Cost and time

| | Build | Per message | Cost per message |
|---|---|---|---|
| Ada | 4 s, once (7 s with `learn()`) | 5.9 ms | ~$0.00000003 on a $0.019/h machine |
| Naive Bayes | instant | 0.1 ms | ~0 |
| TF-IDF softmax | 12 s, once | 0.01 ms | ~0 |
| Claude, batched as here (200 at once, examples read once) | — | ~1,800 tokens, 0.6 s | ≈ $0.01–$0.03 |
| Claude, as an app would call it (the examples resent) | — | ~150,000 tokens | ≈ $0.75–$2.25 |

Claude's two runs: 359,197 and 384,065 tokens, 108 s each (the token price bracketed by the same two public rates
as the credit page, $5/M in and $25/M out to $15/M in and $75/M out). Ada's ingest is paid once.

## What it says

- **Text is a different problem from fields, and the first lesson was which words matter.** With every word the
  same, "card", "my" and "the" weighed as much as "stolen" and Ada sat at 77%. Learned word weights took it to
  83%, past naive Bayes; the classic model's remaining 7 points are a weight per word *per intent*, which a
  similarity has no place for.
- **The second lesson was how a weight becomes a voice.** The first weighting, information gain, made it worse
  (48%): with 77 outcomes, gain crowns the words that split them into big groups ("card", "top", "up"), not the
  ones that name one. Weighing a word by how far it moves some outcome from its base rate fixed the ranking and
  still lost 10 points (67%), because a bag's glyph is a majority vote and two words at 1.0 drown twenty at 0.02.
  A floor of 0.35 under every word, chosen on 1,000 messages held inside the training set (`tune_words.py`:
  plain 76.3%, floors and powers from 70.8% to 83.4%), is what let the quiet words keep voting. Both wrong turns
  are reported because each was measured on the hold-out once before the next was tried; the floor was not.
- **The LLM is in its element.** Routing a short message to a named intent is reading comprehension, and the
  judges leaned on the training file for the near-neighbour pairs (card_arrival against card_delivery_estimate,
  the three top-up intents). They were also consistent with each other, which they were not on the loans.
- **Ada is still deterministic, free per call and gives receipts**, and 77% from one role and no learning is not
  nothing; but a developer with this case should use the classic model or the LLM today.

## What would move Ada

1. **Word pairs.** The interactions the credit page added, for words ("top up" + "failed"; "card" + "arrive").
2. **A weight per word per outcome.** What the classic model has and a similarity does not: the kernel's
   probabilities could weigh a precedent's vote by how its words bear on *its* outcome.
3. **The NSM layer.** The kernel's primes could stand in for a word's meaning, so "stolen" and "taken" meet.

Done on this page: learned word weights (kernel 3.6.0), 77% to 83%.

## Reproduce

```sh
benchmarks/banking77/fetch.sh                      # train.csv, test.csv
uv run python benchmarks/banking77/run.py          # Ada, plain and with learned words, the classic models, the grids; ~6 minutes
uv run python benchmarks/banking77/tune_words.py   # how a word's weight became its voice, inside the training set
uv run python benchmarks/banking77/score.py benchmarks/banking77/results/llm_run1.json   # a judge's answers
```

`results/` holds the 200 sample and its labels, both judges' answers, and Ada's 3,080 guesses.

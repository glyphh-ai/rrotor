# Banking77: routing a customer's message from its text alone

The counterpart to [`../credit`](../credit/README.md). There the cases were typed fields and Ada came first. Here
the case is nothing but free text, which is where a developer reaches for a language model today, and the question
is the same: can a precedent store route a message as well as Claude, deterministically, for nothing per call?

**The answer, honestly: nearly there, and together, past it.** On this data the best Claude routes 96.5% right,
a classic text model 90%, and Ada 88% once it learned which words matter and what each says of each outcome (77%
at the start of the page). Ada deciding where it is sure and handing the rest to Claude routes 97.5% with Claude
seeing a fifth of the traffic. This page is kept because a benchmark that only shows wins is not a benchmark;
most of it is what each run taught the kernel, including the sample that leaked and had to be reissued.

## The table

Routing 200 real bank messages to one of 77 intents, each message its own call as a chatbot makes them, every
system given the same 10,003 past cases, scored on the same 200 (opaque ids, nothing tuned on them). Dollars at the
models' list prices.

| | Accuracy | Cost for the 200 | Cost per 1,000 messages | Same answer twice? |
|---|---|---|---|---|
| Claude Fable 5.1 alone | 96.5% | $300 | $1,500 | 199 of 200 |
| Claude Opus 5.5 alone | 96.0% | $120 | $600 | |
| Claude Sonnet 5.5 alone | 86.0% | $60 | $300 | |
| Claude Haiku 4.5 alone | 81.0% | $30 | $150 | |
| Ada alone | 90.0% | $0.000006 | $0.00003 | always |
| **Ada, then Fable for the 22% it is unsure of** | **97.5%** | **$0.22** | **$1.10** | Ada's share always |
| Ada, then Opus | 97.0% | $0.09 | $0.44 | |
| Ada, then Sonnet | 94.0% | $0.04 | $0.22 | |
| Ada, then Haiku | 91.5% | $0.02 | $0.11 | |

Ada in front of the model: more accurate than the best model alone, a thousand times cheaper, deterministic and
with receipts for everything it decides itself, and every hand-off recorded so the model's share falls over time.
The rest of this page is how it got there and what it does not prove.

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
- **The LLMs:** fresh Claude agents (effort medium), each given the 10,003 as a file and the 77 names, told to
  route the 200 by judgement alone (`judge_prompt.md`), no code allowed: Fable 5.1 twice, Opus 5.5, Sonnet 5.5
  and Haiku 4.5 once each.

Nothing was tuned on the 3,080 or on the 200. **The sample was reissued once:** the first round's ids were the
test file's row numbers, in order, and that file is sorted by intent, so the id leaked the label (Opus noticed).
Every judge had said it routed by the text, but the numbers could no longer prove it; the same 200 messages were
given opaque ids in shuffled order and every judge rerun. The first round is in `results/leaky/`, superseded,
and scored a point higher than the clean one.

## Results

| | 3,080 held out | The 200 sample |
|---|---|---|
| Ada, one bag-of-words role, every word the same (kernel 3.5.0) | 77.2% | 77.0% |
| the same, read through the seeded lexicon (kernel 3.8.0) | 77.7% | 78.5% |
| Multinomial naive Bayes | 78.8% | 78.5% |
| Ada, learned word weights, the lexicon (kernel 3.8.0) | 83.3% | 88.0% |
| the same, without the lexicon | 83.1% | 87.5% |
| the same, word pairs on | 83.1% | 88.0% |
| Ada, the same, learned from its own store, word evidence 0 | 83.3% | 88.0% |
| **Ada, learned from its own store, word evidence 2** (kernel 3.9.0) | **88.2%** | 90.0% |
| TF-IDF + softmax regression, tuned inside the training set | 90.3% | 92.0% |
| Claude Fable 5.1, two runs (read all 10,003) | — | 96.5%, 96.5% |
| Claude Opus 5.5 (read all 10,003) | — | 96.0% |
| Claude Sonnet 5.5 (read none: zero-shot) | — | 86.0% |
| Claude Haiku 4.5 (read ~5,500) | — | 81.0% |

The two Fable runs agreed with each other on 199 of the 200. The published state of the art on this set, fine-tuned
BERT-class models, is around 93% on the 3,080; Fable's 96.5% is on a 200 sample and reads a little high for the same
reason every sample does, but the gap to everything else is real. Sonnet chose not to read the examples at all and
paid eleven points for it; Haiku read half and paid more. Ada with word evidence, at 90.0% on the same 200, is
above both of the smaller models and four points under the largest.

## Ada and the LLM together

The question a developer actually has is not Ada or the LLM. It is how to route a thousand messages a day without
paying the LLM to re-read ten thousand examples a thousand times. The answer is a dial: Ada decides where it is
sure, and hands the rest to the LLM with its three nearest precedents in the prompt instead of the file. Every
answer the LLM gives is recorded, so next time that case is Ada's.

On the 200 sample, with Ada's calibrated probability as the bar and each model's answers as the hand-off (all
already on disk; `hybrid.py`, nothing tuned on the sample). Ada keeps 156 of the 200 at a bar of 0.7 and is right
on 98.7% of them; the other 44 go to the model:

| Hand-off model | Alone | With Ada, bar 0.7 | LLM share |
|---|---|---|---|
| Fable 5.1 | 96.5% | **97.5%** | 22% |
| Opus 5.5 | 96.0% | 97.0% | 22% |
| Sonnet 5.5 | 86.0% | 94.0% | 22% |
| Haiku 4.5 | 81.0% | 91.5% | 22% |
| none (Ada alone) | 90.0% | | 0% |

With the two largest models the pair is more accurate than the model alone; with the two smaller ones it adds
eight to ten points, because Ada keeps the easy 78% and the small model only has to be right on the hard 22%.
The bar is a dial, from Ada alone at 90.0% (bar 0) through 95.0% at 0.5, 97.5% at 0.7 and 0.8, to the model
alone: the user's choice between a dearer misroute and a dearer LLM.

The LLM's fifth is cheap: a call carries the message and three precedents with their paths, a few hundred tokens,
not the 150,000 of re-sending the examples. For the 200, each case its own call as a chatbot makes them, at the
models' list prices (Fable $10/$50, Opus $4/$20, Sonnet $2/$10, Haiku $1/$5 per million tokens in/out):

| | Fable 5.1 | Opus 5.5 | Sonnet 5.5 | Haiku 4.5 |
|---|---|---|---|---|
| LLM alone: 200 calls, the examples sent each time (~150k in, 20 out) | $300 | $120 | $60 | $30 |
| Both at 0.7: 44 calls of ~400 in, 20 out | $0.22 | $0.09 | $0.04 | $0.02 |
| Ada alone: 200 queries, 6 ms each on a $0.019/h machine | $0.000006 | | | |

So on these 200 the pair is a thousand times cheaper than the model alone and at least as accurate, and each
hand-off is recorded, so the 22% falls over time.

The bar is the user's: higher for a dearer misroute, lower for a cheaper LLM. The same dial is `output.ask` in the
spec, priced in the bank's own units, as the credit page uses it.

## Cost and time

| | Build | Per message | Cost per message |
|---|---|---|---|
| Ada | 4 s, once (7 s with `learn()`) | 5.9 ms | ~$0.00000003 on a $0.019/h machine |
| Naive Bayes | instant | 0.1 ms | ~0 |
| TF-IDF softmax | 12 s, once | 0.01 ms | ~0 |
| Fable, batched as the judges ran (200 at once, examples read once) | — | ~1,950 tokens, 0.9 s | ≈ $0.02 |
| Fable, as a chatbot calls it (the examples resent) | — | ~150,000 tokens | ≈ $1.50 |

The judge runs, at list prices: Fable 391,685 and 389,260 tokens, 183 and 160 s, $4.23 and $4.20; Opus 390,963
tokens, 153 s, $1.69; Sonnet 56,828 tokens, 38 s, $0.12; Haiku 125,230 tokens, 40 s, $0.14. Ada's ingest is paid
once.

## What it says

- **Text is a different problem from fields, and the first lesson was which words matter.** With every word the
  same, "card", "my" and "the" weighed as much as "stolen" and Ada sat at 77%. Learned word weights took it to
  83%, past naive Bayes; the classic model's remaining 7 points are a weight per word *per intent*, which a
  similarity has no place for.
- **The third lesson was the one the classic model had all along: a weight per word per outcome.** Kernel
  3.9.0's `learn` keeps what each word says of each outcome (counts, with the store), and at query time a
  message's words weigh on the vote: each voter's weight scaled by exp(`word_evidence` × the mean pointwise
  mutual information of the words with the voter's outcome), in `query`, `probabilities`, `decide` and the
  calibration alike, the similarity bar and receipts untouched. Chosen inside the training set
  (`tune_evidence.py`: 0 → 83.4%, 0.5 → 85.7%, 1 → 87.5%, 2 → 88.0%, 3 → 87.8%), run once on the hold-out:
  83.3% to 88.2%, two points from the tuned classic model, still deterministic and with receipts. The evidence
  lives with the store, so it is what `learn()` on the runtime gives and what a spec written by hand cannot.
- **The seeds and the pairs are levers, not wins, here.** Kernel 3.8.0 ships Ada's two seeds (WordNet 3.0's
  77,817 words with their synonyms and near words, the Moby list's 192,960 forms with their parts of speech), and
  every text role reads its words through them, so "arrive" and "come" correlate. On this data that is worth half
  a point on plain bags and a tenth of one on learned bags: a bank's customers say "card", "transfer" and "top
  up", and no synonym settles which of 77 intents they mean. Word pairs ("top" + "up", learned among the common
  words that a partner settles) moved nothing on the hold-out and took 0.1 to 0.4 inside the training set, so
  they default off. Both were chosen on the 1,000 inside the training set first (`tune_reading.py`) and the
  hold-out run once; both stay as settings (`lexicon`, `word_pairs`, `max_word_pairs`) for a model whose words
  are rarer or whose intents are phrases.
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
- **Alone, Ada is two points under the tuned classic model and six under the best LLM;** in front of the LLM it
  is ahead of both, deterministic and with receipts for the share it decides.

## What would move Ada

1. **Phrases, not pairs.** "top up" is one token to a person; a tokenizer that keeps the set's own phrases
   would do what word pairs were meant to.

## Reproduce

```sh
benchmarks/banking77/fetch.sh                      # train.csv, test.csv
uv run python benchmarks/banking77/run.py          # Ada, plain and with learned words, the classic models, the grids; ~6 minutes
uv run python benchmarks/banking77/tune_words.py   # how a word's weight became its voice, inside the training set
uv run python benchmarks/banking77/tune_reading.py # the lexicon and word pairs, on and off, inside the training set
uv run python benchmarks/banking77/tune_evidence.py # word_evidence from 0 to 3, inside the training set
uv run python benchmarks/banking77/hybrid.py        # Ada and Claude together, at each confidence bar, on the 200
uv run python benchmarks/banking77/score.py benchmarks/banking77/results/clean/fable1.json   # a judge's answers
```

`results/` holds the 200 sample (opaque ids, shuffled) and its labels, the five judges' answers in `clean/`, the
superseded first round in `leaky/`, and Ada's 3,080 guesses (learned words; with word evidence).

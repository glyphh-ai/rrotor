# Banking77: routing a customer's message from its text alone

The counterpart to [`../credit`](../credit/README.md). There the cases were typed fields and Ada came first. Here
the case is nothing but free text, which is where a developer reaches for a language model today, and the question
is the same: can a precedent store route a message as well as Claude, deterministically, for nothing per call?

**The answer, honestly: not yet.** On this data Claude routes 97% right, a classic text model 90%, and Ada 77%.
This page is kept because a benchmark that only shows wins is not a benchmark. What would move Ada is named at the
end, and it is one specific thing.

## The test

- **Data:** [Banking77](https://github.com/PolyAI-LDN/task-specific-datasets) (PolyAI, CC BY 4.0): 13,083 real
  messages from a bank's customers, each labelled with one of 77 intents (`card_arrival`, `lost_or_stolen_card`,
  `exchange_rate`, `top_up_failed`, …). Short, informal, often ambiguous between near-neighbour intents.
- **Split:** the set's own, 10,003 to learn from and 3,080 held out. Every contestant sees the same 10,003 and is
  scored on the same 3,080; a 200-message sample of the 3,080 (seed 7) is what the LLM judges route, and every
  model is scored on that sample too.
- **Ada:** one bag-of-words role (`text_encoding: "bag_of_words"`), the 10,003 recorded as precedents, each
  held-out message answered by the vote of its nearest precedents. `k` and `min_sim` were put to a grid on 1,000
  messages held inside the 10,003; the defaults (15, 0.10) won.
- **Classic models:** multinomial naive Bayes, and TF-IDF with softmax regression (numpy, no library), its
  epochs, learning rate and L2 chosen on the same 1,000 inside the 10,003.
- **The LLM:** two fresh Claude Fable 5.1 agents (effort medium), each given the 10,003 as a file and the 77
  names, told to route the 200 by judgement alone (`judge_prompt.md`), no code allowed.

Nothing was tuned on the 3,080 or on the 200.

## Results

| | 3,080 held out | The 200 sample |
|---|---|---|
| Ada, one bag-of-words role, k 15, min_sim 0.10 | 77.2% | 77.0% |
| Multinomial naive Bayes | 78.8% | 78.5% |
| TF-IDF + softmax regression, tuned inside the training set | 90.3% | 92.0% |
| Claude, run 1 (read ~8,100 of the 10,003) | — | 97.0% |
| Claude, run 2 (read all 10,003) | — | 97.5% |

The two Claude runs agreed with each other on 199 of the 200. The published state of the art on this set, fine-tuned
BERT-class models, is around 93% on the 3,080; Claude's 97% is on a 200 sample and reads a little high for the same
reason every sample does, but the gap to everything else is real.

## Cost and time

| | Build | Per message | Cost per message |
|---|---|---|---|
| Ada | 4 s, once | 5.9 ms | ~$0.00000003 on a $0.019/h machine |
| Naive Bayes | instant | 0.1 ms | ~0 |
| TF-IDF softmax | 12 s, once | 0.01 ms | ~0 |
| Claude, batched as here (200 at once, examples read once) | — | ~1,800 tokens, 0.6 s | ≈ $0.01–$0.03 |
| Claude, as an app would call it (the examples resent) | — | ~150,000 tokens | ≈ $0.75–$2.25 |

Claude's two runs: 359,197 and 384,065 tokens, 108 s each (the token price bracketed by the same two public rates
as the credit page, $5/M in and $25/M out to $15/M in and $75/M out). Ada's ingest is paid once.

## What it says

- **Text is a different problem from fields.** Ada's similarity on a bag of words counts shared words, every word
  the same: "card", "my" and "the" weigh as much as "stolen". The classic model's whole advantage is that it
  learned which words matter (TF-IDF, then a weight per word per intent). Ada has no way to learn a word's
  weight today: `learn` skips text roles.
- **The LLM is in its element.** Routing a short message to a named intent is reading comprehension, and the
  judges leaned on the training file for the near-neighbour pairs (card_arrival against card_delivery_estimate,
  the three top-up intents). They were also consistent with each other, which they were not on the loans.
- **Ada is still deterministic, free per call and gives receipts**, and 77% from one role and no learning is not
  nothing; but a developer with this case should use the classic model or the LLM today.

## What would move Ada

1. **Learned word weights.** A bag-of-words role whose words carry weights the store learned: the kernel can
   bundle with a weight per word already, so this is `learn` writing `value_weights` for text roles (how much each
   word tells about the outcome, the same information gain the fields get) and the encoder honouring them. That
   is TF-IDF's trick, inside Ada, with the receipts kept. The expected move is from 77% toward the classic model's
   90%; the honest way to find out is to build it, choose nothing on the 3,080, and run this page again.
2. **Word pairs.** The interactions the credit page added, for words ("top up" + "failed").
3. **The NSM layer.** The kernel's primes could stand in for a word's meaning, so "stolen" and "taken" meet.

## Reproduce

```sh
benchmarks/banking77/fetch.sh                      # train.csv, test.csv
uv run python benchmarks/banking77/run.py          # Ada, the two classic models, the tuning grids; ~5 minutes
uv run python benchmarks/banking77/score.py benchmarks/banking77/results/llm_run1.json   # a judge's answers
```

`results/` holds the 200 sample and its labels, both judges' answers, and Ada's 3,080 guesses.

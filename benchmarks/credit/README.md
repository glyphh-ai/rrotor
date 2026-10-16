# Benchmark: credit decisions — Ada against an LLM, on the same hold-out

**The question:** for a structured decision an app would otherwise hand to an LLM, how does an Ada model compare on
accuracy, cost and time, with a classic model beside both for honesty?

**The data:** German Credit (UCI Statlog): 1,000 loan applications, 20 typed fields (checking account, credit
history, purpose, amount, duration, savings, employment, housing, age, …) and the bank's real decision, good or
bad. The dataset's own cost matrix: approving a bad loan costs 5, refusing a good one costs 1.

**The split:** one shuffle (seed 7): 800 to learn from, 200 held out. The same 800 and the same 200 for everyone.

**The contestants**

| | How it learned | How it decided |
|---|---|---|
| Ada | `record` of the 800 cases with their outcome, into the spec in `run.py` (3 layers; the applicant's money roles weigh 1.0, their life roles 0.6; hand-set, untuned) | `query` each of the 200; `top`. "Cost-aware": refuse when `outcomes["bad"]` > 1/6 |
| LLM (Claude Fable 5.1, effort medium, two independent runs) | Read `train.jsonl` (the brief is `judge_prompt.md`; no code allowed) | Judged each of the 200 by hand; told the cost matrix |
| Logistic regression | One-hot + standardised, numpy, 3,000 steps | P(bad) > 0.5; "cost-aware": P(bad) > 1/6 |

Run on 2026-10-16.

## Results, 200 held out

| | Accuracy | Bad loans approved (×5) | Good refused (×1) | Bank's cost | Same answer every time |
|---|---|---|---|---|---|
| Always approve | 67.5% | 65 | 0 | 325 | — |
| **Ada** (min_sim 0.25, k 25) | **77.0%** | 41 | 5 | 210 | yes |
| Ada, cost-aware | 59.5% | 10 | 71 | 121 | yes |
| Logistic regression | 75.0% | 37 | 13 | 198 | yes |
| Logistic regression, cost-aware | 68.5% | 10 | 53 | **103** | yes |
| **LLM, run 1** (read 430 of the 800) | **77.0%** | 16 | 30 | 110 | the two runs agree on |
| **LLM, run 2** (read ~140 of the 800) | **79.0%** | 18 | 24 | 114 | 94% of cases |

Other Ada settings on the same split: min_sim 0.35 / k 15 → 76.0%, cost 212; min_sim 0.45 / k 9 → 75.0%, cost 214.
Ada answered (`sufficient`) on 100% of cases at every setting.

## Cost and time

Ada on a hosted runtime machine (Fly shared-cpu, $0.019 an hour). The LLM per token; its price is bracketed by two
public rates because the model's own was not to hand.

| Ada | Time | Cost |
|---|---|---|
| Build the model: record 800 | 0.4 s, once | ~$0.000002 |
| One run: 200 decisions | 195 s (0.97 s each) | ~$0.001 |
| Every Ada run made for this page (4 settings × 200) | ~13 min | ~$0.004 |

| LLM | Tokens | Time | at $5/M in, $25/M out | at $15/M in, $75/M out |
|---|---|---|---|---|
| Run 1: read 430 examples + 200 cases, decide | 179,892 | 166 s | ≈ $0.95 | ≈ $2.85 |
| Run 2: read ~140 examples + 200 cases, decide | 116,647 | 119 s | ≈ $0.62 | ≈ $1.85 |
| Both runs, 400 decisions | 296,539 | 285 s | ≈ $1.57 | ≈ $4.70 |
| Per decision, batched like this | ~740 | 0.7 s | ≈ $0.004 | ≈ $0.012 |
| Per decision as an app calls it (the examples resent on every call) | ~100,000 | 1–3 s | ≈ $0.50 | ≈ $1.50 |

The LLM keeps nothing between runs: every run re-reads the examples, and neither run read all 800 (430 and ~140).
Ada's ingest is paid once; every decision after is a query.

## What it says

- **Accuracy: a tie.** Ada 77%, the LLM 77–79%, logistic regression 75%, all from the same 800 cases.
- **The bank's cost: the LLM and the classic model ahead of Ada today.** Ada's `outcomes` is a similarity-weighted
  vote, not a calibrated probability, so a cost threshold over-refuses (71 good loans). Calibration and a cost matrix
  in the spec are the fix (below).
- **Determinism:** the two LLM runs disagreed on 12 of 200 decisions. Ada and the classic model never do.
- **Cost per decision:** Ada is three to five orders of magnitude cheaper, and keeps the data in the runtime.
- **The uncomfortable line:** on purely structured data a classic model with a threshold is hard to beat on numbers.
  Ada's reasons to exist are the receipts (which precedents), the clearance layers, and learning one confirmed case at
  a time with no retraining.

## What would move Ada

1. **Learned role weights**: how well each role separates the outcomes, from the store, written back as
   `similarity_weight` (today every role weighs what the spec says, by hand).
2. **Calibration and a cost matrix in the spec**: leave-one-out over the store maps the vote to P(bad);
   `output.costs` makes the threshold fall out of the costs.
3. **Abstain by expected cost**: `ask` in the band where either answer is costly; the person's answer is recorded.
4. **A vectorised query**: 0.97 s per query at 800 precedents is the Python loop; one matmul makes it milliseconds.

## Reproduce

```sh
benchmarks/credit/fetch.sh                         # german.data (CC BY 4.0, UCI)
uv run python benchmarks/credit/run.py             # the split, Ada and the baseline; ~4 minutes
uv run python benchmarks/credit/score.py benchmarks/credit/results/llm_run1.json   # any answers file
```

`run.py` writes `train.jsonl`, `holdout.jsonl` and `holdout_labels.json`; the judges' answers are in `results/`.
The kernel is the one the repo's tests carry (`tests/kernel/`), the same the Ada part ships.

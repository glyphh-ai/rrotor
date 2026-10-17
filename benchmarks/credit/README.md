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

## After calibration, the same 200 once more

`calibrate.py`, run after the table above and reported whatever it said. Nothing in it looks at the 200: the
weights and the temperature come from the 800 alone, the rule from the cost matrix alone. Since kernel 3.2.0 every
step is the kernel's own, not the script's: the script only names the costs and calls it.

1. **`Spec.learned(cases)`** on the 800: each role's weight is its information gain about the decision, scaled so
   the best weighs 1 — `checking` 1.0, `duration_months` 0.51, `history` 0.39, `amount` 0.34, `savings` 0.30,
   `purpose` 0.21, … `job` 0.01, `dependants` 0.0 — and each value of a role weighs how far it moves the decision
   from the base rate (`checking`: `negative` 1.0, `no account` 0.98, `under 200` 0.40, `200 or more` 0.21). The
   hand-set segment weights are dropped.
2. **`Model.calibrate(sample=800)`**: the kernel holds out each of the 800 in turn against the rest and keeps the
   temperature with the least log-loss (0.2).
3. **`output.costs = {good: {bad: 5}, bad: {good: 1}}`** in the spec; `decide` returns the outcome with the least
   expected cost under the calibrated probabilities. **`output.ask = 0.5`** prices asking a person at half a refused
   good loan (chosen before any run); `decide` answers `ask: true` where every outcome's expected cost is above it.
4. **The runtime's way, as a check**: the same spec unlearned, the 800 recorded in order with `refresh_every=100`,
   so the model learns its weights from its own store and calibrates (on its newest 200) eight times as it fills.

`k` and `min_sim` were put to five-fold cross-validation inside the 800 too, but `decide` reads the 25 nearest
precedents whatever `k` is, so all five grid points cost the same (422 over the 800) and the first, k 15 and
min_sim 0.35, stood; only the plain `query` line below depends on it.

| | Accuracy | Bad approved (×5) | Good refused | Bank's cost |
|---|---|---|---|---|
| Ada, learned spec, plain `query` | 78.5% | 32 | 11 | 171 |
| **Ada, learned spec, calibrated, `decide`** | 66.0% | 10 | 58 | **108** |
| the same, asking where every answer costs over 0.5 | 82.2% of 107 answered | 6 | 13 | 43, asked 93 |
| Ada, learned from its own store as it filled, `decide` | 67.5% | 9 | 56 | 101 |
| earlier, by hand in the script (isotonic curve, 1/6 rule) | 67.0% | 11 | 55 | 110 |
| for reference: LLM runs 1 and 2 | 77.0% / 79.0% | 16 / 18 | 30 / 24 | 110 / 114 |
| for reference: logistic regression, cost-aware | 68.5% | 10 | 53 | 103 |

Cross-validated accuracy inside the 800 was 71.8%, so this hold-out runs a little easy for every contestant; the
ranking is what to read, not the third digit.

So: with the learned spec Ada's plain answer is the best accuracy on the table (78.5%, cost 171, from 210 with
the hand-set weights), and on the bank's cost `decide` lands at 108 — a hair ahead of both LLM runs, 5 behind the
thresholded classic model — with every choice made before the hold-out was opened. The whole calibration now costs
4 s (the vectorised query: 0.002 s per query at 800 precedents, was 0.97 s).

The priced ask sends 93 of the 200 to a person and decides the other 107 at cost 43 (82.2% right): asking at 0.5 is
dear, and what it buys is a clean half. A cheaper ask asks less; the band is the user's price, not a tuning knob.

The model that learned from its own store as it filled decided at cost 101, below the batch model's 108 and the
classic model's 103, from the same weights (learned at 800 both ways) and a different temperature: calibrating on
its newest 200 chose tau 1.0, on all 800 tau 0.2, and the decisions moved by seven. Read that as the fit of the
temperature being loose on this data (the log-loss is nearly flat between those), not as one path being better;
both were set before the hold-out was opened, and both are reported.

## What would move Ada

1. **Interactions between roles** (a bad `checking` with a long `duration_months`): the per-value weights are
   one-dimensional, which is where the logistic model's last few come from.
2. **A steadier temperature**: the fit between tau 0.2 and 1.0 is loose here; a finer grid, or fitting on the
   bank's cost rather than log-loss, would settle it (and would have to be chosen before the next hold-out).

Done since the first run: learned role and value weights, costs and `decide` in the kernel; the priced `ask`;
a model that learns from its own store and refreshes as cases arrive (`refresh_every`); the vectorised query.

## Reproduce

```sh
benchmarks/credit/fetch.sh                         # german.data (CC BY 4.0, UCI)
uv run python benchmarks/credit/run.py             # the split, Ada and the baseline; ~4 minutes
uv run python benchmarks/credit/calibrate.py       # learned spec, calibration, the cost rule; ~2 minutes
uv run python benchmarks/credit/score.py benchmarks/credit/results/llm_run1.json   # any answers file
```

`run.py` writes `train.jsonl`, `holdout.jsonl` and `holdout_labels.json`; the judges' answers are in `results/`.
The kernel is the one the repo's tests carry (`tests/kernel/`), the same the Ada part ships.

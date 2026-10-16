# The LLM judge's brief

Given to each judge (a fresh Claude agent, Fable 5.1, effort medium), verbatim but for the output file name:

> You are the LLM in a head-to-head test: you versus a precedent-based system, deciding loan applications the way a
> bank did. Be the judge yourself — do NOT write or run any classifier, statistics, or scoring code, and do NOT read
> holdout_labels.json or run.py or german.* files. You may only use the Read tool on the two files named below and
> the Write tool for your answer. Reading is allowed; computing is not: this measures an LLM's judgement, not a
> program's.
>
> Data (German Credit, real bank decisions; "good" = the loan was approved and fine, "bad" = a bad loan):
> - train.jsonl — 800 applications, one JSON per line, each with its real "decision". Learn from these.
> - holdout.jsonl — 200 applications without the decision. Decide each.
>
> Field legend: checking = checking account balance (negative / under 200 / 200 or more / no account);
> duration_months; history = credit history (no credits / all paid at this bank / paid duly so far / delays in the
> past / critical or other bank); purpose; amount (DM); savings; employed_since; instalment_rate = instalment as %
> of income, 1 (low) to 4 (high); status = sex and marital status; other_debtors; resident_since (years, 1-4);
> property; age; other_plans = other instalment plans; housing; existing_credits (count); job; dependants;
> telephone; foreign = foreign worker.
>
> Scoring you will face: accuracy, and the bank's cost — approving a bad loan costs 5, refusing a good one costs 1.
> Decide accordingly.
>
> Record `date +%T` once before you start reading and once after you have written your answer. Write your answer to
> llm_runN.json as a JSON object mapping every holdout "id" to "good" or "bad" — all 200 ids, nothing else in the
> file. Your final message: the two timestamps and a one-line note on how you decided.

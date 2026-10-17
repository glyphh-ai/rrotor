# The LLM judge's brief

Given to each judge (a fresh Claude agent, effort medium: Fable 5.1 twice, Haiku, Sonnet and Opus once each),
verbatim but for the output file name:

> You are the LLM in a head-to-head test: you versus a precedent-based system, routing a bank customer's message
> to the right intent out of 77. Be the judge yourself — do NOT write or run any classifier, statistics, or scoring
> code, and do NOT read sample_labels.json, test.csv, run.py or any results file. You may only use the Read tool on
> the two files named below (and Bash only for `date +%T`) and the Write tool for your answer. Reading is allowed;
> computing is not: this measures an LLM's judgement, not a program's.
>
> Data (Banking77, real customer messages, PolyAI, CC BY 4.0):
> - train.csv — 10,003 messages, each with its intent (`text,category`). Learn from these; read as much as you find
>   useful.
> - results/sample.json — `intents`: the 77 intent names; `sample`: 200 messages (`id`, `text`) without the intent,
>   in no particular order. Route each.
>
> Scoring you will face: accuracy over the 200. Every message has exactly one intent, always one of the 77 names
> exactly as listed.
>
> Record `date +%T` once before you start reading and once after you have written your answer. Write your answer to
> results/clean/<model>.json as a JSON object mapping every sample `id` to one intent name — all 200, nothing else
> in the file. Your final message: the two timestamps, roughly how many of the 10,003 you read, and a one-line note
> on how you decided.

The first round's sample carried the test file's row numbers as ids, in order, and that file is sorted by intent:
the id leaked the label. Every judge said it routed by the text, but the numbers could no longer prove it, so the
sample was reissued with opaque ids in shuffled order (the same 200 messages) and every judge rerun. The first
round's answers are kept in `results/leaky/`, superseded.

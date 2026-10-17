# The LLM judge's brief

Given to each judge (a fresh Claude agent, Fable 5.1, effort medium), verbatim but for the output file name:

> You are the LLM in a head-to-head test: you versus a precedent-based system, routing a bank customer's message
> to the right intent out of 77. Be the judge yourself — do NOT write or run any classifier, statistics, or scoring
> code, and do NOT read sample_labels.json, test.csv, run.py or any results file. You may only use the Read tool on
> the two files named below and the Write tool for your answer. Reading is allowed; computing is not: this measures
> an LLM's judgement, not a program's.
>
> Data (Banking77, real customer messages, PolyAI, CC BY 4.0):
> - train.csv — 10,003 messages, each with its intent (`text,category`). Learn from these; read as much as you find
>   useful.
> - results/sample.json — `intents`: the 77 intent names; `sample`: 200 messages (`i`, `text`) without the intent.
>   Route each.
>
> Scoring you will face: accuracy over the 200. Every message has exactly one intent, always one of the 77.
>
> Record `date +%T` once before you start reading and once after you have written your answer. Write your answer to
> results/llm_runN.json as a JSON object mapping every sample `i` (as a string) to one intent name — all 200,
> nothing else in the file. Your final message: the two timestamps, roughly how many of the 10,003 you read, and a
> one-line note on how you decided.

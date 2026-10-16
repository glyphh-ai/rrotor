# Benchmark: GitHub issue triage — free text, Ada's weakest fit

The first thing tried, kept for the record: closed issues from two public repositories, the outcome a label humans
set, Ada learning from 80% and queried on the 20% held out (`holdout.py`; the issues were fetched with `gh api` on
2026-10-16, ~1,500 each, not kept here). Defaults, no tuning: title and body as `bag_of_words`, four cheap shape roles.

| Task | Outcomes | Majority baseline | Ada when it answers | How often it answers | Forced to answer everything |
|---|---|---|---|---|---|
| vscode — triage kind (bug / feature-request / info-needed / duplicate / spam) | 5 | 42% | 61% | 92% | 59% |
| kubernetes — area (`sig/*`) | 11 | 36% | 66% | 69% | 54% |
| kubernetes — kind (bug / feature / flake / failing-test / cleanup / docs) | 6 | 37% | 86% | 78% | 76% |

Free text is where a TF-IDF classifier or an LLM is at home and Ada's edges (typed fields, receipts, clearance, no
model in the path) count for little. The credit benchmark beside this one is the fit Ada is for.

```sh
gh api "repos/kubernetes/kubernetes/issues?state=closed&per_page=100&page=1" --jq '...' > k8s.jsonl   # see holdout.py
uv run python benchmarks/issues/holdout.py k8s.jsonl sig
```

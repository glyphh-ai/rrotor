# A session's memory: what a turn needs from the turns before it, and what carrying all of them costs

The idea under test, Chris's: a Claude Code session carries its whole transcript into every call; a precedent store
could carry only the turns a new turn needs, and the bill would fall by the difference. Two questions, both
answered on real data: every Claude Code session on this machine, July to October 2026, 180 sessions, read from the
transcripts the harness keeps (`~/.claude/projects`). No transcript text is in this folder; the scripts read the
transcripts in place and print aggregates.

## The table

**What the bill is**, by the API's own token counts, at Fable 5.1 list prices (input $10/M, cache write $12.50/M,
cache read $1/M, output $50/M), over the 85 sessions with 20 or more calls, 135,142 calls:

| | |
|---|---|
| The bill | $90,980 ($673 per 1,000 calls) |
| of which re-reading the context on every call | $64,037 (70%) |
| context per call, average | 484,985 tokens |
| of which the fixed preamble (system prompt, tools, memory, skills) | ~51,000 |
| of which history carried from earlier in the session | ~430,000 (89%) |
| the same bill with history replaced by five precedents of ~400 tokens | ~$27,400 (**70% less**) |

**What a turn needs from history.** After the fact the label is known: for each file a turn edits or reads, the last
earlier turn that touched it (1,928 turns in 44 sessions of 10+ turns; median 2 earlier turns needed, 3 files
touched). Before the fact a retriever has only the new request, the folder and the branch. Recall of those needed
turns, carrying k of them:

| Retriever | recall@5 | recall@10 |
|---|---|---|
| The last k turns (what compaction keeps) | **36.4%** | **47.3%** |
| TF-IDF over the request text | 11.2% | 17.3% |
| Ada: a precedent per turn, queried by the request | 14.2% | 21.7% |
| Ada plus the last 2 turns | 30.6% | 35.7% |
| Oracle | 91.8% | 98.5% |

**How far back a need reaches:** within the last turn 14%, 5 turns 36%, 10 turns 47%, 20 turns 59%, 50 turns 74%;
median 12 turns, the ninetieth percentile 141.

## What it says

- **The money is real and it is history.** Nine tenths of every call's context is the session so far, and it is
  re-read on every call. Replace it with a few hundred tokens of what the turn needs and the bill falls by about
  seventy percent, with the fixed preamble as the floor.
- **The request does not say what is needed; what was done last does.** Similarity over the request text, Ada's or
  TF-IDF's, recovers a seventh of the needed turns. Recency recovers a third to a half. The needed turns are the last
  touch of each file the turn works on, and nothing in "fix the flaky test" names which turn that was.
- **So the mechanism is a ledger, not a search.** A precedent per file, the last thing done to it and why, fetched
  when the model touches the file again: by construction that recovers every file-keyed need, at a median of three
  files a turn and a hundred tokens a precedent. Recency on top for the last few turns. Ada's similarity is not
  the lever here; its record, receipts and learning are, keyed by path.
- **What this label misses.** A turn also needs decisions, constraints and errors from earlier turns that touched no
  file in common ("don't start servers", "the user chose option C"). Those are not in this label and they are
  where a semantic recall could still earn its place; measuring that needs a person to say what was needed, not a
  script.

This page reverses the design I had argued for before measuring it: the memory tree keyed by request text. The
data says keep a file ledger and the recent turns, and put the semantic store to work on decisions, which is a
different and smaller store.

## Reproduce

```sh
uv run python benchmarks/session/mine.py      # every turn of every session on this machine -> turns.jsonl (not committed)
uv run python benchmarks/session/spend.py     # the bill by token kind; finding before the first edit
uv run python benchmarks/session/spend2.py    # each find's result costed by size and life in context
uv run python benchmarks/session/recall.py 5  # recall of the needed turns, k=5 (10 for k=10)
```

The transcripts are the harness's own (`~/.claude/projects/*/*.jsonl`); the numbers above are this machine's and
will differ on another.

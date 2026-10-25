# Bots at the door: is this session a person or a robot, from how it behaves?

**The question:** can an Ada model sitting at a site's edge tell a robot's session from a person's, from the
requests alone, well enough to block the robot without ever blocking the person? The gate's costs: blocking a
person costs five robots let in; a challenge (a proof of browser) costs half a robot.

**What this test leaves out, first.** The data is a university library's search site in 2018: crawlers,
scrapers and link checkers, labelled by the dataset's authors from user agents, robots.txt and known addresses.
It is not an LLM agent driving a browser, and nothing here says how one of those looks. A robot that spoofs a
browser and paces itself like a person is not in this data either. What it does measure: from behaviour alone,
with no user agent and no address, how far a precedent store gets at recovering those labels, how many
precedents it needs, and what is lost when the gate sees only what an origin behind a CDN sees. The live gate at
glyphh.ai (`glyphh-ai/web`, `docs/ada-gate.md`) and its first probe are at the end of this page.

## The table

Deciding 2,000 real sessions, person or robot, from 35 measures of each (and from the 16 an origin can compute
itself); every system learned the same 10,000; nothing tuned on the 2,000. Cost in the gate's units.

| | Right | Challenged | People blocked | Robots let in | Robots caught | Cost |
|---|---|---|---|---|---|---|
| Let everyone in | 80.9% | | 0 | 382 | 0% | 382 |
| Rule: one request, no gap, is a robot | 83.0% | | 200 | 139 | 64% | 1,139 |
| **Ada, every feature, learned, calibrated, `decide`** | **97.1%** | 4.2% | **12** | 43 | 74% | **145.5** |
| the same, on the 1,322 sessions of 5+ requests | 99.6% | 0.5% | 3 | 2 | 94% | 20.5 |
| Ada, edge features (what the origin sees) | 92.9% | 8.8% | 18 | 111 | 45% | 289 |
| the same, on the 1,322 sessions of 5+ requests | 99.1% | 1.4% | 6 | 6 | 77% | 45.5 |

Ada's cost is well under letting everyone in and a tenth of the rule's; on the sessions a gate actually meets
while they run (five requests or more, where the gate first asks) it is nearly exact, blocking three people in
1,322 with every feature and six with the edge's. The single-request sessions are where the robots hide (443 of
the 2,000 have one request, most of them robots) and where behaviour has nothing to say: one request, no gap.
Those are a rate limiter's and a referrer check's, not a precedent store's, and the table does not pretend
otherwise.

### The 200 sample, with an LLM judge

| | Right | Challenged | People blocked | Robots let in | Robots caught | Cost |
|---|---|---|---|---|---|---|
| Ada, every feature | 98.9% | 5.0% | 0 | 2 | 81% | 7 |
| Ada, edge features | 95.7% | 8.0% | 0 | 8 | 48% | 16 |
| Claude Fable 5.1 (read 490 of its 1,000 sessions) | 96.5% | | 6 | 1 | 97% | 31 |
| Ada decides, Fable takes the 10 it challenges | 97.5% | | 3 | 2 | 94% | 17 |
| any of them, on the 138 sessions of 5+ requests | 100% | | 0 | 0 | 100% | 0 to 3 |

The judge caught nearly every robot and blocked six people doing it, the dear mistake; Ada blocked none. Fable
took 5½ minutes and 367,000 tokens for the 200; Ada 64 ms a decision at 10,000 precedents (36 ms with the edge
features), deterministic, for nothing. An LLM is not a thing one puts in the path of a web request anyway; the
row is here because every benchmark on these pages asks the same question of the same contestants.

### The growing store: the gate as it fills (edge features)

The gate the way it is deployed: a fresh model, sessions arriving one at a time, learned and calibrated at each
mark, scored on the same 2,000 before it grows further.

| Precedents | Right | Challenged | People blocked | Robots caught | Cost |
|---|---|---|---|---|---|
| 30 | 80.9% | 0% | 0 | 0% | 382 |
| 100 | 92.7% | 22% | 0 | 0.5% | 336 |
| 300 | 89.3% | 12% | 9 | 11% | 343.5 |
| 1,000 | 91.8% | 8% | 12 | 38% | 281 |
| 3,000 | 91.7% | 6% | 16 | 42% | 280 |
| 10,000 | 92.9% | 9% | 18 | 45% | 289 |

At a hundred precedents the gate is honest rather than useful: it challenges a fifth and blocks no one. At a
thousand (the Free plan's cloud cap) it has most of what ten thousand give it; the edge features saturate early,
which says the ceiling is the features, not the store.

## The test

- **Data:** [Web robot detection - Server logs](https://zenodo.org/records/3477932) (Iliou et al., Aristotle
  University of Thessaloniki, CC BY 4.0): one month of a library search engine's logs, 4.1 million requests,
  grouped by the authors into 67,352 sessions, 20% robots, each session 30 "simple" measures (requests,
  duration, gaps, methods, statuses, images, pages, width and depth of the walk, referrer and file-type measures,
  trap hits) and 5 "semantic" ones (what the pages were about). `fetch.sh` gets the two feature files.
- **Split:** one shuffle (seed 7): 10,000 to learn from, 2,000 held out; the first 200 of the 2,000, under
  opaque shuffled ids, are the judge's. Counts and seconds go in as log10(1 + x) in quarter bins, shares in
  tenths; a measure the file has as NaN is left out of the case (it weighs nothing).
- **Edge features:** the 16 an origin computes from a request's time, method and path alone: no status, no bytes,
  no referrer measures, no trap hits, no page semantics. `edge_spec.json` is their spec, the one the live gate
  uses.
- **Ada:** one spec, roles in layers by what they say of a session (`run.py`); `Spec.learned` over the 10,000
  gives every role and value its weight and finds five pairs that tell more together; the 10,000 recorded;
  `calibrate` on 1,000 of its own; `decide` under the costs in the spec, `ask` priced at 0.5. Defaults otherwise
  (k 15, min_sim 0.10). The plain vote is beside it in `results/table.json`: 95.8% right, 42 people blocked, cost
  253 with every feature; the costs and the challenge are what take it to 12 and 145.5.
- **The judge:** a fresh Claude agent (Fable 5.1, effort medium) given 1,000 of the 10,000 (ten thousand rows of
  35 numbers are more than a context holds), the brief in `judge_prompt.md`, no code allowed. It read about 490
  and judged the 200 by hand; its note: a session with no referrer was its master robot signal, and the 5:1 cost
  pushed every borderline call to "person".

Learned role weights with every feature, highest first: the referrer measures (`unassigned` 1.0, `sf_referrer`
0.92, `sf_filetype` 0.84), the spread of the gaps (0.76), the share of images (0.57). With the edge features: the
spread of the gaps (1.0), images (0.76), duration (0.62), the busiest second (0.58), requests (0.57). Method
shares, the night share and repeats weigh nearly nothing in either.

## The live gate, and what the first probe said

The same edge spec is a model on Ada Cloud, `bots-edge`, seeded with 650 of the 10,000 (the Free plan holds
1,000 across the org), learned and calibrated there (tau 0.05); its backtest on its own newest 200: 82% right,
11.5% challenged, no person blocked, 36 robots let in. Asked two of this page's held-out sessions over the API, it
called the 151-request robot a robot (0.91) and the 30-request person a person (1.0), with receipts.

glyphh.ai's proxy asks it at a session's 5th, 20th and 60th request, in shadow mode (`glyphh-ai/web`:
`middleware.js`, `app/lib/gate.js`, `docs/ada-gate.md`). Then `scripts/probe.mjs` ran three sessions against the
live site and asked the model the way the gate does, 2026-10-25:

| Session | Requests | Seconds | Mean gap | Busiest second | Verdict | P(robot) |
|---|---|---|---|---|---|---|
| Scraper: plain fetches, no pauses, no cookies | 14 | 2 | 0.14 s | 6 | allow | 0.05 |
| Agent: headless browser, 1 to 2 s a page | 61 | 17 | 0.29 s | 7 | allow | 0.00 |
| Person (scripted): scrolls, 6 to 15 s a page | 59 | 119 | 2.05 s | 7 | allow | 0.00 |

**The seed does not transfer, and the probe shows why.** In the library's logs a robot's busiest second was
empty and a person's held fourteen requests, a page's burst of assets; at glyphh.ai's origin Cloudflare serves
the assets, so a person's burst is gone, while a scraper fetching fourteen pages in two seconds looks, to a
model that learned the library, like a person loading one page. The live model's precedents have to be
glyphh.ai's own sessions; shadow mode logs each session's facts beside Ada's verdict so they can be labelled
(known crawlers, our own probes, people who signed in) and recorded. Until then the gate is a logger, which is
what shadow mode is for. The next measurement is this probe again once the model holds a few hundred of the
site's own sessions.

```sh
sh benchmarks/bots/fetch.sh
uv run python benchmarks/bots/run.py      # ~10 min: the table, the judge's files, edge_spec.json, results/
uv run python benchmarks/bots/score.py    # the 200 sample: Ada beside results/llm_run1.json
```

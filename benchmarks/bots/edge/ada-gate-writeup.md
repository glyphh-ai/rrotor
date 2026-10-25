# The Ada gate at glyphh.ai's edge

**25 October 2026.** Live in shadow mode since 21:20 UTC.

## What it is

A door in front of glyphh.ai that asks Ada whether the session on the other side behaves like a person or a robot,
and remembers every answer. Not a user-agent list, not a CAPTCHA on every visitor, not a model that has to be
retrained: a precedent store that decides from the sessions it has seen, says how sure it is, and shows the
precedents it stood on.

## How it works

1. **Every request to glyphh.ai is kept against its session** by the site's proxy. A session is the `glyphh_sid`
   cookie; a visitor that keeps no cookies (a scraper) is its address and user agent together, so it is one session
   whether it wants to be or not.
2. **At the 5th, 20th and 60th request** the session's behaviour becomes sixteen facts: how many requests, over how
   long, the mean and spread of the gaps between them, the busiest second, the share at night, repeats, the share by
   method, the share of images and of pages, distinct paths, how deep it went and how evenly. Nothing about who the
   visitor says it is.
3. **Ada Cloud decides.** The facts go to `ada.glyphh.ai`, to a model called `bots-edge`, which answers from its
   precedents with the outcome of least expected cost under the gate's prices: blocking a person costs five robots
   let in, a challenge costs half a robot. Three verdicts: allow, challenge, block. Each comes with probabilities
   and receipts.
4. **No visitor waits.** The question is asked after the response has gone out; the verdict applies from the
   session's next request. Ada answers in about 450 ms.
5. **Today the gate logs; tomorrow it enforces.** In shadow mode every decision is one JSON line in the app's logs,
   with the session's facts beside the verdict, and the response carries an `x-ada-gate: shadow:<verdict>` header.
   One secret, `ADA_GATE=enforce`, turns a block into a 403.

## What the benchmark proved

Before any of this touched production the method was measured on real sessions: a month of a university library's
search engine, 67,352 sessions grouped and labelled by the dataset's authors, one fifth robots. Ada learned from
10,000 and was scored on 2,000 it had never seen.

| | Right | People blocked | Robots let in | Cost |
|---|---|---|---|---|
| Let everyone in | 80.9% | 0 | 382 | 382 |
| A one-request rule | 83.0% | 200 | 139 | 1,139 |
| **Ada, every feature** | **97.1%** | **12** | 43 | **145.5** |
| the same, on the 1,322 sessions of 5+ requests | 99.6% | 3 | 2 | 20.5 |
| Ada, edge features only | 92.9% | 18 | 111 | 289 |
| the same, on the 1,322 sessions of 5+ requests | 99.1% | 6 | 6 | 45.5 |

On the sessions a gate actually meets while they run, five requests or more, Ada is nearly exact and blocks three
people in 1,322. The robots it misses are the single-request sessions, where behaviour has nothing to say and a
rate limiter belongs. A Claude Fable judge given the same task on a 200 sample was 96.5% right but blocked six
people doing it, the dear mistake, for a cost of 31 against Ada's 7, in five and a half minutes against 64 ms a
decision. The store saturates early: at 1,000 precedents the edge model has most of what 10,000 give it.

## What the edge showed

The same edge spec is the live `bots-edge` model, seeded with 650 of the library's sessions. Asked over the API
about two held-out sessions, it called the 151-request robot a robot and the 30-request person a person. Then a
probe ran three sessions against the live site: a scraper (plain fetches, no pauses, no cookie), an agent (a
headless browser, a second or two a page) and a scripted person (scrolls, six to fifteen seconds a page).

Asked from outside with its own 14 fast requests, the scraper was allowed with a robot probability of 0.05. Seen
by the gate itself at its fifth request, five pages in under a second with no cookie, it was **challenged** at
0.70. Both browsers were allowed. The two readings of the same scraper say what the seed is worth: in the library's
logs a robot's busiest second was empty and a person's held a page's burst of fourteen assets, but Cloudflare
serves glyphh.ai's assets, so at our origin a person's burst is gone and a scraper's burst of pages is the only
burst there is. The library taught the model what a robot is somewhere else. The gate's job now is to learn what
one is here.

## What is next

1. **Label our own sessions.** Every shadow decision in the logs carries the session's facts. Known crawlers by
   user agent, our own probes, and people who went on to sign in give the labels; recording a few hundred of them
   into `bots-edge` makes the model glyphh.ai's. The probe runs again after.
2. **A challenge page.** The `challenge` verdict is a hook today; a proof of browser behind it is what makes the
   middle verdict cheap for people and dear for robots.
3. **Enforce.** One secret. Before it, the plan: the Free tier's 5,000 Ada requests a month covers the gate's
   120 decisions a day in shadow, not real traffic under enforce.
4. **A retention policy in the kernel.** Stores fill with near-duplicates (the live model's receipts showed seven
   precedents at similarity 1.0 for one case); a configurable `retain` block, with a cap, a policy (refuse, oldest,
   redundant, least used) and mistakes always kept, is the agreed design.

## What it leaves out

The benchmark's robots are 2018 crawlers and scrapers labelled by user agent and robots.txt, not LLM agents, and a
robot that paces itself like a person is in neither the data nor the probe. The gate does not claim to catch what
it has never seen; it claims to remember what it has, decide from it with its reasons on the table, and get better
with every session it is shown.

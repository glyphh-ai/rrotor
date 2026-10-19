# tickets: an Ada model on rrotor

A support-ticket triage model. `spec.json` says what a ticket is (its title,
component, severity, labels, when it was opened, which team owns it); each
ticket recorded with how it was handled becomes a precedent; a new ticket is
answered from the precedents closest to it.

| Function | Does |
|---|---|
| `record` `{data, outcome?}` | Keeps a ticket, with its outcome when known |
| `query` `{data}` | The leading outcome, its confidence, the precedents it stands on |
| `facts` `{data, top?}` | The closest precedents, best first, cut to what the caller may see |
| `probabilities` `{data}` | P(outcome \| ticket) over the nearest precedents |
| `decide` `{data}` | The outcome when confident; else asks for it (`needs_input`) and records it |
| `learn` `{sample?}` | Weighs the roles, the words and the pairs from the store's own precedents and calibrates; needs ten graded precedents of two outcomes |
| `settings` `{changes?}` | Reads the model's levers, or changes them live |
| `start` | How many precedents the model holds |

The manifest names the part `ada` and asks for a data folder: the store,
`precedents.sqlite`, lives there and outlives every version. `clearance`
(0 to 1, from who calls) cuts what the answers show: the `meta` layer weighs
0.8, so a caller below that sees no team or time.

Its tests, from the repo (the kernel is `tests/kernel/`):

```sh
uv run pytest examples/ada_tickets -o addopts="" -p no:cacheprovider --import-mode=importlib
```

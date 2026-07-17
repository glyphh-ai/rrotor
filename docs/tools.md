# Tools: the ToolSpec and the standard library

RotorSpec defines the **loop**; **ToolSpec** defines the **capabilities** the loop can
call. This is the leverage point of the whole system: the runtime is domain-agnostic,
and it becomes "code / music / slides / anything" purely through its tools. A tool is
not just a function — it is a typed, versioned, permission-scoped,
determinism-classified unit, which is what lets a *better* implementation drop in
behind the same name and every rotor benefit for free.

Source: `src/tools/` (`spec.ts` + the packs). SDK surface: `defineTool` / `definePack`.

---

## The ToolSpec

```ts
interface ToolSpec {
  name: string;            // "file.read", "git.commit" — the swappable slot
  version: number;         // pin (reproducible) vs float (latest-best)
  description: string;
  effect: "pure" | "reading" | "mutating" | "external";   // determinism class
  grants: Capability[];    // capabilities required (the permission gate)
  input:  JSONSchema;      // the model's arg contract
  output?: JSONSchema;     // the interchange contract for swappable impls
  handler: (args) => result;
}
```

Two fields carry the design:

### `effect` — the determinism class

The runtime records every `tool` step's output into the append-only event history
and, on replay, returns that recorded output **without re-dispatching** (the
executor's replay path + a step's `idempotency: auto`). So a run that writes a file or
runs a command **replays without doing it again.**

| effect | meaning | replay rule |
| --- | --- | --- |
| `pure` | deterministic function of inputs | recompute-safe |
| `reading` | reads external state (fs, net, git) | output recorded; replay returns it |
| `mutating` | reversible side effect (write, git add) | **must** run under `idempotency: auto` |
| `external` | irreversible / outward (bash, deploy, send) | idempotency + usually an approval gate |

This is why "generate a PowerPoint" is replayable and auditable and a bare `bash`
agent isn't: the ToolSpec forces every author to classify the effect, and the runtime
does the checkpointing. **The tool library is deterministic by construction.**

### `grants` — the permission gate (by construction)

Each tool declares the capability it needs. A **permission mode** is a set of granted
capabilities; the registry installs a tool for a run **iff all its grants are held**.
So "read-only chat" simply never exposes a `mutating` tool — it isn't denied at call
time, it *doesn't exist* for that run. Capabilities: `fs.read` · `fs.write` ·
`shell.exec` · `vcs.read` · `vcs.write` · `doc.write` · `cowork.write` · `memory.read`
· `net.read`.

Built-in modes (`MODES` in `spec.ts`):

| Mode | Capabilities | Gets |
| --- | --- | --- |
| **chat** | read + recall + web | `file.read`, `fs.grep`, `git.status`, `recall`, `web.fetch` — nothing that mutates |
| **cowork** | + doc/artifact authoring | + `doc.write`, `todo.write`, `artifact.write` |
| **code** | + shell + repo writes | the full workbench: `file.write`, `file.edit`, `shell.bash`, `git.commit` |

---

## The standard library (batteries included)

You do **not** hand-write hundreds of tools. A small first-class core covers the
common, safe, structured path; `shell.bash` is the universal escape hatch for the OS
long tail (`find`, `rg`, `sed`, `curl`…); MCP covers external integrations; custom
tools cover domain packs.

| Pack | Tools | Notes |
| --- | --- | --- |
| **fs** | `file.read` `file.write` `file.edit` `file.list` `fs.glob` `fs.grep` | sandboxed under a workspace root; glob/grep are **bounded + sorted** |
| **exec** | `shell.bash` | the escape hatch; `external`, heaviest grant, timed out + captured |
| **git** | `git.status` `git.diff` `git.log` `git.show` `git.add` `git.commit` `git.branch` | structured (`--porcelain`), `execFile` (no shell injection) |
| **doc** | `doc.outline` `doc.section` `doc.write` | token-smart: return a heading outline or one section, not the whole file |
| **cowork** | `todo.write` `todo.read` `artifact.write` `artifact.read` | durable in the stator kv |
| **chat** | `recall` `web.fetch` | semantic recall over turns; bounded web fetch |

### Smart tools save tokens (the marketplace wedge)

Tools are where token cost lives — the tool decides how much of the model's context a
task burns. A naive `read` dumps a 4,000-line file; a smart one paginates or returns
an outline. A naive `find` returns 10,000 paths; a smart one returns 20 ranked hits.
The stdlib's `glob`/`grep`/`doc.outline` are deliberately bounded for exactly this.
Because every run is a recorded, metered tape, a tool's real token/time cost is
**measurable** — so a marketplace can rank tools by measured efficiency, and a better
`read`/`find` (behind the same ToolSpec) drops in and helps everyone.

---

## Build your own tools (the SDK)

"A built-in tool" and "your tool" are the identical mechanism:

```ts
import { defineTool, definePack, installStdlib } from "rrotor/tools";

const shout = defineTool({
  name: "text.shout", version: 1, effect: "pure", grants: [],
  description: "Uppercase a string.",
  input: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
  handler: async (args) => ({ shouted: String(args.text).toUpperCase() }),
});
```

Register it into the connections plugin (via a `ToolRegistry` or `connections.register`)
and it dispatches exactly like a built-in — gated by its `grants`, checkpointed by its
`effect`. MCP methods are a third source on the same interface. The runtime never
cares where a tool came from.

---

## Installing for a run

```ts
import { installStdlib, kvFromStore } from "rrotor/tools";

const res = installStdlib(plugins.connections, {
  root: workspaceDir,
  memory: plugins.memory,
  kv: kvFromStore(store),
  mode: "code",           // or "chat" | "cowork", or an explicit `granted` set
});
// res.installed / res.skipped — the client can show "this mode can't do X"
```

The rotor then calls tools as `tool` steps: `{ flavor: "mcp", name: "file.write",
args: { path, content } }`, with `idempotency: auto` on effectful ones.

---

## Deferred (intentionally)

- **Platform providers** — POSIX-first; a Windows provider drops in behind the same
  tool names when a Windows user needs it (paths/shell live in the provider, the rotor
  stays portable).
- **Version pinning UX** — `version` is on every spec; a rotor pinning a tool version
  (reproducible) vs floating (latest-best) is a small follow-up.
- **Domain packs** — office (pptx/docx), audio (music), image, browser: each is a pack
  of a dozen tools, authored per vertical, on this same contract.

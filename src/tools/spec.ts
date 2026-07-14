/**
 * ToolSpec — the sister spec to RotorSpec. RotorSpec defines the *loop*; ToolSpec
 * defines the *capabilities* the loop can call. A tool is not just a function: it is
 * a typed, versioned, permission-scoped, determinism-classified unit that any
 * implementation can satisfy — which is what lets a better `read` or `find` drop in
 * behind the same name and every rotor benefit for free.
 *
 * Two fields carry the whole design:
 *
 *  - **`effect`** — the determinism class. The runtime records every `tool` step's
 *    output into the event history and, on replay, returns the recorded output
 *    WITHOUT re-dispatching (the executor's replay path + a step's `idempotency:
 *    auto`). So a run that writes a file or runs a command replays without doing it
 *    again. `pure`/`reading` are recompute-safe; `mutating`/`external` MUST be run
 *    with `idempotency: auto`.
 *  - **`grants`** — the capability the tool needs. The permission mode a user picks
 *    resolves to a set of granted capabilities; a tool is installed for a run iff
 *    ALL its grants are held. So "read-only chat" simply never exposes a `mutating`
 *    tool — permissioning is by construction, not by policy checks after the fact.
 */

import type { ConnectionsPlugin, Row, ToolHandler, ToolSchema } from "../plugins/interfaces.js";

/** The determinism class of a tool's effect on the world (SPEC.md §7.2, §7.16). */
export type ToolEffect =
  | "pure" // deterministic function of its inputs; recompute-safe
  | "reading" // reads external state (fs, net) — non-deterministic input, recorded
  | "mutating" // a reversible side effect (write a file, git add) — idempotency-keyed
  | "external"; // an irreversible / outward side effect (deploy, send) — approve + idempotent

/** The canonical capability grants tools require. A permission mode is a set of these. */
export const CAPABILITIES = [
  "fs.read",
  "fs.write",
  "shell.exec",
  "vcs.read",
  "vcs.write",
  "doc.write",
  "cowork.write",
  "memory.read",
  "net.read",
] as const;
export type Capability = (typeof CAPABILITIES)[number];

/**
 * A single tool. `handler` is the implementation; the rest is the contract an
 * alternative implementation must satisfy to be a drop-in swap (the interchange
 * standard that makes a tool marketplace possible).
 */
export interface ToolSpec {
  /** Namespaced, stable id: `file.read`, `git.commit`. */
  name: string;
  /** Interface version — for pinning (reproducible) vs floating (latest-best). */
  version: number;
  description: string;
  /** Determinism class — drives checkpointing/idempotency. */
  effect: ToolEffect;
  /** Capabilities required; the run's permission mode must hold all of them. */
  grants: Capability[];
  /** JSON Schema for the args (the model's contract). */
  input: Record<string, unknown>;
  /** JSON Schema for the result — the interchange contract for swappable impls. */
  output?: Record<string, unknown>;
  /** The implementation. Returns the tool's result payload (wrapped as `{ result }`
   *  by the `tool` step). NEVER read wall-clock/RNG in a way that reaches the tape. */
  handler: ToolHandler;
}

/** A named, versioned bundle of tools (a domain: fs, git, office, audio…). */
export interface ToolPack {
  name: string;
  version: string;
  tools: ToolSpec[];
}

/** The `ToolSchema` a `ToolSpec` advertises to the connections registry. */
function toSchema(t: ToolSpec): ToolSchema {
  return { name: t.name, description: t.description, inputSchema: t.input };
}

export interface InstallResult {
  installed: string[];
  /** Tools skipped because the run lacks their grants (permission mode too narrow). */
  skipped: Array<{ name: string; missing: Capability[] }>;
}

/**
 * The tool registry — the source of truth for `name → ToolSpec`. It installs a
 * pack's handlers into a {@link ConnectionsPlugin}, **filtering by the run's granted
 * capabilities** so the permission mode controls the available surface. It also
 * carries the metadata (effect, grants, version) that governance, discovery, and the
 * efficiency/marketplace layer read.
 */
export class ToolRegistry {
  private readonly specs = new Map<string, ToolSpec>();

  /** Register a pack's tools (last write wins on a name — how a better impl swaps in). */
  add(pack: ToolPack): this {
    for (const t of pack.tools) this.specs.set(t.name, t);
    return this;
  }

  get(name: string): ToolSpec | undefined {
    return this.specs.get(name);
  }
  list(): ToolSpec[] {
    return [...this.specs.values()].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  }

  /**
   * Install into a connections plugin. If `granted` is provided, only tools whose
   * grants are all held are registered — permissioning by construction. If it is
   * omitted, everything installs (bare-box open, matching the governance default).
   */
  install(connections: ConnectionsPlugin, opts: { granted?: ReadonlySet<Capability> } = {}): InstallResult {
    const result: InstallResult = { installed: [], skipped: [] };
    for (const t of this.list()) {
      const missing = opts.granted ? t.grants.filter((g) => !opts.granted!.has(g)) : [];
      if (missing.length > 0) {
        result.skipped.push({ name: t.name, missing });
        continue;
      }
      connections.register(t.name, t.handler, toSchema(t));
      result.installed.push(t.name);
    }
    return result;
  }
}

/** Build a capability set (a permission mode) from a list. */
export function grantSet(caps: Capability[]): ReadonlySet<Capability> {
  return new Set(caps);
}

/**
 * The built-in permission modes — the "read-only chat / co-work / full code" ladder
 * the client exposes. Each is just a capability set; a rotor runs under one and the
 * registry install exposes exactly the tools it permits.
 */
export const MODES = {
  /** Chat: read + recall + web, nothing that mutates. */
  chat: grantSet(["fs.read", "vcs.read", "memory.read", "net.read"]),
  /** Co-work: chat + document/artifact authoring, still no shell or repo writes. */
  cowork: grantSet(["fs.read", "fs.write", "vcs.read", "memory.read", "net.read", "doc.write", "cowork.write"]),
  /** Code: the full workbench — files, shell, and version control. */
  code: grantSet([
    "fs.read",
    "fs.write",
    "shell.exec",
    "vcs.read",
    "vcs.write",
    "memory.read",
    "net.read",
    "doc.write",
    "cowork.write",
  ]),
} as const satisfies Record<string, ReadonlySet<Capability>>;

export type ModeName = keyof typeof MODES;

/** Re-export for pack authors. */
export type { Row, ToolHandler };

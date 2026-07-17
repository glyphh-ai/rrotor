/**
 * The RotorSpec loader + validator — the L1 (Core) surface the CLI drives for
 * `rrotor validate` (docs/runtime.md §4.2, §6.1).
 *
 *   loadRotor(path)      parse a YAML/JSON `.rotor` document → RotorDocument
 *   validateRotor(doc)   JSON-Schema validation (ajv, draft 2020-12) against
 *                        spec/schema/rotor.schema.json, PLUS the static graph
 *                        checks the schema cannot express (docs/runtime.md §4.2):
 *                          - closed step catalog (schema) + unique step ids
 *                          - every `next` / branch / gate / catch / loop / parallel
 *                            target resolves to a step id or a reserved terminal
 *                          - every path reaches a terminal (`end` / `__fail__` /
 *                            a refusing `assert` / a `fail` step)
 *                          - every `loop` carries `max_iterations` (or a budget)
 *                          - `space` declared when a retrieval / gate / hdc.map
 *                            step is present
 *
 * The single JSON Schema under spec/schema/ is the source of truth (docs/runtime.md
 * §6.2); this module consumes it directly, never a hand-copied duplicate.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { Ajv2020, type ErrorObject, type ValidateFunction } from "ajv/dist/2020.js";
import addFormatsModule, { type FormatsPlugin } from "ajv-formats";
import { parse as parseYaml } from "yaml";

// ajv-formats ships CommonJS; under NodeNext the interop default can land on the
// module namespace, so unwrap a `.default` if present to recover the callable.
const addFormats: FormatsPlugin =
  (addFormatsModule as unknown as { default?: FormatsPlugin }).default ??
  (addFormatsModule as unknown as FormatsPlugin);

import {
  RESERVED_TERMINALS,
  type RotorDocument,
  type Step,
} from "../types.js";

// ───────────────────────────────────────────────────────────────────────────
// Result shape.
// ───────────────────────────────────────────────────────────────────────────

/** A single validation failure — schema or structural. */
export interface RotorError {
  /** Where the failure is, e.g. `/spec/steps/2/config` or `spec.steps[recall]`. */
  path: string;
  /** Human-readable message. */
  message: string;
  /** `schema` (ajv) or a structural check name (`unique-id`, `terminal`, …). */
  kind: string;
}

/** The result of {@link validateRotor}. */
export interface ValidationResult {
  valid: boolean;
  errors: RotorError[];
}

// ───────────────────────────────────────────────────────────────────────────
// Schema loading + compilation (once, memoized).
// ───────────────────────────────────────────────────────────────────────────

function schemaPath(name: string): string {
  // From src/parser/ (tsx dev) or dist/parser/ (built) the schema dir is two
  // levels up at <repo>/spec/schema/.
  return fileURLToPath(new URL(`../../spec/schema/${name}`, import.meta.url));
}

function readSchema(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(schemaPath(name), "utf8"));
}

let cachedValidate: ValidateFunction | undefined;

/** Compile (and memoize) the rotor JSON Schema, with step.schema.json added so
 *  the `$ref: "step.schema.json"` resolves. */
export function getRotorValidator(): ValidateFunction {
  if (cachedValidate) return cachedValidate;
  const ajv = new Ajv2020({ allErrors: true, strict: false });
  addFormats(ajv);
  ajv.addSchema(readSchema("step.schema.json"));
  const compiled = ajv.compile(readSchema("rotor.schema.json"));
  cachedValidate = compiled;
  return compiled;
}

// ───────────────────────────────────────────────────────────────────────────
// loadRotor — parse a YAML or JSON document.
// ───────────────────────────────────────────────────────────────────────────

/**
 * Parse a `.rotor` document from disk. YAML and JSON are interchangeable
 * (SPEC.md §4); `.json` is parsed as JSON, everything else as YAML (YAML is a
 * JSON superset, so `.yaml`/`.yml`/`.rotor` all work).
 *
 * This does NOT validate — call {@link validateRotor} on the result.
 */
export function loadRotor(path: string): RotorDocument {
  const raw = readFileSync(path, "utf8");
  const doc = path.endsWith(".json") ? JSON.parse(raw) : parseYaml(raw);
  return doc as RotorDocument;
}

/** Parse a document from an in-memory string (same YAML/JSON rules). */
export function parseRotor(source: string, format: "yaml" | "json" = "yaml"): RotorDocument {
  const doc = format === "json" ? JSON.parse(source) : parseYaml(source);
  return doc as RotorDocument;
}

// ───────────────────────────────────────────────────────────────────────────
// validateRotor — schema validation + static graph checks.
// ───────────────────────────────────────────────────────────────────────────

/**
 * Validate a Rotor Document: first against the JSON Schema (L1), then — only if
 * the shape is sound enough to walk — the static graph checks the schema can't
 * express. Returns `{ valid, errors }`; `valid` is `true` iff `errors` is empty.
 */
export function validateRotor(doc: unknown): ValidationResult {
  const errors: RotorError[] = [];

  // 1) JSON Schema (L1).
  const validate = getRotorValidator();
  const ok = validate(doc);
  if (!ok && validate.errors) {
    for (const e of validate.errors) errors.push(fromAjv(e));
  }

  // If the top-level shape is wrong, the structural walk would just add noise.
  if (!isWalkable(doc)) {
    return { valid: errors.length === 0, errors };
  }

  // 2) Static graph checks.
  errors.push(...structuralChecks(doc as RotorDocument));

  return { valid: errors.length === 0, errors };
}

function fromAjv(e: ErrorObject): RotorError {
  const path = e.instancePath || "/";
  const extra =
    e.keyword === "additionalProperties" && e.params && "additionalProperty" in e.params
      ? ` (unknown property '${(e.params as { additionalProperty: string }).additionalProperty}')`
      : e.keyword === "enum" && e.params && "allowedValues" in e.params
        ? ` (allowed: ${(e.params as { allowedValues: unknown[] }).allowedValues.join(", ")})`
        : "";
  return { path, message: `${e.message ?? "invalid"}${extra}`, kind: "schema" };
}

/** Just enough shape to run the graph walk without throwing. */
function isWalkable(doc: unknown): doc is RotorDocument {
  if (typeof doc !== "object" || doc === null) return false;
  const spec = (doc as { spec?: unknown }).spec;
  if (typeof spec !== "object" || spec === null) return false;
  const steps = (spec as { steps?: unknown }).steps;
  return Array.isArray(steps);
}

// ───────────────────────────────────────────────────────────────────────────
// Static graph checks (docs/runtime.md §4.2).
// ───────────────────────────────────────────────────────────────────────────

/** Step types that are themselves terminals (a refusing/asserting terminal, or
 *  a typed failure). SPEC.md §7.9, §7.20; §4.2. */
const TERMINAL_STEP_TYPES = new Set(["assert", "fail"]);

/** Step types that REQUIRE a declared `space` (SPEC.md §4.1, docs/runtime.md
 *  §4.2 — space_id validated on every retrieval / gate / encode step). */
const SPACE_REQUIRING_TYPES = new Set([
  "hdc.map",
  "write",
  "retrieve.sql",
  "retrieve.kb",
  "retrieve.vector",
  "gate",
]);

function structuralChecks(doc: RotorDocument): RotorError[] {
  const errors: RotorError[] = [];
  const steps = doc.spec.steps;

  // ── unique step ids ──
  const ids = new Set<string>();
  const dupes = new Set<string>();
  for (const s of steps) {
    if (typeof s?.id !== "string") continue;
    if (ids.has(s.id)) dupes.add(s.id);
    ids.add(s.id);
  }
  for (const id of dupes) {
    errors.push({
      path: `spec.steps[${id}]`,
      message: `duplicate step id '${id}' — step ids MUST be unique within the rotor`,
      kind: "unique-id",
    });
  }

  // Valid edge targets = any step id or a reserved terminal.
  const isTarget = (t: string): boolean =>
    ids.has(t) || (RESERVED_TERMINALS as readonly string[]).includes(t);

  // ── entry resolves ──
  const entry = doc.spec.entry ?? steps[0]?.id;
  if (doc.spec.entry && !ids.has(doc.spec.entry)) {
    errors.push({
      path: "spec.entry",
      message: `entry step '${doc.spec.entry}' does not exist`,
      kind: "entry",
    });
  }

  // ── per-step: edge targets resolve, loop caps present, collect adjacency ──
  const outgoing = new Map<string, string[]>();
  for (const s of steps) {
    if (typeof s?.id !== "string") continue;
    const targets = edgeTargets(s);
    outgoing.set(s.id, targets);

    for (const { target, where } of enumerateEdges(s)) {
      if (!isTarget(target)) {
        errors.push({
          path: `spec.steps[${s.id}].${where}`,
          message: `'${where}' target '${target}' resolves to no step id or reserved terminal (${RESERVED_TERMINALS.join(", ")})`,
          kind: "target",
        });
      }
    }

    // loop MUST carry max_iterations or a budget (SPEC.md §7.12).
    if (s.type === "loop") {
      const cfg = (s.config ?? {}) as { max_iterations?: unknown; budget?: unknown };
      const hasCap =
        (typeof cfg.max_iterations === "number" && cfg.max_iterations > 0) ||
        cfg.budget != null;
      if (!hasCap) {
        errors.push({
          path: `spec.steps[${s.id}].config`,
          message: "loop MUST declare a positive 'max_iterations' or a 'budget' cap — termination must be guaranteed",
          kind: "loop-cap",
        });
      }
    }

    // A non-terminal step with no outgoing edge can never reach a terminal.
    if (!TERMINAL_STEP_TYPES.has(s.type) && targets.length === 0) {
      errors.push({
        path: `spec.steps[${s.id}]`,
        message: `step '${s.id}' (type '${s.type}') has no successor and is not a terminal — every path MUST reach a terminal (end / __fail__ / assert / fail)`,
        kind: "terminal",
      });
    }
  }

  // ── space required when a retrieval / gate / encode step is present ──
  if (!doc.spec.space) {
    const needer = steps.find((s) => SPACE_REQUIRING_TYPES.has(s?.type));
    if (needer) {
      errors.push({
        path: "spec.space",
        message: `spec.space is REQUIRED because step '${needer.id}' (type '${needer.type}') binds an HDC space (§4.1, §15.4)`,
        kind: "space",
      });
    }
  }

  // ── reachability: every step reachable from entry can reach a terminal ──
  if (entry && ids.has(entry) && dupes.size === 0) {
    errors.push(...reachabilityChecks(steps, entry, outgoing));
  }

  return errors;
}

/** Does a target string denote reaching a terminal (a reserved terminal)? */
function isReservedTerminal(t: string): boolean {
  return (RESERVED_TERMINALS as readonly string[]).includes(t);
}

interface EdgeRef {
  target: string;
  where: string;
}

/** Enumerate every outgoing edge of a step, with a label for error paths. */
function enumerateEdges(s: Step): EdgeRef[] {
  const edges: EdgeRef[] = [];
  const push = (target: unknown, where: string) => {
    if (typeof target === "string") edges.push({ target, where });
  };

  push(s.next, "next");
  for (const [i, c] of (s.catch ?? []).entries()) push(c?.next, `catch[${i}].next`);

  const cfg = (s.config ?? {}) as Record<string, unknown>;
  switch (s.type) {
    case "branch": {
      const cases = (cfg.cases as Array<{ next?: unknown }>) ?? [];
      cases.forEach((c, i) => push(c?.next, `config.cases[${i}].next`));
      push(cfg.default, "config.default");
      break;
    }
    case "gate": {
      push(cfg.on_pass, "config.on_pass");
      push(cfg.on_fail, "config.on_fail");
      push(cfg.on_escalate, "config.on_escalate");
      break;
    }
    case "loop": {
      push(cfg.body, "config.body");
      break;
    }
    case "parallel": {
      const branches = (cfg.branches as unknown[]) ?? [];
      branches.forEach((b, i) => push(b, `config.branches[${i}]`));
      push(cfg.body, "config.body");
      break;
    }
    default:
      break;
  }
  return edges;
}

/** The distinct set of edge target strings out of a step (for adjacency). */
function edgeTargets(s: Step): string[] {
  const seen = new Set<string>();
  for (const e of enumerateEdges(s)) seen.add(e.target);
  return [...seen];
}

/** Reachability-to-terminal: every step reachable from `entry` MUST be able to
 *  reach a terminal (a reserved terminal, or a terminal step type). */
function reachabilityChecks(
  steps: Step[],
  entry: string,
  outgoing: Map<string, string[]>,
): RotorError[] {
  const errors: RotorError[] = [];
  const byId = new Map(steps.filter((s) => typeof s?.id === "string").map((s) => [s.id, s]));

  // Forward reachability from entry.
  const reachable = new Set<string>();
  const stack = [entry];
  while (stack.length) {
    const id = stack.pop()!;
    if (reachable.has(id)) continue;
    reachable.add(id);
    for (const t of outgoing.get(id) ?? []) {
      if (!isReservedTerminal(t) && byId.has(t)) stack.push(t);
    }
  }

  // Can a step reach a terminal? Memoized DFS with cycle guard.
  const canReach = new Map<string, boolean>();
  const reaches = (id: string, onPath: Set<string>): boolean => {
    if (canReach.has(id)) return canReach.get(id)!;
    const step = byId.get(id);
    if (step && TERMINAL_STEP_TYPES.has(step.type)) {
      canReach.set(id, true);
      return true;
    }
    onPath.add(id);
    let ok = false;
    for (const t of outgoing.get(id) ?? []) {
      if (isReservedTerminal(t)) {
        ok = true;
        break;
      }
      if (!byId.has(t)) continue; // unresolved target already reported
      if (onPath.has(t)) continue; // cycle — try other edges
      if (reaches(t, onPath)) {
        ok = true;
        break;
      }
    }
    onPath.delete(id);
    // Don't memoize a false reached via a cut cycle edge as permanent unless the
    // path set is empty of ambiguity; simplest sound approach: memoize true only.
    if (ok) canReach.set(id, true);
    return ok;
  };

  for (const id of reachable) {
    if (!reaches(id, new Set())) {
      const step = byId.get(id);
      errors.push({
        path: `spec.steps[${id}]`,
        message: `step '${id}'${step ? ` (type '${step.type}')` : ""} cannot reach a terminal — every path MUST reach 'end', '__fail__', a refusing 'assert', or a 'fail' step`,
        kind: "terminal",
      });
    }
  }

  return errors;
}

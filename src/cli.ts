#!/usr/bin/env node
/**
 * openrotor — the CLI entry point.
 *
 *   openrotor                    launch the REPL (default)
 *   openrotor run <file> [k=v…]  execute a .rotor through the basic-tier executor
 *   openrotor validate <file>    validate against schema + static graph checks
 *   openrotor serve [-p PORT]    start the HTTP runtime (probes + /run)
 *   openrotor version | help
 */

import { fileURLToPath } from "node:url";

import { printBanner } from "./banner.js";
import { loadRotor, validateRotor } from "./parser/index.js";
import { execute } from "./exec/executor.js";
import { buildBasicPlugins } from "./plugins/index.js";
import { startServer } from "./server.js";
import { runRepl } from "./repl.js";
import { VERSION } from "./version.js";
import { describe, errorCatalog } from "./errors.js";
import { statorFromEnvAsync } from "./exec/stator.js";
import { traceId } from "./obs/trace.js";
import type { RotorDocument, StepRecord } from "./types.js";

/** `openrotor validate <file>` — L1 parse + JSON Schema + static graph checks.
 *  Exit code is the conformance gate: 0 = valid, 1 = invalid / error. */
function runValidate(file: string | undefined): number {
  if (!file) {
    console.error("validate: missing <file> (usage: openrotor validate <file>)");
    return 1;
  }
  let doc: unknown;
  try {
    doc = loadRotor(file);
  } catch (err) {
    console.error(`validate: cannot load ${file}: ${(err as Error).message}`);
    return 1;
  }
  const { valid, errors } = validateRotor(doc);
  if (valid) {
    console.log(`✓ ${file} is a valid RotorSpec document`);
    return 0;
  }
  console.error(`✗ ${file} — ${errors.length} error${errors.length === 1 ? "" : "s"}:`);
  for (const e of errors) {
    console.error(`  [${e.kind}] ${e.path}: ${e.message}`);
  }
  return 1;
}

/** Parse trailing `key=value` tokens into an inputs object. */
function parseInputs(tokens: string[]): Record<string, unknown> {
  const inputs: Record<string, unknown> = {};
  for (const tok of tokens) {
    const eq = tok.indexOf("=");
    if (eq <= 0) continue;
    const key = tok.slice(0, eq);
    const raw = tok.slice(eq + 1);
    // Try JSON (numbers, booleans, arrays, objects); fall back to the raw string.
    try {
      inputs[key] = JSON.parse(raw);
    } catch {
      inputs[key] = raw;
    }
  }
  return inputs;
}

/** Fill any required input the caller omitted with a placeholder so the demo
 *  run reaches a terminal instead of throwing E_MISSING_INPUT. */
function fillRequired(doc: RotorDocument, inputs: Record<string, unknown>): Record<string, unknown> {
  const out = { ...inputs };
  for (const p of doc.spec.inputs ?? []) {
    if (p.required && out[p.name] === undefined && p.default === undefined) {
      out[p.name] = `<${p.name}>`;
    }
  }
  return out;
}

/** `openrotor run <file> [k=v…]` — load + validate + execute + print the run. */
async function runRotorFile(file: string | undefined, rest: string[]): Promise<number> {
  if (!file) {
    console.error("run: missing <file> (usage: openrotor run <file> [key=value …])");
    return 1;
  }
  let doc: RotorDocument;
  try {
    doc = loadRotor(file);
  } catch (err) {
    console.error(`run: cannot load ${file}: ${(err as Error).message}`);
    return 1;
  }
  const { valid, errors } = validateRotor(doc);
  if (!valid) {
    console.error(`run: ${file} is not a valid RotorSpec document (${errors.length} error${errors.length === 1 ? "" : "s"}):`);
    for (const e of errors) console.error(`  [${e.kind}] ${e.path}: ${e.message}`);
    return 1;
  }

  const inputs = fillRequired(doc, parseInputs(rest));
  // Persist to the env-configured stator so `openrotor support <run_id>` can pull
  // the run's tape afterward (durable backends only; in-memory is per-process).
  const store = await statorFromEnvAsync();
  const plugins = buildBasicPlugins({ store });

  let result;
  try {
    result = await execute(doc, inputs, plugins);
  } catch (err) {
    console.error(`run: execution error: ${(err as Error).message}`);
    return 1;
  }

  const ns = doc.metadata.namespace ? `${doc.metadata.namespace}/` : "";
  console.log(`▸ ${ns}${doc.metadata.name}@${doc.metadata.version}  run ${result.run_id}`);
  console.log(`  trace:  ${result.trace_id}`);
  console.log(`  inputs: ${JSON.stringify(inputs)}`);
  console.log("");
  console.log("  step records:");
  for (const rec of result.history) {
    const frameTypes = (rec.frames ?? []).map((f) => f.type).join(",");
    const errMark = rec.error ? `  ✗ ${rec.error.name}` : "";
    console.log(
      `    #${rec.logical_tick.toString().padStart(2)} ${rec.step_id.padEnd(12)} ` +
        `${rec.status.padEnd(10)} ${frameTypes ? `[${frameTypes}]` : ""}${errMark}`,
    );
  }
  console.log("");
  console.log(`  terminal: ${result.terminal}`);
  console.log(`  status:   ${result.status}`);
  console.log(`  outputs:  ${JSON.stringify(result.outputs, null, 2).replace(/\n/g, "\n            ")}`);
  // On failure, surface the taxonomy code + remediation right here — no log-diving.
  if (result.error) printErrorHelp(result.error);
  console.log("");
  await store.close?.();

  // Exit non-zero when the run failed, so scripts can gate on it.
  return result.status === "failed" ? 1 : 0;
}

/** Print a taxonomy-backed error block: what failed, whether it retries, how to fix. */
function printErrorHelp(err: { name: string; cause?: string }): void {
  const d = describe(err.name);
  console.log("");
  console.log(`  ✗ error:  ${d.code}  (${d.category}, retryable=${d.retryable}, severity=${d.severity})`);
  if (err.cause) console.log(`            ${err.cause}`);
  console.log(`            ↳ fix: ${d.remediation}`);
  console.log(`            ↳ see: docs/errors.md#codes  ·  \`openrotor errors ${d.code}\``);
}

/** `openrotor errors [CODE] [--json]` — the error catalog for humans and dev-ops
 *  agents. No args: the whole table. A code: that entry's detail. `--json`: the
 *  machine-readable catalog. */
function runErrors(rest: string[]): number {
  const json = rest.includes("--json");
  const code = rest.find((r) => !r.startsWith("--"));
  const rows = errorCatalog();
  if (code) {
    const d = describe(code);
    if (json) console.log(JSON.stringify(d, null, 2));
    else {
      console.log(`${d.code}  (${d.category}, retryable=${d.retryable}, severity=${d.severity}, http=${d.httpStatus})`);
      console.log(`  ${d.summary}`);
      console.log(`  fix: ${d.remediation}`);
    }
    return 0;
  }
  if (json) {
    console.log(JSON.stringify(rows, null, 2));
    return 0;
  }
  console.log("OpenRotor error catalog (docs/errors.md):\n");
  for (const r of rows) {
    console.log(`  ${r.code.padEnd(20)} ${r.category.padEnd(12)} retry=${r.retryable ? "y" : "n"}  ${r.summary}`);
  }
  console.log(`\n  ${rows.length} codes. \`openrotor errors <CODE>\` for remediation, \`--json\` for machine output.`);
  return 0;
}

/** `openrotor support <run_id> [--json]` — a support bundle for a run: its trace id
 *  and every step's status + any taxonomy error + remediation, pulled from the
 *  durable stator. The artifact you attach to a ticket or hand an AI dev-ops agent. */
async function runSupport(rest: string[]): Promise<number> {
  const runId = rest.find((r) => !r.startsWith("--"));
  if (!runId) {
    console.error("support: missing <run_id> (usage: openrotor support <run_id> [--json])");
    return 1;
  }
  const store = await statorFromEnvAsync();
  const history: StepRecord[] = await store.history.read(runId);
  const failing = history.filter((h) => h.error).map((h) => ({
    step_id: h.step_id,
    attempt: h.attempt,
    ...describe(h.error!.name),
    cause: h.error!.cause,
  }));
  const bundle = {
    run_id: runId,
    trace_id: traceId(runId),
    steps: history.length,
    status: history.length === 0 ? "not-found" : "found",
    errors: failing,
    timeline: history.map((h) => ({ tick: h.logical_tick, step_id: h.step_id, status: h.status, error: h.error?.name })),
  };
  await store.close?.();
  if (rest.includes("--json")) {
    console.log(JSON.stringify(bundle, null, 2));
    return 0;
  }
  if (history.length === 0) {
    console.log(`No history for run ${runId}. (A durable stator is required — set ROTOR_STATOR_BACKEND=sqlite|pgvector.)`);
    return 1;
  }
  console.log(`Support bundle · run ${runId}`);
  console.log(`  trace: ${bundle.trace_id}`);
  console.log(`  steps: ${bundle.steps}`);
  console.log("  timeline:");
  for (const t of bundle.timeline) {
    console.log(`    #${String(t.tick).padStart(2)} ${t.step_id.padEnd(12)} ${t.status}${t.error ? `  ✗ ${t.error}` : ""}`);
  }
  if (failing.length > 0) {
    console.log("  errors:");
    for (const f of failing) {
      console.log(`    ${f.step_id}: ${f.code} (${f.category}) — ${f.cause ?? ""}`);
      console.log(`      ↳ fix: ${f.remediation}`);
    }
  }
  return 0;
}

/** `openrotor serve [-p PORT]` — start the HTTP runtime and block. */
function runServe(rest: string[]): Promise<number> {
  let port = Number(process.env.PORT ?? process.env.ROTOR_PORT ?? 8080);
  for (let i = 0; i < rest.length; i++) {
    if ((rest[i] === "-p" || rest[i] === "--port") && rest[i + 1]) {
      port = Number(rest[i + 1]);
      i++;
    }
  }
  if (!Number.isFinite(port)) port = 8080;
  startServer(port);
  // Never resolves — the server owns the process until it is killed.
  return new Promise<number>(() => {});
}

export async function main(argv: string[]): Promise<number> {
  const [cmd, ...rest] = argv;
  switch (cmd) {
    case undefined:
    case "repl":
      return runRepl();
    case "version":
    case "--version":
    case "-v":
      console.log(`openrotor v${VERSION}`);
      return 0;
    case "run":
      return runRotorFile(rest[0], rest.slice(1));
    case "validate":
      return runValidate(rest[0]);
    case "errors":
      return runErrors(rest);
    case "support":
      return runSupport(rest);
    case "serve":
      return runServe(rest);
    case "help":
    case "--help":
    case "-h":
      printHelp();
      return 0;
    default:
      console.error(`unknown command: ${cmd} (try: openrotor help)`);
      return 1;
  }
}

function printHelp(): void {
  printBanner(VERSION);
  console.log(`  openrotor                     launch the REPL
  openrotor run <file> [k=v…]   execute a .rotor through the executor
  openrotor validate <file>     validate a .rotor against the schema
  openrotor errors [CODE]       the error catalog (--json for machine output)
  openrotor support <run_id>    a support bundle for a run (trace + errors + fixes)
  openrotor serve [-p PORT]     start the HTTP runtime (probes + /run)
  openrotor version
`);
}

// Run when invoked directly (as the bin), not when imported.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main(process.argv.slice(2)).then((code) => process.exit(code));
}

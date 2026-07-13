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
import type { RotorDocument } from "./types.js";

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
  const plugins = buildBasicPlugins();

  let result;
  try {
    result = await execute(doc, inputs, plugins);
  } catch (err) {
    console.error(`run: execution error: ${(err as Error).message}`);
    return 1;
  }

  const ns = doc.metadata.namespace ? `${doc.metadata.namespace}/` : "";
  console.log(`▸ ${ns}${doc.metadata.name}@${doc.metadata.version}  run ${result.run_id}`);
  console.log(`  inputs: ${JSON.stringify(inputs)}`);
  console.log("");
  console.log("  step records:");
  for (const rec of result.history) {
    const frameTypes = (rec.frames ?? []).map((f) => f.type).join(",");
    console.log(
      `    #${rec.logical_tick.toString().padStart(2)} ${rec.step_id.padEnd(12)} ` +
        `${rec.status.padEnd(10)} ${frameTypes ? `[${frameTypes}]` : ""}`,
    );
  }
  console.log("");
  console.log(`  terminal: ${result.terminal}`);
  console.log(`  status:   ${result.status}`);
  console.log(`  outputs:  ${JSON.stringify(result.outputs, null, 2).replace(/\n/g, "\n            ")}`);
  console.log("");

  // Exit non-zero when the run failed, so scripts can gate on it.
  return result.status === "failed" ? 1 : 0;
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
  console.log(`  openrotor                    launch the REPL
  openrotor run <file> [k=v…]  execute a .rotor through the executor
  openrotor validate <file>    validate a .rotor against the schema
  openrotor serve [-p PORT]    start the HTTP runtime (probes + /run)
  openrotor version
`);
}

// Run when invoked directly (as the bin), not when imported.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main(process.argv.slice(2)).then((code) => process.exit(code));
}

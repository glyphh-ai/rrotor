/**
 * The OpenRotor REPL — the human face of the runtime.
 *
 * Prints the banner + capability manifest, then a readline loop (up-arrow
 * history + tab completion come free from node:readline). Every command is a
 * thin verb over the Runtime; the REPL owns no logic. Command handling is
 * factored into {@link execCommand} so it is unit-testable without a TTY, and
 * `validate` / `run` execute against the real parser + basic-tier executor.
 */

import * as readline from "node:readline";

import { printBanner } from "./banner.js";
import { Runtime } from "./runtime/runtime.js";
import { VERSION } from "./version.js";
import { loadRotor, validateRotor } from "./parser/index.js";
import { execute } from "./exec/executor.js";
import type { RotorDocument } from "./types.js";

const C = "\x1b[36m";
const W = "\x1b[97m";
const D = "\x1b[90m";
const R = "\x1b[0m";

const COMMANDS = ["help", "status", "version", "validate", "run", "clear", "quit", "exit"];

export function runRepl(): Promise<number> {
  const rt = new Runtime();
  printBanner(VERSION);
  printLines(statusLines(rt));
  console.log(`  ${D}type${R} help ${D}for commands, ${R}quit${D} to exit${R}\n`);

  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    prompt: `  ${C}rotor${R} ${D}›${R} `,
    completer,
  });
  rl.prompt();

  return new Promise<number>((resolve) => {
    rl.on("line", (line) => {
      const [cmd = "", ...rest] = line.trim().split(/\s+/);
      const arg = rest.join(" ");
      const lc = cmd.toLowerCase();
      if (lc === "quit" || lc === "exit" || lc === "q") {
        rl.close();
        return;
      }
      if (lc === "clear") {
        process.stdout.write("\x1b[2J\x1b[3J\x1b[H");
        printBanner(VERSION);
        rl.prompt();
        return;
      }
      void execCommand(rt, cmd, arg).then((lines) => {
        printLines(lines);
        rl.prompt();
      });
    });
    rl.on("close", () => {
      console.log();
      resolve(0);
    });
  });
}

/**
 * Execute one REPL command, returning the lines to print. Pure over `rt` +
 * filesystem so tests can drive it directly. `quit`/`exit` are handled by the
 * loop (they close the stream), not here.
 */
export async function execCommand(rt: Runtime, cmd: string, arg: string): Promise<string[]> {
  switch (cmd.toLowerCase()) {
    case "":
      return [];
    case "help":
    case "?":
      return helpLines();
    case "status":
      return statusLines(rt);
    case "version":
      return [`  openrotor v${VERSION}`, ""];
    case "clear":
      // The loop clears the screen; here it is a no-op set of lines.
      return [];
    case "validate":
      return validateCommand(arg);
    case "run":
      return runCommand(rt, arg);
    default:
      return [`  ${D}unknown:${R} ${cmd} ${D}(try help)${R}`, ""];
  }
}

function validateCommand(file: string): string[] {
  if (!file) return [`  ${D}usage: validate <file>${R}`, ""];
  let doc: RotorDocument;
  try {
    doc = loadRotor(file);
  } catch (err) {
    return [`  ${D}cannot load ${file}: ${(err as Error).message}${R}`, ""];
  }
  const { valid, errors } = validateRotor(doc);
  if (valid) return [`  ${C}✓${R} ${file} is a valid RotorSpec document`, ""];
  const lines = [`  ${D}✗ ${file} — ${errors.length} error${errors.length === 1 ? "" : "s"}${R}`];
  for (const e of errors) lines.push(`    ${D}[${e.kind}] ${e.path}: ${e.message}${R}`);
  lines.push("");
  return lines;
}

async function runCommand(rt: Runtime, arg: string): Promise<string[]> {
  const [file, ...kv] = arg.split(/\s+/).filter(Boolean);
  if (!file) return [`  ${D}usage: run <file> [key=value …]${R}`, ""];
  let doc: RotorDocument;
  try {
    doc = loadRotor(file);
  } catch (err) {
    return [`  ${D}cannot load ${file}: ${(err as Error).message}${R}`, ""];
  }
  const { valid, errors } = validateRotor(doc);
  if (!valid) {
    return [
      `  ${D}✗ ${file} is invalid (${errors.length} error${errors.length === 1 ? "" : "s"}) — fix before running${R}`,
      "",
    ];
  }
  const inputs = parseInputs(kv);
  let result;
  try {
    result = await execute(doc, inputs, rt.plugins);
  } catch (err) {
    return [`  ${D}run error: ${(err as Error).message}${R}`, ""];
  }
  const lines = [
    `  ${C}▸${R} ${doc.metadata.name}@${doc.metadata.version}  run ${result.run_id}`,
    `  ${D}status ${result.status} · terminal ${result.terminal} · ${result.history.length} steps${R}`,
    `  ${D}outputs${R} ${JSON.stringify(result.outputs)}`,
    "",
  ];
  return lines;
}

/** Parse trailing `key=value` tokens (JSON-typed, falling back to string). */
function parseInputs(tokens: string[]): Record<string, unknown> {
  const inputs: Record<string, unknown> = {};
  for (const tok of tokens) {
    const eq = tok.indexOf("=");
    if (eq <= 0) continue;
    const key = tok.slice(0, eq);
    const raw = tok.slice(eq + 1);
    try {
      inputs[key] = JSON.parse(raw);
    } catch {
      inputs[key] = raw;
    }
  }
  return inputs;
}

function completer(line: string): [string[], string] {
  const hits = COMMANDS.filter((c) => c.startsWith(line));
  return [hits.length ? hits : COMMANDS, line];
}

function dot(ready: boolean): string {
  return ready ? `${C}●${R}` : `${D}○${R}`;
}

function statusLines(rt: Runtime): string[] {
  const lines: string[] = [];
  for (const [name, st] of Object.entries(rt.status())) {
    lines.push(`  ${dot(st.ready)} ${W}${name.padEnd(12)}${R}${D}${st.detail}${R}`);
  }
  lines.push("");
  return lines;
}

function helpLines(): string[] {
  return [
    "  commands",
    "    status            capability manifest",
    "    validate <file>   validate a .rotor against the schema",
    "    run <file> [k=v]  execute a .rotor",
    "    version · clear · help · quit",
    "",
  ];
}

function printLines(lines: string[]): void {
  for (const ln of lines) console.log(ln);
}

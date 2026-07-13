/**
 * The OpenRotor REPL — the human face of the runtime.
 *
 * Prints the banner + capability manifest, then a readline loop (up-arrow
 * history + tab completion come free from node:readline). Every command is a
 * thin verb over the Runtime; the REPL owns no logic. `run`/`validate` are
 * placeholders until the executor + schema validator land.
 */

import * as readline from "node:readline";

import { printBanner } from "./banner.js";
import { Runtime } from "./runtime/runtime.js";
import { VERSION } from "./version.js";

const C = "\x1b[36m";
const W = "\x1b[97m";
const D = "\x1b[90m";
const R = "\x1b[0m";

const COMMANDS = ["help", "status", "version", "validate", "run", "clear", "quit", "exit"];

export function runRepl(): Promise<number> {
  const rt = new Runtime();
  printBanner(VERSION);
  printStatus(rt);
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
      switch (cmd.toLowerCase()) {
        case "":
          break;
        case "quit":
        case "exit":
        case "q":
          rl.close();
          return;
        case "help":
        case "?":
          printHelp();
          break;
        case "status":
          printStatus(rt);
          break;
        case "version":
          console.log(`  openrotor v${VERSION}\n`);
          break;
        case "clear":
          process.stdout.write("\x1b[2J\x1b[3J\x1b[H");
          printBanner(VERSION);
          break;
        case "validate":
          console.log(`  ${D}validate ${arg || "<file>"} — schema validator lands next build phase${R}\n`);
          break;
        case "run":
          console.log(`  ${D}run ${arg || "<file>"} — executor lands next build phase${R}\n`);
          break;
        default:
          console.log(`  ${D}unknown:${R} ${cmd} ${D}(try help)${R}\n`);
      }
      rl.prompt();
    });
    rl.on("close", () => {
      console.log();
      resolve(0);
    });
  });
}

function completer(line: string): [string[], string] {
  const hits = COMMANDS.filter((c) => c.startsWith(line));
  return [hits.length ? hits : COMMANDS, line];
}

function dot(ready: boolean): string {
  return ready ? `${C}●${R}` : `${D}○${R}`;
}

function printStatus(rt: Runtime): void {
  for (const [name, st] of Object.entries(rt.status())) {
    console.log(`  ${dot(st.ready)} ${W}${name.padEnd(12)}${R}${D}${st.detail}${R}`);
  }
  console.log();
}

function printHelp(): void {
  console.log(`  commands
    status            capability manifest
    validate <file>   validate a .rotor against the schema
    run <file>        execute a .rotor
    version · clear · help · quit
`);
}

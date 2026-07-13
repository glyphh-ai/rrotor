#!/usr/bin/env node
/**
 * openrotor — the CLI entry point.
 *
 *   openrotor                 launch the REPL
 *   openrotor run <file>      execute a .rotor         (executor: next phase)
 *   openrotor validate <file> validate against schema  (validator: next phase)
 *   openrotor version | help
 */

import { fileURLToPath } from "node:url";

import { printBanner } from "./banner.js";
import { runRepl } from "./repl.js";
import { VERSION } from "./version.js";

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
      console.log(`run: ${rest[0] ?? "<file>"} — executor lands next build phase`);
      return 0;
    case "validate":
      console.log(`validate: ${rest[0] ?? "<file>"} — schema validator lands next build phase`);
      return 0;
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
  console.log(`  openrotor                 launch the REPL
  openrotor run <file>      execute a .rotor
  openrotor validate <file> validate a .rotor against the schema
  openrotor version
`);
}

// Run when invoked directly (as the bin), not when imported.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main(process.argv.slice(2)).then((code) => process.exit(code));
}

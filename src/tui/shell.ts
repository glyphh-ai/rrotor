/**
 * The `glyphh` interactive shell — the thin readline front-end over {@link Session}.
 * One prompt interface: type a message → the selected rotor runs and streams; type a
 * `/command` → switch rotor/model or inspect. Chat, co-work, and code are all "just a
 * rotor" here. This file is the I/O skin; the logic + rendering are testable modules.
 */

import * as readline from "node:readline";

import { printBanner } from "../banner.js";
import { VERSION } from "../version.js";
import { statorFromEnvAsync } from "../exec/stator.js";
import { Session, loadRotors, type TurnEvent } from "./session.js";
import { renderEvent, header } from "./render.js";

export interface ShellOptions {
  rotorsDir?: string;
  workspace?: string;
  rotor?: string;
}

/** Launch the interactive TUI. Resolves when the user exits. */
export async function runShell(opts: ShellOptions = {}): Promise<number> {
  const rotorsDir = opts.rotorsDir ?? "rotors";
  const workspace = opts.workspace ?? process.cwd();
  const rotors = loadRotors(rotorsDir);
  const store = await statorFromEnvAsync();

  const session = new Session({ store, workspace, rotors, rotor: opts.rotor ?? "base" });

  printBanner(VERSION);
  process.stdout.write(
    `  ${header({ rotor: session.rotorName, mode: session.mode, model: session.model })}\n` +
      `  \x1b[90mtype a message, or /help. ${rotors.size} rotor${rotors.size === 1 ? "" : "s"} loaded.\x1b[0m\n\n`,
  );

  const rl = readline.createInterface({ input: process.stdin, output: process.stdout, prompt: "\x1b[36m› \x1b[0m" });
  const emit = (e: TurnEvent) => process.stdout.write(renderEvent(e) + "\n");

  const ask = (q: string): Promise<string> => new Promise((res) => rl.question(q, res));

  rl.prompt();
  for await (const line of rl) {
    const input = line.trim();
    if (!input) {
      rl.prompt();
      continue;
    }
    if (input.startsWith("/")) {
      const r = session.command(input);
      if (r.message) process.stdout.write(r.message + "\n");
      if (r.quit) break;
      // Refresh the header after a rotor/model switch.
      process.stdout.write("  " + header({ rotor: session.rotorName, mode: session.mode, model: session.model }) + "\n");
      rl.prompt();
      continue;
    }

    try {
      const result = await session.turn(input, emit);
      // Human-in-the-loop: if the turn paused at an approval step, ask and resume.
      while (result && session.awaiting) {
        const yn = (await ask("\x1b[33m  approve? (y/n) \x1b[0m")).trim().toLowerCase();
        await session.resume({ decision: yn === "y" || yn === "yes" ? "approve" : "deny" }, emit);
      }
    } catch (e) {
      process.stdout.write("\x1b[31m✗ " + (e as Error).message + "\x1b[0m\n");
    }
    process.stdout.write("\n");
    rl.prompt();
  }

  rl.close();
  await store.close?.();
  return 0;
}

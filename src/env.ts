/**
 * env.ts — minimal .env loading, imported FIRST by the CLI entry so every
 * module that reads the environment at load time sees the file's values.
 * Precedence: real environment > ./.env > ~/.rrotor/.env. Never overrides —
 * an exported shell variable always wins. KEY=VALUE lines, `#` comments,
 * optional surrounding quotes. No dependency; ~20 lines beats one.
 */

import { homedir } from "node:os";
import { join } from "node:path";
import { existsSync, readFileSync } from "node:fs";

/** The .env files actually loaded this process — surfaced in the TUI welcome
 *  card so "why aren't my settings applying" is answerable at a glance. */
export const loadedEnvFiles: string[] = [];

function loadFile(path: string): void {
  if (!existsSync(path)) return;
  loadedEnvFiles.push(path);
  // Tolerant of the shapes people actually write: CRLF endings, shell-style
  // `export KEY=…`, surrounding quotes, comments.
  for (const raw of readFileSync(path, "utf8").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim().replace(/^export\s+/, "");
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (key && process.env[key] === undefined) process.env[key] = value;
  }
}

loadFile(join(process.cwd(), ".env"));
loadFile(join(homedir(), ".rrotor", ".env"));

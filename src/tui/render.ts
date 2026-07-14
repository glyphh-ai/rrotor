/**
 * Pure rendering for the TUI — a {@link TurnEvent} → a display string. Kept separate
 * from the readline shell so it is unit-testable and so a different front-end (the
 * desktop app) can render the same event stream its own way.
 */

import type { TurnEvent } from "./session.js";

const C = {
  dim: (s: string) => `\x1b[90m${s}\x1b[0m`,
  green: (s: string) => `\x1b[32m${s}\x1b[0m`,
  red: (s: string) => `\x1b[31m${s}\x1b[0m`,
  yellow: (s: string) => `\x1b[33m${s}\x1b[0m`,
  cyan: (s: string) => `\x1b[36m${s}\x1b[0m`,
  bold: (s: string) => `\x1b[1m${s}\x1b[0m`,
};

/** Render one streamed event to a line (no trailing newline). */
export function renderEvent(e: TurnEvent, opts: { color?: boolean } = {}): string {
  const c = opts.color === false ? passthrough : C;
  switch (e.kind) {
    case "step": {
      const mark = e.status === "ok" ? c.green("✓") : e.status === "failed" ? c.red("✗") : c.yellow("•");
      const frames = e.frames.length ? c.dim(` [${e.frames.join(",")}]`) : "";
      const err = e.error ? c.red("  " + e.error) : "";
      return `  ${mark} ${c.dim(e.step_id.padEnd(10))} ${c.dim(e.status)}${frames}${err}`;
    }
    case "answer":
      return e.text ? c.green(e.text) : c.dim("(no answer — the model lane is stubbed on the basic tier)");
    case "interrupt":
      return c.yellow(`⏸ awaiting approval at step "${e.step_id}"`);
    case "error":
      return `${c.red("✗ " + e.code)} ${e.detail}\n  ${c.dim("↳ " + e.remediation)}`;
    case "info":
      return c.dim(e.text);
  }
}

const passthrough = {
  dim: (s: string) => s,
  green: (s: string) => s,
  red: (s: string) => s,
  yellow: (s: string) => s,
  cyan: (s: string) => s,
  bold: (s: string) => s,
};

/** The one-line status header shown above the prompt. */
export function header(o: { rotor: string; mode: string; model: string }, color = true): string {
  const c = color ? C : passthrough;
  return c.dim(`rotor:`) + c.cyan(o.rotor) + c.dim(`  mode:`) + c.cyan(o.mode) + c.dim(`  model:`) + c.cyan(o.model);
}

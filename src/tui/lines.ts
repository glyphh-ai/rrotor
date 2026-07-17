/**
 * lines.ts — the transcript LINE MODEL. The viewport owns scrolling, so every
 * item flattens to styled lines: slicing a window of lines is exact (no layout
 * estimation), scrolling is array math, and ctrl+o re-expansion is retroactive
 * by construction — lines regenerate from items every render.
 */

import type { TurnStep } from "./types.js";

export type LineStyle =
  | "user"
  | "head"
  | "headErr"
  | "headSel"
  | "detail"
  | "hint"
  | "answer"
  | "note"
  | "blank"
  | "cardEdge"
  | "cardTitle"
  | "cardBody";

export interface Line {
  text: string;
  style: LineStyle;
}

const COLLAPSED_LINES = 3;

/** Greedy word-wrap into `width` columns with a hanging indent. */
export function wrap(text: string, width: number, indent: string): string[] {
  const out: string[] = [];
  for (const raw of text.split("\n")) {
    let line = "";
    for (const word of raw.split(/\s+/)) {
      const candidate = line ? `${line} ${word}` : word;
      if ([...candidate].length > width && line) {
        out.push(out.length === 0 ? line : indent + line);
        line = word;
      } else {
        line = candidate;
      }
    }
    out.push(out.length === 0 ? line : indent + line);
  }
  return out;
}

export function userLines(text: string, width: number): Line[] {
  const wrapped = wrap(`› ${text}`, width, "  ");
  return [{ text: "", style: "blank" }, ...wrapped.map((t) => ({ text: t, style: "user" as const }))];
}

export function answerLines(text: string, width: number): Line[] {
  const wrapped = wrap(`● ${text}`, width - 2, "  ");
  return [
    { text: "", style: "blank" },
    ...wrapped.map((t) => ({ text: `  ${t}`, style: "answer" as const })),
  ];
}

export function sectionLines(step: TurnStep, expanded: boolean, selected = false): Line[] {
  const meta: string[] = [];
  if (step.secs !== undefined) meta.push(`${step.secs.toFixed(1)}s`);
  if (step.tokens) meta.push(`${step.tokens.up}↑ ${step.tokens.down}↓`);
  if (step.error) meta.push(step.error);
  // Fold glyph: only sections with more to show get an affordance.
  const foldable = step.details.length > COLLAPSED_LINES;
  const fold = foldable ? (expanded ? "▾ " : "▸ ") : "⏺ ";
  const head = `${fold}${step.label}${meta.length ? ` · ${meta.join(" · ")}` : ""}`;
  const lines: Line[] = [{ text: head, style: selected ? "headSel" : step.ok ? "head" : "headErr" }];
  const body = expanded ? step.details : step.details.slice(0, COLLAPSED_LINES);
  for (const d of body) lines.push({ text: `  │ ${d}`, style: "detail" });
  const hidden = step.details.length - body.length;
  if (hidden > 0) lines.push({ text: `  │ … +${hidden} lines (enter unfolds · ctrl+o all)`, style: "hint" });
  return lines;
}

export function noteLines(text: string): Line[] {
  return [{ text: `  ${text}`, style: "note" }];
}

/** A boxed card: `╭─ Title ─…─╮` edge, body rows, closing edge. The title row
 *  carries the accent; body rows wrap to the card's inner width. */
export function cardLines(title: string, body: string[], width: number): Line[] {
  const inner = Math.max(20, Math.min(width, 100)) - 4;
  const top = `╭─ ${title} ${"─".repeat(Math.max(0, inner - title.length - 2))}╮`;
  const lines: Line[] = [{ text: "", style: "blank" }, { text: top, style: "cardTitle" }];
  for (const row of body) {
    for (const w of wrap(row, inner, "  ")) {
      lines.push({ text: `│ ${w}${" ".repeat(Math.max(0, inner - [...w].length + 1))}│`, style: "cardBody" });
    }
  }
  lines.push({ text: `╰${"─".repeat(inner + 2)}╯`, style: "cardEdge" });
  return lines;
}

/**
 * theme.ts — the rrotor TUI palette. Neutral grays + ONE cyan accent, chosen
 * for 256-color safety (no truecolor assumptions). Chrome is dim; content is
 * white; the accent marks identity, success, and focus — nothing else.
 */
export const theme = {
  /** Lime green — the highlight color (ANSI-256 safe: chalk maps to 82). */
  accent: "#5FFF00",
  white: "white",
  gray: "gray",
  dim: "gray",
  /** Box-drawing lines: borders, rules, gutters — always white. */
  line: "white",
} as const;

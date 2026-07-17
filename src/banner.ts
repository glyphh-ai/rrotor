/**
 * banner.ts — the rrotor identity, rendered in braille.
 *
 * rrotor: Recursive Reasoning on top of reasoning. The identity is generated,
 * not hand-drawn: a tiny pixel font + a rotor glyph are rasterized into braille
 * cells (2×4 dots per cell, U+2800 + bitmask), so the wordmark and the mark are
 * deterministic functions of their bitmaps — same spirit as the runtime.
 *
 * Variants (RROTOR_LOGO env or the caller's choice):
 *   full     — the rotor mark beside the big wordmark + tagline (default)
 *   wordmark — just the big braille wordmark
 *   mark     — just the rotor character
 *   min      — one plain text line
 */

const W = "\x1b[97m"; // bright white
const D = "\x1b[90m"; // dim
const R = "\x1b[0m"; // reset

// ── braille rasterizer ──────────────────────────────────────────────────────
// A braille cell covers 2 columns × 4 rows of pixels. Dot bit positions:
//   (x0,y0)=0x01 (x0,y1)=0x02 (x0,y2)=0x04 (x1,y0)=0x08
//   (x1,y1)=0x10 (x1,y2)=0x20 (x0,y3)=0x40 (x1,y3)=0x80
const DOT: number[][] = [
  [0x01, 0x08],
  [0x02, 0x10],
  [0x04, 0x20],
  [0x40, 0x80],
];

/** Rasterize a pixel bitmap ('#' = on) into rows of braille characters. */
export function braille(bitmap: string[]): string[] {
  const height = bitmap.length;
  const width = Math.max(...bitmap.map((r) => r.length));
  const on = (x: number, y: number): boolean => y < height && x < (bitmap[y]?.length ?? 0) && bitmap[y][x] === "#";
  const rows: string[] = [];
  for (let cy = 0; cy < Math.ceil(height / 4); cy++) {
    let row = "";
    for (let cx = 0; cx < Math.ceil(width / 2); cx++) {
      let mask = 0;
      for (let dy = 0; dy < 4; dy++) {
        for (let dx = 0; dx < 2; dx++) {
          if (on(cx * 2 + dx, cy * 4 + dy)) mask |= DOT[dy][dx];
        }
      }
      row += String.fromCharCode(0x2800 + mask);
    }
    rows.push(row);
  }
  return rows;
}

// ── the wordmark: full-resolution 16-row letterforms, bold 3px strokes ──────
// Lowercase, x-height rows 4–13; `t` keeps its ascender. Drawn at final size —
// no scaling — so every curve lands exactly where the braille dots want it.
const FONT: Record<string, string[]> = {
  r: [
    ".........",
    ".........",
    ".........",
    ".........",
    "###..####",
    "###.#####",
    "######...",
    "#####....",
    "####.....",
    "###......",
    "###......",
    "###......",
    "###......",
    "###......",
    ".........",
    ".........",
  ],
  o: [
    "..........",
    "..........",
    "..........",
    "..........",
    "..######..",
    ".########.",
    "###....###",
    "###....###",
    "###....###",
    "###....###",
    "###....###",
    "###....###",
    ".########.",
    "..######..",
    "..........",
    "..........",
  ],
  t: [
    ".###....",
    ".###....",
    ".###....",
    ".###....",
    "########",
    "########",
    ".###....",
    ".###....",
    ".###....",
    ".###....",
    ".###....",
    ".###....",
    ".###.###",
    "..#####.",
    "........",
    "........",
  ],
};

FONT["R"] = [
  "#########.",
  "##########",
  "###....###",
  "###....###",
  "###....###",
  "#########.",
  "########..",
  "###..###..",
  "###...###.",
  "###....###",
  "###....###",
  "###....###",
  "###....###",
  "###....###",
  "..........",
  "..........",
];

function wordBitmap(word: string): string[] {
  const rows = Array.from({ length: 16 }, () => "");
  for (const ch of word) {
    const glyph = FONT[ch] ?? Array.from({ length: 16 }, () => "....");
    for (let y = 0; y < 16; y++) rows[y] += (glyph[y] ?? "") + "..";
  }
  return rows;
}

/** Render any word the FONT covers as big braille rows. */
export function wordmark(word: string): string[] {
  return braille(wordBitmap(word));
}

/** The big braille wordmark rows — little r, big R: reasoning on top of Reasoning. */
export const WORDMARK: readonly string[] = wordmark("rRotor");

/** The compact monogram — `rR` in the same gradient, the TUI header mark. */
export const MARK: readonly string[] = wordmark("rR");

// ── the video-game gradient: one color per braille row, 256-color safe ──────
// The arcade chrome-logo look: hot yellow scanning down through orange and
// pink into purple. Hex for Ink; the raw-ANSI banner maps to the 256 codes.
export const GRADIENT: readonly string[] = ["#ffff5f", "#ffaf00", "#ff5f87", "#af87ff"];
const GRADIENT_256 = [227, 214, 204, 141];



export type LogoVariant = "full" | "wordmark" | "mark" | "min";

const TAGLINE = "Recursive Reasoning on top of reasoning";

/** A row colored by its position in the gradient (raw ANSI 256). */
function gradRow(row: string, i: number): string {
  const code = GRADIENT_256[Math.min(i, GRADIENT_256.length - 1)];
  return `\x1b[38;5;${code}m${row}${R}`;
}

/** A horizontal gradient rule sized to the wordmark — the scanline underline. */
function underline(width: number): string {
  const seg = Math.max(1, Math.ceil(width / GRADIENT_256.length));
  let out = "";
  for (let i = 0; i < GRADIENT_256.length; i++) {
    out += `\x1b[38;5;${GRADIENT_256[i]}m${"▔".repeat(Math.min(seg, Math.max(0, width - i * seg)))}`;
  }
  return out + R;
}

/** Compose the banner lines for a variant (uncolored callers can strip). */
export function bannerLines(version: string, variant: LogoVariant = "full"): string[] {
  const lines: string[] = [];
  if (variant === "min") {
    return [`  ${W}rRotor${R} ${D}v${version} · ${TAGLINE}${R}`, ""];
  }
  if (variant === "mark") {
    MARK.forEach((row, i) => lines.push(`  ${gradRow(row, i)}`));
    lines.push(`  ${W}rRotor${R} ${D}v${version}${R}`, "");
    return lines;
  }
  // wordmark + full: the big gradient wordmark; full adds the underline rule.
  WORDMARK.forEach((row, i) => lines.push(`  ${gradRow(row, i)}`));
  if (variant === "full") {
    const width = Math.max(...WORDMARK.map((r) => [...r].length));
    lines.push(`  ${underline(width)}`);
  }
  lines.push(`  ${D}${TAGLINE} · v${version}${R}`, "");
  return lines;
}

function variantFromEnv(): LogoVariant {
  const v = process.env.RROTOR_LOGO;
  return v === "wordmark" || v === "mark" || v === "min" || v === "full" ? v : "full";
}

export function printBanner(version: string): void {
  for (const ln of bannerLines(version, variantFromEnv())) console.log(ln);
}

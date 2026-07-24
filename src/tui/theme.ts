/**
 * theme.ts — the rrotor TUI palette registry. Each named theme reduces to the
 * four roles a terminal actually paints:
 *   text   — primary content (the answer, the mark, what you typed)
 *   muted  — secondary content (labels, receipts, the status line)
 *   faint  — chrome (borders, box edges, hints, gutters)
 *   accent — the ONE brand color (focus, the version, the active rotor, the ● bullet)
 * plus `danger` — the interrupt/stop signal (the Esc✕stop button).
 *
 * `theme` is a LIVE object: components read `theme.text` etc. at render, and
 * {@link applyTheme} swaps the values IN PLACE so a pick re-colors the whole tree
 * on the next render without threading a context through every component.
 *
 * The legacy role names (white/gray/dim/line) remain as ALIASES onto the four
 * roles, so components written before the re-skin keep working and inherit the
 * new palette: white→text, gray→muted, dim→faint, line→faint (chrome).
 */

/** The five semantic roles a theme defines. */
export interface CorePalette {
  text: string;
  muted: string;
  faint: string;
  accent: string;
  danger: string;
}

/** The full live palette: the five roles plus the legacy aliases. */
export interface Palette extends CorePalette {
  /** @deprecated alias of `text` */ white: string;
  /** @deprecated alias of `muted` */ gray: string;
  /** @deprecated alias of `faint` */ dim: string;
  /** @deprecated alias of `faint` (box edges/rules) */ line: string;
}

export interface ThemeDef {
  name: string;
  label: string;
  description: string;
  colors: CorePalette;
}

/** The available themes. `dark` is the default. Values mirror the Glyphh CLI /
 *  desktop palettes; Ink downsamples hex to the nearest 256 index on a
 *  256-color terminal and paints exactly on truecolor. */
export const THEMES: readonly ThemeDef[] = [
  {
    name: "dark",
    label: "Dark",
    description: "Bright text on ink — the default",
    colors: { text: "#ffffff", muted: "#c6c6c6", faint: "#6c6c6c", accent: "#af5fff", danger: "#ff5f5f" },
  },
  {
    name: "light",
    label: "Light",
    description: "Dark text for light terminals",
    colors: { text: "#1c1c1c", muted: "#4b5563", faint: "#9aa0a6", accent: "#7c3aed", danger: "#c0392b" },
  },
  {
    name: "highvis",
    label: "High Vis",
    description: "Maximum contrast — white on black, electric yellow",
    colors: { text: "#ffffff", muted: "#eaeaea", faint: "#b0b0b0", accent: "#ffd600", danger: "#ff3b30" },
  },
  {
    name: "claude",
    label: "Claude",
    description: "Warm cream + Claude coral",
    colors: { text: "#f5f0e8", muted: "#c9bfb0", faint: "#8a8175", accent: "#d97757", danger: "#e5484d" },
  },
  {
    name: "aurora",
    label: "Aurora",
    description: "Deep violet with a lilac accent",
    colors: { text: "#f1ecf8", muted: "#c4b6da", faint: "#6e6188", accent: "#ad5df8", danger: "#ff6b81" },
  },
];

export const DEFAULT_THEME = "dark";

/** The resolved theme for a saved name, falling back to the default. */
export function themeByName(name?: string): ThemeDef {
  return THEMES.find((t) => t.name === name) ?? THEMES[0]!;
}

/** Expand the five roles to the full palette (roles + legacy aliases). */
function expand(core: CorePalette): Palette {
  return { ...core, white: core.text, gray: core.muted, dim: core.faint, line: core.faint };
}

/** The live palette every component reads. Initialized to the default; mutated in
 *  place by {@link applyTheme} — the object identity is what keeps every
 *  `import { theme }` reference pointing at the current colors. */
export const theme: Palette = expand(THEMES[0]!.colors);

/** Swap the active palette in place and return the theme that won. */
export function applyTheme(name?: string): ThemeDef {
  const def = themeByName(name);
  Object.assign(theme, expand(def.colors));
  return def;
}

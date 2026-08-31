/**
 * The TUI's PURE render model — the palette registry (theme.ts) and the
 * transcript line model (lines.ts). Both are deterministic view-model logic (no
 * terminal I/O), so they are unit-testable in isolation from Ink; the .tsx
 * components and interactive entry (main.tsx) are the untestable readline-style
 * surface and stay out of coverage like shell.ts/repl.ts.
 */

import { describe, it, expect } from "vitest";

import {
  THEMES,
  DEFAULT_THEME,
  themeByName,
  applyTheme,
  theme,
  type ThemeDef,
} from "../../src/tui/theme.js";
import {
  wrap,
  userLines,
  answerLines,
  sectionLines,
  noteLines,
  cardLines,
} from "../../src/tui/lines.js";
import type { TurnStep } from "../../src/tui/types.js";

describe("theme registry", () => {
  it("ships the default plus the named themes, each with the five roles", () => {
    expect(DEFAULT_THEME).toBe("dark");
    expect(THEMES.map((t) => t.name)).toEqual(["dark", "light", "highvis", "claude", "aurora"]);
    for (const t of THEMES) {
      expect(t.colors).toEqual(
        expect.objectContaining({
          text: expect.any(String),
          muted: expect.any(String),
          faint: expect.any(String),
          accent: expect.any(String),
          danger: expect.any(String),
        }),
      );
    }
  });

  it("themeByName resolves a known name and falls back to the default", () => {
    expect(themeByName("aurora").name).toBe("aurora");
    expect(themeByName("does-not-exist").name).toBe("dark");
    expect(themeByName(undefined).name).toBe("dark");
  });

  it("applyTheme swaps the live palette in place and expands the legacy aliases", () => {
    const won: ThemeDef = applyTheme("light");
    expect(won.name).toBe("light");
    // Live object identity is preserved; values mutated in place.
    expect(theme.text).toBe(won.colors.text);
    expect(theme.accent).toBe(won.colors.accent);
    // Aliases: white→text, gray→muted, dim→faint, line→faint.
    expect(theme.white).toBe(won.colors.text);
    expect(theme.gray).toBe(won.colors.muted);
    expect(theme.dim).toBe(won.colors.faint);
    expect(theme.line).toBe(won.colors.faint);
    // Restore the default so cross-test state stays clean.
    applyTheme(DEFAULT_THEME);
    expect(theme.text).toBe(themeByName("dark").colors.text);
  });
});

describe("line model — wrap", () => {
  it("word-wraps past width with a hanging indent on continuation rows", () => {
    const out = wrap("one two three four five", 9, "  ");
    expect(out[0]).toBe("one two"); // first row: no indent
    expect(out.slice(1).every((l) => l.startsWith("  "))).toBe(true);
    // Every word survives the wrap.
    expect(out.join(" ").replace(/\s+/g, " ").trim()).toBe("one two three four five");
  });

  it("preserves explicit newlines as hard breaks", () => {
    expect(wrap("a\nb", 80, "  ")).toEqual(["a", "  b"]);
  });
});

describe("line model — item renderers", () => {
  it("userLines lead with a blank then the accent bar", () => {
    const out = userLines("hello", 80);
    expect(out[0]).toEqual({ text: "", style: "blank" });
    expect(out[1].style).toBe("user");
    expect(out[1].text).toContain("▌");
  });

  it("answerLines lead with a blank then the ● bullet, indented", () => {
    const out = answerLines("done", 80);
    expect(out[0]).toEqual({ text: "", style: "blank" });
    expect(out[1].style).toBe("answer");
    expect(out[1].text).toContain("●");
    expect(out[1].text.startsWith("  ")).toBe(true);
  });

  it("noteLines is a single indented note row", () => {
    expect(noteLines("fyi")).toEqual([{ text: "  fyi", style: "note" }]);
  });
});

describe("line model — sectionLines fold behaviour", () => {
  const base: TurnStep = { label: "search", ok: true, details: [], secs: 1.234, tokens: { up: 10, down: 20 } };

  it("renders meta (secs · tokens) and a solid bullet when not foldable", () => {
    const out = sectionLines({ ...base, details: ["a", "b"] }, false);
    expect(out[0].style).toBe("head");
    expect(out[0].text).toContain("⏺ search");
    expect(out[0].text).toContain("1.2s");
    expect(out[0].text).toContain("10↑ 20↓");
    // ≤ COLLAPSED_LINES details, all shown, no "+N lines" hint.
    expect(out.some((l) => l.style === "hint")).toBe(false);
    expect(out.filter((l) => l.style === "detail")).toHaveLength(2);
  });

  it("collapses to 3 detail rows with a hidden-count hint when folded", () => {
    const out = sectionLines({ ...base, details: ["1", "2", "3", "4", "5"] }, false);
    expect(out[0].text).toContain("▸ "); // foldable, collapsed glyph
    expect(out.filter((l) => l.style === "detail")).toHaveLength(3);
    const hint = out.find((l) => l.style === "hint");
    expect(hint?.text).toContain("+2 lines");
  });

  it("shows all rows and the open glyph when expanded", () => {
    const out = sectionLines({ ...base, details: ["1", "2", "3", "4", "5"] }, true);
    expect(out[0].text).toContain("▾ ");
    expect(out.filter((l) => l.style === "detail")).toHaveLength(5);
    expect(out.some((l) => l.style === "hint")).toBe(false);
  });

  it("marks a failed step headErr and folds the error into the meta", () => {
    const out = sectionLines({ label: "run", ok: false, details: [], error: "boom" }, false);
    expect(out[0].style).toBe("headErr");
    expect(out[0].text).toContain("boom");
  });

  it("honours the selected flag with the headSel style", () => {
    const out = sectionLines({ ...base, details: [] }, false, true);
    expect(out[0].style).toBe("headSel");
  });
});

describe("line model — cardLines", () => {
  it("boxes a titled card with an edge, wrapped body, and closing edge", () => {
    const out = cardLines("Title", ["body line"], 60);
    expect(out[0]).toEqual({ text: "", style: "blank" });
    expect(out[1].style).toBe("cardTitle");
    expect(out[1].text.startsWith("╭─ Title ")).toBe(true);
    expect(out.some((l) => l.style === "cardBody")).toBe(true);
    expect(out[out.length - 1].style).toBe("cardEdge");
    expect(out[out.length - 1].text.startsWith("╰")).toBe(true);
  });
});

/**
 * `text` pack — pure string tools dispatched directly through a connections
 * plugin. Every tool gets a happy path plus its important error/bound path; the
 * whole pack is pure so no workspace state matters, but we keep the mkdtemp
 * workspace convention of the stdlib suite anyway.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { BasicConnections } from "../../src/plugins/connections.js";
import { textPack } from "../../src/tools/text.js";

let root: string;
beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "rrotor-text-"));
});
afterAll(() => rmSync(root, { recursive: true, force: true }));

function conn() {
  const c = new BasicConnections();
  for (const t of textPack().tools) c.register(t.name, t.handler);
  return c;
}
const c = conn();
const call = async (name: string, args: Record<string, unknown>) => {
  const r = await c.dispatch(name, args);
  if (!r.ok) throw new Error(r.error);
  return r.result as Record<string, unknown>;
};
const fails = (name: string, args: Record<string, unknown>, re: RegExp) =>
  expect(c.dispatch(name, args)).resolves.toMatchObject({ ok: false, error: expect.stringMatching(re) });

describe("text pack — shape", () => {
  it("declares every tool pure with no grants", () => {
    const pack = textPack();
    expect(pack.name).toBe("text");
    expect(pack.version).toBe("1.0.0");
    expect(pack.tools.length).toBe(18);
    for (const t of pack.tools) {
      expect(t.effect).toBe("pure");
      expect(t.grants).toEqual([]);
      expect((t.input as { required?: string[] }).required?.length).toBeGreaterThan(0);
    }
  });
});

describe("text.head / text.tail / text.slice", () => {
  const text = ["l1", "l2", "l3", "l4", "l5"].join("\n");

  it("head returns the first N lines + total", async () => {
    expect(await call("text.head", { text, lines: 2 })).toEqual({ text: "l1\nl2", total_lines: 5 });
    expect((await call("text.head", { text })).text).toBe(text); // default 10 > 5
  });

  it("tail returns the last N lines + total", async () => {
    expect(await call("text.tail", { text, lines: 2 })).toEqual({ text: "l4\nl5", total_lines: 5 });
    expect((await call("text.tail", { text, lines: 0 })).text).toBe("");
  });

  it("slice is 1-indexed inclusive and validates the range", async () => {
    expect(await call("text.slice", { text, from_line: 2, to_line: 4 })).toEqual({ text: "l2\nl3\nl4", total_lines: 5 });
    await fails("text.slice", { text, from_line: 3, to_line: 1 }, /E_MISSING_INPUT/);
    await fails("text.slice", { text, from_line: 0, to_line: 2 }, /E_MISSING_INPUT/);
  });

  it("refuses a non-string text", async () => {
    await fails("text.head", { text: 42 }, /E_MISSING_INPUT/);
  });
});

describe("text.count", () => {
  it("counts chars, words, lines, bytes", async () => {
    expect(await call("text.count", { text: "one two\nthree" })).toEqual({ chars: 13, words: 3, lines: 2, bytes: 13 });
    expect(await call("text.count", { text: "" })).toEqual({ chars: 0, words: 0, lines: 0, bytes: 0 });
  });

  it("bytes counts UTF-8, not chars", async () => {
    expect(await call("text.count", { text: "héllo" })).toMatchObject({ chars: 5, bytes: 6 });
  });
});

describe("text.replace", () => {
  it("replaces globally by default and counts", async () => {
    expect(await call("text.replace", { text: "a b a", pattern: "a", replacement: "x" })).toEqual({ text: "x b x", replacements: 2 });
  });

  it("honors explicit non-global flags", async () => {
    expect(await call("text.replace", { text: "A a A", pattern: "a", replacement: "x", flags: "i" })).toEqual({ text: "x a A", replacements: 1 });
  });

  it("refuses an invalid regex with E_MISSING_INPUT", async () => {
    await fails("text.replace", { text: "x", pattern: "(", replacement: "y" }, /E_MISSING_INPUT/);
  });
});

describe("regex.extract", () => {
  it("extracts matches with groups and index", async () => {
    const r = await call("regex.extract", { text: "id=7 id=9", pattern: "id=(?<n>\\d+)" });
    expect(r.count).toBe(2);
    expect(r.truncated).toBe(false);
    expect(r.matches).toEqual([
      { match: "id=7", groups: { n: "7" }, index: 0 },
      { match: "id=9", groups: { n: "9" }, index: 5 },
    ]);
  });

  it("caps at 200 matches and reports truncated", async () => {
    const r = await call("regex.extract", { text: "a".repeat(500), pattern: "a" });
    expect(r.count).toBe(200);
    expect(r.truncated).toBe(true);
  });

  it("refuses an invalid regex", async () => {
    await fails("regex.extract", { text: "x", pattern: "[" }, /E_MISSING_INPUT/);
  });

  it("bounds a catastrophic-backtracking pattern instead of hanging", async () => {
    // (a+)+$ against a non-matching tail is the classic ReDoS bomb — must return
    // (as E_TIMEOUT) well within the worker deadline, not hang the process.
    const start = Date.now();
    await fails("regex.extract", { text: "a".repeat(40) + "!", pattern: "(a+)+$" }, /E_TIMEOUT/);
    expect(Date.now() - start).toBeLessThan(3000);
  }, 5000);
});

describe("text.split / text.join", () => {
  it("splits on a literal separator with an optional limit", async () => {
    expect(await call("text.split", { text: "a,b,c", separator: "," })).toEqual({ parts: ["a", "b", "c"], count: 3, truncated: false });
    expect(await call("text.split", { text: "a,b,c", separator: ",", limit: 2 })).toEqual({ parts: ["a", "b"], count: 2, truncated: true });
  });

  it("joins parts with a separator, and refuses a non-array", async () => {
    expect(await call("text.join", { parts: ["a", "b"], separator: "-" })).toEqual({ text: "a-b" });
    await fails("text.join", { parts: "nope", separator: "-" }, /E_MISSING_INPUT/);
  });
});

describe("text.case", () => {
  it("converts across all seven modes", async () => {
    const src = "helloWorld foo-bar";
    expect((await call("text.case", { text: src, mode: "upper" })).text).toBe("HELLOWORLD FOO-BAR");
    expect((await call("text.case", { text: src, mode: "lower" })).text).toBe("helloworld foo-bar");
    expect((await call("text.case", { text: "hello world", mode: "title" })).text).toBe("Hello World");
    expect((await call("text.case", { text: src, mode: "camel" })).text).toBe("helloWorldFooBar");
    expect((await call("text.case", { text: src, mode: "snake" })).text).toBe("hello_world_foo_bar");
    expect((await call("text.case", { text: src, mode: "kebab" })).text).toBe("hello-world-foo-bar");
    expect((await call("text.case", { text: src, mode: "constant" })).text).toBe("HELLO_WORLD_FOO_BAR");
  });

  it("refuses an unknown mode", async () => {
    await fails("text.case", { text: "x", mode: "spongebob" }, /E_MISSING_INPUT/);
  });
});

describe("text.dedent / text.indent / text.wrap", () => {
  it("dedent strips the common leading whitespace, ignoring blank lines", async () => {
    expect((await call("text.dedent", { text: "    a\n\n      b\n    c" })).text).toBe("a\n\n  b\nc");
    expect((await call("text.dedent", { text: "a\n  b" })).text).toBe("a\n  b"); // no common indent
  });

  it("indent takes a prefix or a width, skipping blank lines", async () => {
    expect((await call("text.indent", { text: "a\n\nb", prefix: "> " })).text).toBe("> a\n\n> b");
    expect((await call("text.indent", { text: "a\nb", width: 2 })).text).toBe("  a\n  b");
    await fails("text.indent", { text: "a" }, /E_MISSING_INPUT/);
  });

  it("wrap word-wraps to the width (default 80), keeping overlong words whole", async () => {
    expect((await call("text.wrap", { text: "one two three four", width: 9 })).text).toBe("one two\nthree\nfour");
    expect((await call("text.wrap", { text: "short line" })).text).toBe("short line");
    expect((await call("text.wrap", { text: "aaaaaaaaaa bb", width: 4 })).text).toBe("aaaaaaaaaa\nbb");
    await fails("text.wrap", { text: "x", width: 0 }, /E_MISSING_INPUT/);
  });
});

describe("text.sort", () => {
  it("sorts lines with unique/numeric/desc options", async () => {
    expect(await call("text.sort", { text: "b\na\nb" })).toEqual({ text: "a\nb\nb", lines: 3 });
    expect((await call("text.sort", { text: "b\na\nb", unique: true })).text).toBe("a\nb");
    expect((await call("text.sort", { text: "10\n2\n1", numeric: true })).text).toBe("1\n2\n10");
    expect((await call("text.sort", { text: "10\n2\n1", numeric: true, desc: true })).text).toBe("10\n2\n1");
  });
});

describe("diff.lines", () => {
  it("produces a unified diff with counts", async () => {
    const a = ["one", "two", "three", "four"].join("\n");
    const b = ["one", "2", "three", "four"].join("\n");
    const r = await call("diff.lines", { a, b });
    expect(r.additions).toBe(1);
    expect(r.deletions).toBe(1);
    expect(r.diff).toBe(["@@ -1,4 +1,4 @@", " one", "-two", "+2", " three", " four"].join("\n"));
  });

  it("returns an empty diff for identical inputs and honors context", async () => {
    expect(await call("diff.lines", { a: "same\nsame", b: "same\nsame" })).toEqual({ diff: "", additions: 0, deletions: 0 });
    const long = Array.from({ length: 9 }, (_, i) => `l${i}`);
    const changed = [...long];
    changed[4] = "CHANGED";
    const r = await call("diff.lines", { a: long.join("\n"), b: changed.join("\n"), context: 1 });
    expect(r.diff).toBe(["@@ -4,3 +4,3 @@", " l3", "-l4", "+CHANGED", " l5"].join("\n"));
  });

  it("refuses inputs above the 20k-line cap", async () => {
    await fails("diff.lines", { a: "x\n".repeat(20_001), b: "y" }, /E_MISSING_INPUT/);
  });
});

describe("text.template", () => {
  it("substitutes {{name}} vars and reports missing ones as \"\"", async () => {
    const r = await call("text.template", { template: "hi {{ name }}, {{missing}}!", vars: { name: "ada" } });
    expect(r.text).toBe("hi ada, !");
    expect(r.missing).toEqual(["missing"]);
  });

  it("refuses a non-object vars", async () => {
    await fails("text.template", { template: "x", vars: ["nope"] }, /E_MISSING_INPUT/);
  });
});

describe("text.chunk", () => {
  it("chunks by chars with overlap", async () => {
    expect(await call("text.chunk", { text: "abcdefgh", size: 4 })).toEqual({ chunks: ["abcd", "efgh"], count: 2, truncated: false });
    expect((await call("text.chunk", { text: "abcdef", size: 4, overlap: 2 })).chunks).toEqual(["abcd", "cdef", "ef"]);
  });

  it("caps at 500 chunks and validates size/overlap", async () => {
    const r = await call("text.chunk", { text: "x".repeat(1000), size: 1 });
    expect(r.count).toBe(500);
    expect(r.truncated).toBe(true);
    await fails("text.chunk", { text: "x", size: 0 }, /E_MISSING_INPUT/);
    await fails("text.chunk", { text: "x", size: 4, overlap: 4 }, /E_MISSING_INPUT/);
  });
});

describe("text.slug", () => {
  it("slugifies with diacritics stripped and edges trimmed", async () => {
    expect(await call("text.slug", { text: "  Héllo, Wörld!  " })).toEqual({ slug: "hello-world" });
    expect((await call("text.slug", { text: "--Already--Sluggy--" })).slug).toBe("already-sluggy");
  });
});

describe("text.similarity", () => {
  it("computes Levenshtein distance and a 0..1 ratio", async () => {
    expect(await call("text.similarity", { a: "kitten", b: "sitting" })).toEqual({ ratio: 1 - 3 / 7, distance: 3 });
    expect(await call("text.similarity", { a: "", b: "" })).toEqual({ ratio: 1, distance: 0 });
    expect(await call("text.similarity", { a: "abc", b: "xyz" })).toEqual({ ratio: 0, distance: 3 });
  });

  it("refuses inputs above the 10k-char cap", async () => {
    await fails("text.similarity", { a: "x".repeat(10_001), b: "y" }, /E_MISSING_INPUT/);
  });
});

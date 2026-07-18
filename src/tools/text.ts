/**
 * `text` tool pack — the pure string workbench: slicing, counting, regex
 * search/replace, case conversion, wrapping, sorting, diffing, templating,
 * chunking, slugs and similarity. Every tool here is a deterministic function of
 * its inputs (`effect: "pure"`, `grants: []`): no filesystem, no network, no
 * clock — which makes the whole pack recompute-safe on replay and installable
 * under every permission mode, including bare read-only chat.
 *
 * Outputs are BOUNDED on purpose: match lists, chunk lists and diff inputs are
 * capped and report `truncated` rather than flooding the tape with tokens.
 * Anything user-supplied that must parse (a regex pattern, flags) refuses with
 * `E_MISSING_INPUT` instead of leaking a raw SyntaxError.
 */

import { Worker } from "node:worker_threads";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

const MAX_MATCHES = 200; // regex.extract cap
const REGEX_TIMEOUT_MS = 500; // hard wall-clock bound on a single user-supplied regex run
const MAX_REGEX_TEXT = 1_000_000; // defense-in-depth input cap (matches sibling packs)

/**
 * Run a user-supplied regex against text in a WORKER with a hard timeout.
 * A catastrophic-backtracking pattern (e.g. `(a+)+$`) hangs the matching thread
 * indefinitely and no length cap prevents it — so the only real bound is to run
 * it off the main thread and terminate it on deadline (E_TIMEOUT). Deterministic:
 * same inputs → same result (or same timeout).
 */
function runRegexBounded(
  op: "extract" | "replace",
  args: { pattern: string; flags: string; text: string; replacement?: string; maxMatches?: number },
  tool: string,
): Promise<{ matches?: Array<{ match: string; groups: unknown; index: number }>; truncated?: boolean; text?: string; replacements?: number }> {
  const code = `
    const { parentPort, workerData } = require('node:worker_threads');
    const { op, pattern, flags, text, replacement, maxMatches } = workerData;
    try {
      const re = new RegExp(pattern, flags);
      if (op === 'extract') {
        const matches = []; let truncated = false;
        for (const m of text.matchAll(re)) {
          if (matches.length >= maxMatches) { truncated = true; break; }
          matches.push({ match: m[0], groups: m.groups ? { ...m.groups } : m.slice(1), index: m.index ?? 0 });
        }
        parentPort.postMessage({ ok: true, matches, truncated });
      } else {
        let replacements = 0;
        const out = text.replace(re, () => { replacements++; return replacement; });
        parentPort.postMessage({ ok: true, text: out, replacements });
      }
    } catch (e) { parentPort.postMessage({ ok: false, message: String(e && e.message || e) }); }
  `;
  return new Promise((resolvePromise, reject) => {
    const worker = new Worker(code, { eval: true, workerData: { op, ...args } });
    let settled = false;
    const done = (fn: () => void): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      void worker.terminate();
      fn();
    };
    const timer = setTimeout(
      () => done(() => reject(new RotorError("E_TIMEOUT", `${tool}: regex exceeded ${REGEX_TIMEOUT_MS}ms — likely catastrophic backtracking`, { context: { pattern: args.pattern } }))),
      REGEX_TIMEOUT_MS,
    );
    worker.on("message", (m: { ok: boolean; message?: string; matches?: Array<{ match: string; groups: unknown; index: number }>; truncated?: boolean; text?: string; replacements?: number }) =>
      done(() => (m.ok ? resolvePromise(m) : reject(new RotorError("E_MISSING_INPUT", `${tool}: invalid regex: ${m.message}`, { context: { pattern: args.pattern } })))),
    );
    worker.on("error", (e) => done(() => reject(new RotorError("E_TOOL", `${tool}: ${e.message}`, { context: { pattern: args.pattern }, cause: e }))));
  });
}
const MAX_CHUNKS = 500; // text.chunk cap
const MAX_PARTS = 10_000; // text.split cap
const MAX_DIFF_LINES = 20_000; // diff.lines per-side input cap
const MAX_SIM_CHARS = 10_000; // text.similarity per-side input cap
const MAX_DIFF_DP_CELLS = 4_000_000; // LCS DP budget after prefix/suffix strip

function reqStr(args: Row, name: string, tool: string): string {
  const v = args[name];
  if (typeof v !== "string") throw new RotorError("E_MISSING_INPUT", `${tool} requires a string \`${name}\``);
  return v;
}

function optInt(args: Row, name: string, tool: string, dflt: number): number {
  const v = args[name];
  if (v == null) return dflt;
  if (typeof v !== "number" || !Number.isFinite(v)) throw new RotorError("E_MISSING_INPUT", `${tool}: \`${name}\` must be a number`);
  return Math.trunc(v);
}

/** Compile a user-supplied regex; a bad pattern/flags is a caller error, not a crash. */
function compileRe(pattern: string, flags: string, tool: string): RegExp {
  try {
    return new RegExp(pattern, flags);
  } catch (e) {
    throw new RotorError("E_MISSING_INPUT", `${tool}: invalid regex: ${(e as Error).message}`, { context: { pattern, flags } });
  }
}

/** Split camelCase/PascalCase/delimited text into lowercase word tokens. */
function tokenize(text: string): string[] {
  return text
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .split(/[^A-Za-z0-9]+/)
    .filter((w) => w.length > 0)
    .map((w) => w.toLowerCase());
}

const cap = (w: string): string => (w.length ? w[0].toUpperCase() + w.slice(1) : w);

function convertCase(text: string, mode: string): string {
  switch (mode) {
    case "upper":
      return text.toUpperCase();
    case "lower":
      return text.toLowerCase();
    case "title":
      return text.replace(/[A-Za-z][A-Za-z0-9']*/g, (w) => cap(w.toLowerCase()));
    case "camel": {
      const t = tokenize(text);
      return t.length ? t[0] + t.slice(1).map(cap).join("") : "";
    }
    case "snake":
      return tokenize(text).join("_");
    case "kebab":
      return tokenize(text).join("-");
    case "constant":
      return tokenize(text).join("_").toUpperCase();
    default:
      throw new RotorError("E_MISSING_INPUT", `text.case: unknown mode \`${mode}\` (upper|lower|title|camel|snake|kebab|constant)`);
  }
}

/** Greedy word-wrap of one logical line; words longer than width stand alone. */
function wrapLine(line: string, width: number): string[] {
  const words = line.split(/\s+/).filter((w) => w.length > 0);
  if (words.length === 0) return [""];
  const out: string[] = [];
  let cur = "";
  for (const w of words) {
    if (cur === "") cur = w;
    else if (cur.length + 1 + w.length <= width) cur += " " + w;
    else {
      out.push(cur);
      cur = w;
    }
  }
  out.push(cur);
  return out;
}

type DiffOp = { tag: " " | "-" | "+"; line: string };

/** LCS-based line ops. Common prefix/suffix are stripped first so the O(n·m) DP
 *  only runs on the changed middle; if that middle still exceeds the DP budget,
 *  it degrades to a whole-block replace (still a valid unified diff). */
function diffOps(a: string[], b: string[]): DiffOp[] {
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }
  const midA = a.slice(start, endA);
  const midB = b.slice(start, endB);
  const ops: DiffOp[] = a.slice(0, start).map((line) => ({ tag: " " as const, line }));

  const n = midA.length;
  const m = midB.length;
  if (n * m > MAX_DIFF_DP_CELLS) {
    for (const line of midA) ops.push({ tag: "-", line });
    for (const line of midB) ops.push({ tag: "+", line });
  } else if (n > 0 || m > 0) {
    // Full LCS length table (row-major, (n+1)×(m+1)), then backtrack.
    const cols = m + 1;
    const dp = new Uint32Array((n + 1) * cols);
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i * cols + j] = midA[i] === midB[j] ? dp[(i + 1) * cols + j + 1] + 1 : Math.max(dp[(i + 1) * cols + j], dp[i * cols + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (midA[i] === midB[j]) {
        ops.push({ tag: " ", line: midA[i] });
        i++;
        j++;
      } else if (dp[(i + 1) * cols + j] >= dp[i * cols + j + 1]) {
        ops.push({ tag: "-", line: midA[i++] });
      } else {
        ops.push({ tag: "+", line: midB[j++] });
      }
    }
    while (i < n) ops.push({ tag: "-", line: midA[i++] });
    while (j < m) ops.push({ tag: "+", line: midB[j++] });
  }

  for (const line of a.slice(endA)) ops.push({ tag: " ", line });
  return ops;
}

/** Render ops as a unified diff body (hunk headers + prefixed lines). */
function unified(ops: DiffOp[], context: number): string {
  // Locate change indices; group into hunks whose context windows touch/overlap.
  const changes: number[] = [];
  for (let k = 0; k < ops.length; k++) if (ops[k].tag !== " ") changes.push(k);
  if (changes.length === 0) return "";

  const hunks: Array<{ from: number; to: number }> = [];
  for (const c of changes) {
    const from = Math.max(0, c - context);
    const to = Math.min(ops.length - 1, c + context);
    const last = hunks[hunks.length - 1];
    if (last && from <= last.to + 1) last.to = Math.max(last.to, to);
    else hunks.push({ from, to });
  }

  const out: string[] = [];
  let aLine = 1;
  let bLine = 1;
  let k = 0;
  for (const h of hunks) {
    for (; k < h.from; k++) {
      if (ops[k].tag !== "+") aLine++;
      if (ops[k].tag !== "-") bLine++;
    }
    const aStart = aLine;
    const bStart = bLine;
    let aCount = 0;
    let bCount = 0;
    const body: string[] = [];
    for (; k <= h.to; k++) {
      const op = ops[k];
      body.push(op.tag + op.line);
      if (op.tag !== "+") {
        aLine++;
        aCount++;
      }
      if (op.tag !== "-") {
        bLine++;
        bCount++;
      }
    }
    out.push(`@@ -${aStart},${aCount} +${bStart},${bCount} @@`, ...body);
  }
  return out.join("\n");
}

/** Levenshtein distance with two rolling rows (O(min) memory). */
function levenshtein(a: string, b: string): number {
  if (a === b) return 0;
  if (a.length === 0) return b.length;
  if (b.length === 0) return a.length;
  let prev = new Uint32Array(b.length + 1);
  let cur = new Uint32Array(b.length + 1);
  for (let j = 0; j <= b.length; j++) prev[j] = j;
  for (let i = 1; i <= a.length; i++) {
    cur[0] = i;
    const ca = a.charCodeAt(i - 1);
    for (let j = 1; j <= b.length; j++) {
      const cost = ca === b.charCodeAt(j - 1) ? 0 : 1;
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
    }
    [prev, cur] = [cur, prev];
  }
  return prev[b.length];
}

const textIn = { type: "object", properties: { text: { type: "string" } }, required: ["text"] };

export function textPack(): ToolPack {
  const tools: ToolSpec[] = [
    {
      name: "text.head",
      version: 1,
      description: "Return the first N lines of a text (default 10) plus the total line count.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { text: { type: "string" }, lines: { type: "number" } }, required: ["text"] },
      output: { type: "object", properties: { text: { type: "string" }, total_lines: { type: "number" } } },
      handler: (args: Row) => {
        const text = reqStr(args, "text", "text.head");
        const n = Math.max(0, optInt(args, "lines", "text.head", 10));
        const all = text.split("\n");
        return { text: all.slice(0, n).join("\n"), total_lines: all.length };
      },
    },
    {
      name: "text.tail",
      version: 1,
      description: "Return the last N lines of a text (default 10) plus the total line count.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { text: { type: "string" }, lines: { type: "number" } }, required: ["text"] },
      output: { type: "object", properties: { text: { type: "string" }, total_lines: { type: "number" } } },
      handler: (args: Row) => {
        const text = reqStr(args, "text", "text.tail");
        const n = Math.max(0, optInt(args, "lines", "text.tail", 10));
        const all = text.split("\n");
        return { text: n === 0 ? "" : all.slice(-n).join("\n"), total_lines: all.length };
      },
    },
    {
      name: "text.slice",
      version: 1,
      description: "Return lines from_line..to_line of a text (1-indexed, inclusive) plus the total line count.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { text: { type: "string" }, from_line: { type: "number" }, to_line: { type: "number" } },
        required: ["text", "from_line", "to_line"],
      },
      output: { type: "object", properties: { text: { type: "string" }, total_lines: { type: "number" } } },
      handler: (args: Row) => {
        const text = reqStr(args, "text", "text.slice");
        const from = optInt(args, "from_line", "text.slice", NaN);
        const to = optInt(args, "to_line", "text.slice", NaN);
        if (!Number.isFinite(from) || !Number.isFinite(to) || from < 1 || to < from) {
          throw new RotorError("E_MISSING_INPUT", "text.slice: from_line/to_line must satisfy 1 <= from_line <= to_line");
        }
        const all = text.split("\n");
        return { text: all.slice(from - 1, to).join("\n"), total_lines: all.length };
      },
    },
    {
      name: "text.count",
      version: 1,
      description: "Count characters, whitespace-separated words, lines, and UTF-8 bytes of a text.",
      effect: "pure",
      grants: [],
      input: textIn,
      output: {
        type: "object",
        properties: { chars: { type: "number" }, words: { type: "number" }, lines: { type: "number" }, bytes: { type: "number" } },
      },
      handler: (args: Row) => {
        const text = reqStr(args, "text", "text.count");
        const words = text.split(/\s+/).filter((w) => w.length > 0).length;
        return { chars: text.length, words, lines: text === "" ? 0 : text.split("\n").length, bytes: Buffer.byteLength(text, "utf8") };
      },
    },
    {
      name: "text.replace",
      version: 1,
      description: "Regex search-and-replace over a text (global by default). Returns the new text and replacement count.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: {
          text: { type: "string" },
          pattern: { type: "string" },
          replacement: { type: "string" },
          flags: { type: "string" },
        },
        required: ["text", "pattern", "replacement"],
      },
      output: { type: "object", properties: { text: { type: "string" }, replacements: { type: "number" } } },
      handler: async (args: Row) => {
        const text = reqStr(args, "text", "text.replace");
        const pattern = reqStr(args, "pattern", "text.replace");
        const replacement = reqStr(args, "replacement", "text.replace");
        const flags = args.flags == null ? "g" : String(args.flags);
        if (text.length > MAX_REGEX_TEXT) throw new RotorError("E_MISSING_INPUT", `text.replace: \`text\` exceeds ${MAX_REGEX_TEXT} chars`);
        compileRe(pattern, flags, "text.replace"); // fail fast on a syntactically bad pattern
        // `$&`-style tokens in `replacement` are treated literally — a simple, predictable contract.
        const r = await runRegexBounded("replace", { pattern, flags, text, replacement }, "text.replace");
        return { text: r.text, replacements: r.replacements };
      },
    },
    {
      name: "regex.extract",
      version: 1,
      description: "Extract all regex matches from a text as {match, groups, index} (capped at 200).",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { text: { type: "string" }, pattern: { type: "string" }, flags: { type: "string" } },
        required: ["text", "pattern"],
      },
      output: {
        type: "object",
        properties: { matches: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } },
      },
      handler: async (args: Row) => {
        const text = reqStr(args, "text", "regex.extract");
        const pattern = reqStr(args, "pattern", "regex.extract");
        let flags = args.flags == null ? "" : String(args.flags);
        if (!flags.includes("g")) flags += "g"; // matchAll requires the global flag
        if (text.length > MAX_REGEX_TEXT) throw new RotorError("E_MISSING_INPUT", `regex.extract: \`text\` exceeds ${MAX_REGEX_TEXT} chars`);
        compileRe(pattern, flags, "regex.extract"); // fail fast on a syntactically bad pattern
        const r = await runRegexBounded("extract", { pattern, flags, text, maxMatches: MAX_MATCHES }, "regex.extract");
        const matches = r.matches ?? [];
        return { matches, count: matches.length, truncated: r.truncated ?? false };
      },
    },
    {
      name: "text.split",
      version: 1,
      description: "Split a text on a literal separator (optional limit). Returns the parts (capped at 10000).",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { text: { type: "string" }, separator: { type: "string" }, limit: { type: "number" } },
        required: ["text", "separator"],
      },
      output: { type: "object", properties: { parts: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } } },
      handler: (args: Row) => {
        const text = reqStr(args, "text", "text.split");
        const separator = reqStr(args, "separator", "text.split");
        const limit = optInt(args, "limit", "text.split", MAX_PARTS);
        const all = text.split(separator);
        const max = Math.min(Math.max(0, limit), MAX_PARTS);
        return { parts: all.slice(0, max), count: Math.min(all.length, max), truncated: all.length > max };
      },
    },
    {
      name: "text.join",
      version: 1,
      description: "Join an array of string parts with a separator.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { parts: { type: "array", items: { type: "string" } }, separator: { type: "string" } },
        required: ["parts", "separator"],
      },
      output: { type: "object", properties: { text: { type: "string" } } },
      handler: (args: Row) => {
        if (!Array.isArray(args.parts)) throw new RotorError("E_MISSING_INPUT", "text.join requires an array `parts`");
        const separator = reqStr(args, "separator", "text.join");
        return { text: args.parts.map((p) => String(p)).join(separator) };
      },
    },
    {
      name: "text.case",
      version: 1,
      description: "Convert a text's case: upper, lower, title, camel, snake, kebab, or constant.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: {
          text: { type: "string" },
          mode: { type: "string", enum: ["upper", "lower", "title", "camel", "snake", "kebab", "constant"] },
        },
        required: ["text", "mode"],
      },
      output: { type: "object", properties: { text: { type: "string" } } },
      handler: (args: Row) => {
        const text = reqStr(args, "text", "text.case");
        const mode = reqStr(args, "mode", "text.case");
        return { text: convertCase(text, mode) };
      },
    },
    {
      name: "text.dedent",
      version: 1,
      description: "Strip the common leading whitespace shared by all non-empty lines.",
      effect: "pure",
      grants: [],
      input: textIn,
      output: { type: "object", properties: { text: { type: "string" } } },
      handler: (args: Row) => {
        const text = reqStr(args, "text", "text.dedent");
        const lines = text.split("\n");
        let common: string | undefined;
        for (const line of lines) {
          if (line.trim() === "") continue;
          const indent = line.slice(0, line.length - line.trimStart().length);
          if (common === undefined) common = indent;
          else {
            let k = 0;
            while (k < common.length && k < indent.length && common[k] === indent[k]) k++;
            common = common.slice(0, k);
          }
          if (common === "") break;
        }
        const width = common?.length ?? 0;
        return { text: width === 0 ? text : lines.map((l) => (l.trim() === "" ? l : l.slice(width))).join("\n") };
      },
    },
    {
      name: "text.indent",
      version: 1,
      description: "Indent every non-empty line with a `prefix` string or `width` spaces.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { text: { type: "string" }, prefix: { type: "string" }, width: { type: "number" } },
        required: ["text"],
      },
      output: { type: "object", properties: { text: { type: "string" } } },
      handler: (args: Row) => {
        const text = reqStr(args, "text", "text.indent");
        let prefix: string;
        if (typeof args.prefix === "string") prefix = args.prefix;
        else if (typeof args.width === "number" && Number.isFinite(args.width) && args.width >= 0) prefix = " ".repeat(Math.trunc(args.width));
        else throw new RotorError("E_MISSING_INPUT", "text.indent requires `prefix` (string) or `width` (non-negative number)");
        return { text: text.split("\n").map((l) => (l.trim() === "" ? l : prefix + l)).join("\n") };
      },
    },
    {
      name: "text.wrap",
      version: 1,
      description: "Greedy word-wrap each line of a text to a column width (default 80).",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { text: { type: "string" }, width: { type: "number" } }, required: ["text"] },
      output: { type: "object", properties: { text: { type: "string" } } },
      handler: (args: Row) => {
        const text = reqStr(args, "text", "text.wrap");
        const width = optInt(args, "width", "text.wrap", 80);
        if (width < 1) throw new RotorError("E_MISSING_INPUT", "text.wrap: `width` must be >= 1");
        return { text: text.split("\n").flatMap((l) => wrapLine(l, width)).join("\n") };
      },
    },
    {
      name: "text.sort",
      version: 1,
      description: "Sort a text's lines (options: unique, numeric, desc). Returns the sorted text and line count.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: {
          text: { type: "string" },
          unique: { type: "boolean" },
          numeric: { type: "boolean" },
          desc: { type: "boolean" },
        },
        required: ["text"],
      },
      output: { type: "object", properties: { text: { type: "string" }, lines: { type: "number" } } },
      handler: (args: Row) => {
        const text = reqStr(args, "text", "text.sort");
        let lines = text.split("\n");
        if (args.unique) lines = [...new Set(lines)];
        lines.sort((a, b) => {
          if (args.numeric) {
            const na = parseFloat(a);
            const nb = parseFloat(b);
            const va = Number.isNaN(na) ? -Infinity : na; // non-numeric lines sort first, like sort -n
            const vb = Number.isNaN(nb) ? -Infinity : nb;
            if (va !== vb) return va - vb;
          }
          return a < b ? -1 : a > b ? 1 : 0;
        });
        if (args.desc) lines.reverse();
        return { text: lines.join("\n"), lines: lines.length };
      },
    },
    {
      name: "diff.lines",
      version: 1,
      description: "Unified line diff of two texts (LCS, default 3 context lines). Returns diff, additions, deletions.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { a: { type: "string" }, b: { type: "string" }, context: { type: "number" } },
        required: ["a", "b"],
      },
      output: {
        type: "object",
        properties: { diff: { type: "string" }, additions: { type: "number" }, deletions: { type: "number" } },
      },
      handler: (args: Row) => {
        const a = reqStr(args, "a", "diff.lines").split("\n");
        const b = reqStr(args, "b", "diff.lines").split("\n");
        if (a.length > MAX_DIFF_LINES || b.length > MAX_DIFF_LINES) {
          throw new RotorError("E_MISSING_INPUT", `diff.lines: inputs are capped at ${MAX_DIFF_LINES} lines each`, {
            context: { a_lines: a.length, b_lines: b.length },
          });
        }
        const context = Math.max(0, optInt(args, "context", "diff.lines", 3));
        const ops = diffOps(a, b);
        let additions = 0;
        let deletions = 0;
        for (const op of ops) {
          if (op.tag === "+") additions++;
          else if (op.tag === "-") deletions++;
        }
        return { diff: unified(ops, context), additions, deletions };
      },
    },
    {
      name: "text.template",
      version: 1,
      description: "Substitute {{name}} placeholders from a vars object; missing vars become \"\" and are reported.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { template: { type: "string" }, vars: { type: "object" } },
        required: ["template", "vars"],
      },
      output: { type: "object", properties: { text: { type: "string" }, missing: { type: "array" } } },
      handler: (args: Row) => {
        const template = reqStr(args, "template", "text.template");
        if (args.vars == null || typeof args.vars !== "object" || Array.isArray(args.vars)) {
          throw new RotorError("E_MISSING_INPUT", "text.template requires an object `vars`");
        }
        const vars = args.vars as Record<string, unknown>;
        const missing = new Set<string>();
        const text = template.replace(/\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}/g, (_m, name: string) => {
          if (Object.prototype.hasOwnProperty.call(vars, name) && vars[name] != null) return String(vars[name]);
          missing.add(name);
          return "";
        });
        return { text, missing: [...missing] };
      },
    },
    {
      name: "text.chunk",
      version: 1,
      description: "Split a text into fixed-size character chunks with optional overlap (capped at 500 chunks).",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { text: { type: "string" }, size: { type: "number" }, overlap: { type: "number" } },
        required: ["text", "size"],
      },
      output: {
        type: "object",
        properties: { chunks: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } },
      },
      handler: (args: Row) => {
        const text = reqStr(args, "text", "text.chunk");
        const size = optInt(args, "size", "text.chunk", NaN);
        const overlap = optInt(args, "overlap", "text.chunk", 0);
        if (!Number.isFinite(size) || size < 1) throw new RotorError("E_MISSING_INPUT", "text.chunk: `size` must be >= 1");
        if (overlap < 0 || overlap >= size) throw new RotorError("E_MISSING_INPUT", "text.chunk: `overlap` must satisfy 0 <= overlap < size");
        const chunks: string[] = [];
        let truncated = false;
        for (let pos = 0; pos < text.length; pos += size - overlap) {
          if (chunks.length >= MAX_CHUNKS) {
            truncated = true;
            break;
          }
          chunks.push(text.slice(pos, pos + size));
        }
        return { chunks, count: chunks.length, truncated };
      },
    },
    {
      name: "text.slug",
      version: 1,
      description: "Turn a text into a URL-safe slug: lowercase ascii, hyphens, diacritics stripped.",
      effect: "pure",
      grants: [],
      input: textIn,
      output: { type: "object", properties: { slug: { type: "string" } } },
      handler: (args: Row) => {
        const text = reqStr(args, "text", "text.slug");
        const slug = text
          .normalize("NFKD")
          .replace(/[\u0300-\u036f]/g, "") // strip combining diacritics
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, "-")
          .replace(/^-+|-+$/g, "");
        return { slug };
      },
    },
    {
      name: "text.similarity",
      version: 1,
      description: "Levenshtein similarity of two strings: {ratio: 0..1, distance} (inputs capped at 10000 chars).",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { a: { type: "string" }, b: { type: "string" } }, required: ["a", "b"] },
      output: { type: "object", properties: { ratio: { type: "number" }, distance: { type: "number" } } },
      handler: (args: Row) => {
        const a = reqStr(args, "a", "text.similarity");
        const b = reqStr(args, "b", "text.similarity");
        if (a.length > MAX_SIM_CHARS || b.length > MAX_SIM_CHARS) {
          throw new RotorError("E_MISSING_INPUT", `text.similarity: inputs are capped at ${MAX_SIM_CHARS} chars each`, {
            context: { a_chars: a.length, b_chars: b.length },
          });
        }
        const distance = levenshtein(a, b);
        const max = Math.max(a.length, b.length);
        return { ratio: max === 0 ? 1 : 1 - distance / max, distance };
      },
    },
  ];

  return { name: "text", version: "1.0.0", tools };
}

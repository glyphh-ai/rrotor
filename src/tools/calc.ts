/**
 * `calc` tool pack — deterministic math + time: a safe expression evaluator,
 * descriptive statistics, unit conversion, wall-clock reading, time parsing /
 * formatting / arithmetic, cron schedule projection, and duration parsing.
 *
 * Everything here is a PURE function of its inputs (grants: []) except
 * `time.now`, which reads the wall clock — external state, so it is classified
 * `reading` and its output is recorded + replayed by the runtime rather than
 * recomputed. All date math is done in UTC via hand-rolled parsers: no
 * `Date.parse` local-time ambiguity, no `eval`/`Function`, no locale
 * dependence — the same inputs give the same answer on every host.
 */

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

// ---------------------------------------------------------------------------
// calc.eval — recursive-descent expression parser (no eval/Function, ever)
// ---------------------------------------------------------------------------

const MAX_EXPR_LEN = 5000;

type Tok = { kind: "num"; value: number } | { kind: "ident"; name: string } | { kind: "op"; op: string };

const FUNCS: Record<string, { arity: [number, number]; fn: (...xs: number[]) => number }> = {
  sqrt: { arity: [1, 1], fn: Math.sqrt },
  abs: { arity: [1, 1], fn: Math.abs },
  round: { arity: [1, 1], fn: Math.round },
  floor: { arity: [1, 1], fn: Math.floor },
  ceil: { arity: [1, 1], fn: Math.ceil },
  min: { arity: [1, 32], fn: Math.min },
  max: { arity: [1, 32], fn: Math.max },
  pow: { arity: [2, 2], fn: Math.pow },
  log: { arity: [1, 1], fn: Math.log },
  log10: { arity: [1, 1], fn: Math.log10 },
  exp: { arity: [1, 1], fn: Math.exp },
  sin: { arity: [1, 1], fn: Math.sin },
  cos: { arity: [1, 1], fn: Math.cos },
  tan: { arity: [1, 1], fn: Math.tan },
};

function tokenize(src: string): Tok[] {
  const toks: Tok[] = [];
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === " " || c === "\t" || c === "\n" || c === "\r") {
      i++;
      continue;
    }
    if (/[0-9.]/.test(c)) {
      const m = /^\d*\.?\d+(?:[eE][+-]?\d+)?/.exec(src.slice(i));
      if (!m) throw new RotorError("E_MISSING_INPUT", `calc.eval: bad number at position ${i}`);
      toks.push({ kind: "num", value: Number(m[0]) });
      i += m[0].length;
      continue;
    }
    if (/[a-zA-Z_]/.test(c)) {
      const m = /^[a-zA-Z_][a-zA-Z0-9_]*/.exec(src.slice(i))!;
      toks.push({ kind: "ident", name: m[0] });
      i += m[0].length;
      continue;
    }
    if ("+-*/%^(),".includes(c)) {
      toks.push({ kind: "op", op: c });
      i++;
      continue;
    }
    throw new RotorError("E_MISSING_INPUT", `calc.eval: unexpected character \`${c}\` at position ${i}`);
  }
  return toks;
}

function evaluate(expr: string, vars: Record<string, number>): number {
  const toks = tokenize(expr);
  let pos = 0;
  const peek = (): Tok | undefined => toks[pos];
  const isOp = (op: string): boolean => {
    const t = toks[pos];
    return t?.kind === "op" && t.op === op;
  };

  // expr := add | add := mul (('+'|'-') mul)* | mul := unary (('*'|'/'|'%') unary)*
  // unary := '-' unary | pow | pow := primary ('^' unary)?   (right-assoc)
  const parseAdd = (): number => {
    let v = parseMul();
    while (isOp("+") || isOp("-")) {
      const op = (toks[pos++] as { op: string }).op;
      const r = parseMul();
      v = op === "+" ? v + r : v - r;
    }
    return v;
  };
  const parseMul = (): number => {
    let v = parseUnary();
    while (isOp("*") || isOp("/") || isOp("%")) {
      const op = (toks[pos++] as { op: string }).op;
      const r = parseUnary();
      v = op === "*" ? v * r : op === "/" ? v / r : v % r;
    }
    return v;
  };
  const parseUnary = (): number => {
    if (isOp("-")) {
      pos++;
      return -parseUnary();
    }
    return parsePow();
  };
  const parsePow = (): number => {
    const base = parsePrimary();
    if (isOp("^")) {
      pos++;
      return Math.pow(base, parseUnary()); // recurse into unary → right-assoc, `2^-3` works
    }
    return base;
  };
  const parsePrimary = (): number => {
    const t = peek();
    if (!t) throw new RotorError("E_MISSING_INPUT", "calc.eval: unexpected end of expression");
    if (t.kind === "num") {
      pos++;
      return t.value;
    }
    if (t.kind === "op" && t.op === "(") {
      pos++;
      const v = parseAdd();
      if (!isOp(")")) throw new RotorError("E_MISSING_INPUT", "calc.eval: missing `)`");
      pos++;
      return v;
    }
    if (t.kind === "ident") {
      pos++;
      if (isOp("(")) {
        const fn = FUNCS[t.name];
        if (!fn) throw new RotorError("E_MISSING_INPUT", `calc.eval: unknown function \`${t.name}\``, { context: { identifier: t.name } });
        pos++;
        const args: number[] = [];
        if (!isOp(")")) {
          args.push(parseAdd());
          while (isOp(",")) {
            pos++;
            args.push(parseAdd());
          }
        }
        if (!isOp(")")) throw new RotorError("E_MISSING_INPUT", `calc.eval: missing \`)\` after ${t.name}(...)`);
        pos++;
        if (args.length < fn.arity[0] || args.length > fn.arity[1]) {
          throw new RotorError("E_MISSING_INPUT", `calc.eval: ${t.name} takes ${fn.arity[0]}${fn.arity[1] > fn.arity[0] ? `..${fn.arity[1]}` : ""} args, got ${args.length}`);
        }
        return fn.fn(...args);
      }
      if (!(t.name in vars)) {
        throw new RotorError("E_MISSING_INPUT", `calc.eval: unknown identifier \`${t.name}\` — pass it in \`vars\``, { context: { identifier: t.name } });
      }
      return vars[t.name];
    }
    throw new RotorError("E_MISSING_INPUT", `calc.eval: unexpected token \`${(t as { op: string }).op}\``);
  };

  const value = parseAdd();
  if (pos !== toks.length) throw new RotorError("E_MISSING_INPUT", "calc.eval: trailing tokens after expression");
  return value;
}

// ---------------------------------------------------------------------------
// convert.unit — linear categories + temperature affine special case
// ---------------------------------------------------------------------------

const LINEAR_UNITS: Record<string, Record<string, number>> = {
  length: { mm: 0.001, cm: 0.01, m: 1, km: 1000, in: 0.0254, ft: 0.3048, yd: 0.9144, mi: 1609.344 },
  mass: { mg: 0.001, g: 1, kg: 1000, lb: 453.59237, oz: 28.349523125 },
  data: { B: 1, KB: 1e3, MB: 1e6, GB: 1e9, TB: 1e12, KiB: 1024, MiB: 1024 ** 2, GiB: 1024 ** 3 },
  time: { ms: 1, s: 1000, min: 60_000, h: 3_600_000, d: 86_400_000 },
};
const TEMP_UNITS = new Set(["C", "F", "K"]);

function unitCategory(unit: string): string | undefined {
  if (TEMP_UNITS.has(unit)) return "temperature";
  for (const [cat, table] of Object.entries(LINEAR_UNITS)) if (unit in table) return cat;
  return undefined;
}

function convertTemp(value: number, from: string, to: string): number {
  const k = from === "C" ? value + 273.15 : from === "F" ? (value - 32) * (5 / 9) + 273.15 : value;
  return to === "C" ? k - 273.15 : to === "F" ? (k - 273.15) * (9 / 5) + 32 : k;
}

// ---------------------------------------------------------------------------
// time — hand-rolled UTC parsing/formatting (no Date.parse local-time drift)
// ---------------------------------------------------------------------------

const ISO_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?)?(Z|[+-]\d{2}:?\d{2})?$/;

/** Parse a "+HH:mm" / "-HHmm" / "Z" offset to minutes; undefined for bad input. */
function offsetMinutes(zone: string): number | undefined {
  if (zone === "Z" || zone === "z") return 0;
  const m = /^([+-])(\d{2}):?(\d{2})$/.exec(zone);
  if (!m) return undefined;
  const min = Number(m[2]) * 60 + Number(m[3]);
  return m[1] === "-" ? -min : min;
}

/** Parse epoch-ms number, numeric string, or ISO-ish text → epoch ms (UTC unless offset given). */
function parseWhen(input: unknown, tz?: unknown, tool = "time"): number {
  if (typeof input === "number") {
    if (!Number.isFinite(input)) throw new RotorError("E_MISSING_INPUT", `${tool}: epoch_ms must be a finite number`);
    return input;
  }
  if (typeof input === "string") {
    const t = input.trim();
    if (/^-?\d+$/.test(t)) return Number(t); // epoch milliseconds
    const m = ISO_RE.exec(t);
    if (m) {
      const [, y, mo, d, h, mi, s, frac, zone] = m;
      const month = Number(mo);
      const day = Number(d);
      if (month < 1 || month > 12 || day < 1 || day > 31 || Number(h ?? 0) > 23 || Number(mi ?? 0) > 59 || Number(s ?? 0) > 59) {
        throw new RotorError("E_MISSING_INPUT", `${tool}: out-of-range date component in \`${t}\``, { context: { text: t } });
      }
      const ms = Date.UTC(Number(y), month - 1, day, Number(h ?? 0), Number(mi ?? 0), Number(s ?? 0), frac ? Number(frac.padEnd(3, "0")) : 0);
      const zoneStr = zone ?? (typeof tz === "string" && tz !== "" ? tz : undefined);
      if (zoneStr === undefined) return ms;
      const off = offsetMinutes(zoneStr);
      if (off === undefined) throw new RotorError("E_MISSING_INPUT", `${tool}: bad tz offset \`${zoneStr}\` (want "+HH:mm" or "Z")`);
      return ms - off * 60_000;
    }
  }
  throw new RotorError("E_MISSING_INPUT", `${tool}: unparseable time \`${String(input)}\` — want ISO-8601, "YYYY-MM-DD[ HH:mm[:ss]]", or epoch ms`, {
    context: { input: String(input).slice(0, 100) },
  });
}

const pad = (n: number, w = 2): string => String(n).padStart(w, "0");

function formatPattern(ms: number, pattern: string): string {
  const d = new Date(ms);
  const map: Record<string, string> = {
    YYYY: pad(d.getUTCFullYear(), 4),
    MM: pad(d.getUTCMonth() + 1),
    DD: pad(d.getUTCDate()),
    HH: pad(d.getUTCHours()),
    mm: pad(d.getUTCMinutes()),
    ss: pad(d.getUTCSeconds()),
  };
  return pattern.replace(/YYYY|MM|DD|HH|mm|ss/g, (t) => map[t]);
}

const TIME_UNIT_MS: Record<string, number> = { ms: 1, s: 1000, min: 60_000, h: 3_600_000, d: 86_400_000, w: 604_800_000 };

// ---------------------------------------------------------------------------
// cron.next — 5-field vixie subset (* , - / steps), evaluated in UTC
// ---------------------------------------------------------------------------

interface CronField {
  set: Set<number>;
  restricted: boolean; // whether the field was anything other than bare "*"
}

function parseCronField(spec: string, lo: number, hi: number, name: string): CronField {
  const set = new Set<number>();
  if (spec === "*") {
    for (let v = lo; v <= hi; v++) set.add(v);
    return { set, restricted: false };
  }
  for (const part of spec.split(",")) {
    const slash = part.split("/");
    if (slash.length > 2 || slash[0] === "") throw new RotorError("E_MISSING_INPUT", `cron.next: bad ${name} field \`${part}\``);
    const step = slash.length === 2 ? Number(slash[1]) : 1;
    if (!Number.isInteger(step) || step < 1) throw new RotorError("E_MISSING_INPUT", `cron.next: bad step in ${name} field \`${part}\``);
    let a: number;
    let b: number;
    const range = slash[0];
    if (range === "*") {
      a = lo;
      b = hi;
    } else if (range.includes("-")) {
      const [x, y] = range.split("-");
      a = Number(x);
      b = Number(y);
    } else {
      a = Number(range);
      b = slash.length === 2 ? hi : a; // "a/step" means a..max by step, per vixie
    }
    if (!Number.isInteger(a) || !Number.isInteger(b) || a < lo || b > hi || a > b) {
      throw new RotorError("E_MISSING_INPUT", `cron.next: ${name} value out of range in \`${part}\` (${lo}-${hi})`);
    }
    for (let v = a; v <= b; v += step) set.add(v);
  }
  return { set, restricted: true };
}

function cronNext(expr: string, fromMs: number, count: number): string[] {
  const fields = expr.trim().split(/\s+/);
  if (fields.length !== 5) {
    throw new RotorError("E_MISSING_INPUT", `cron.next: expected 5 fields (minute hour dom month dow), got ${fields.length}`, { context: { expr } });
  }
  const minute = parseCronField(fields[0], 0, 59, "minute");
  const hour = parseCronField(fields[1], 0, 23, "hour");
  const dom = parseCronField(fields[2], 1, 31, "dom");
  const month = parseCronField(fields[3], 1, 12, "month");
  const dow = parseCronField(fields[4], 0, 7, "dow");
  if (dow.set.has(7)) dow.set.add(0); // 7 is an alias for Sunday

  const out: string[] = [];
  let t = Math.floor(fromMs / 60_000) * 60_000 + 60_000; // first whole minute strictly after `from`
  const horizon = fromMs + 4 * 366 * 86_400_000; // impossible schedules (e.g. Feb 30) must terminate
  let guard = 0;
  while (out.length < count) {
    if (t > horizon || guard++ > 500_000) {
      throw new RotorError("E_TOOL", "cron.next: no matching time within 4 years of `from`", { context: { expr } });
    }
    const d = new Date(t);
    const [y, mo, day] = [d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()];
    if (!month.set.has(mo + 1)) {
      t = Date.UTC(y, mo + 1, 1);
      continue;
    }
    // Vixie semantics: when BOTH dom and dow are restricted, the day matches if EITHER does.
    const domOk = dom.set.has(day);
    const dowOk = dow.set.has(d.getUTCDay());
    const dayOk = dom.restricted && dow.restricted ? domOk || dowOk : dom.restricted ? domOk : dow.restricted ? dowOk : true;
    if (!dayOk) {
      t = Date.UTC(y, mo, day + 1);
      continue;
    }
    if (!hour.set.has(d.getUTCHours())) {
      t = Date.UTC(y, mo, day, d.getUTCHours() + 1);
      continue;
    }
    if (!minute.set.has(d.getUTCMinutes())) {
      t += 60_000;
      continue;
    }
    out.push(new Date(t).toISOString());
    t += 60_000;
  }
  return out;
}

// ---------------------------------------------------------------------------
// duration.parse
// ---------------------------------------------------------------------------

const DUR_UNIT_MS: Record<string, number> = { ms: 1, s: 1000, m: 60_000, min: 60_000, h: 3_600_000, d: 86_400_000, w: 604_800_000 };
const DUR_RE = /(\d+(?:\.\d+)?)(ms|min|m|s|h|d|w)/g; // "ms"/"min" alternatives must precede "m"/"s"

function parseDuration(text: string): number {
  const compact = text.replace(/\s+/g, "");
  if (compact === "") throw new RotorError("E_MISSING_INPUT", "duration.parse requires a non-empty `text`");
  let ms = 0;
  let consumed = 0;
  DUR_RE.lastIndex = 0;
  for (const m of compact.matchAll(DUR_RE)) {
    if (m.index !== consumed) break; // gap → junk between components
    ms += Number(m[1]) * DUR_UNIT_MS[m[2]];
    consumed = m.index + m[0].length;
  }
  if (consumed !== compact.length) {
    throw new RotorError("E_MISSING_INPUT", `duration.parse: unparseable duration \`${text}\` — want e.g. "1h30m", "90s", "2d4h"`, {
      context: { text: text.slice(0, 100) },
    });
  }
  return ms;
}

function humanDuration(ms: number): string {
  if (ms === 0) return "0s";
  const parts: string[] = [];
  let rest = Math.floor(ms);
  for (const [label, size] of [
    ["d", 86_400_000],
    ["h", 3_600_000],
    ["m", 60_000],
    ["s", 1000],
    ["ms", 1],
  ] as Array<[string, number]>) {
    const n = Math.floor(rest / size);
    if (n > 0) parts.push(`${n}${label}`);
    rest -= n * size;
  }
  return parts.join(" ");
}

// ---------------------------------------------------------------------------
// shared arg helpers
// ---------------------------------------------------------------------------

function requireFiniteResult(value: number, tool: string): number {
  if (!Number.isFinite(value)) {
    throw new RotorError("E_TOOL", `${tool}: result is not a finite number`, { context: { value: String(value) } });
  }
  return value;
}

function numberArray(x: unknown, tool: string): number[] {
  if (!Array.isArray(x) || x.length === 0) throw new RotorError("E_MISSING_INPUT", `${tool} requires a non-empty \`values\` array`);
  if (x.length > 100_000) throw new RotorError("E_MISSING_INPUT", `${tool}: \`values\` is capped at 100000 entries`, { context: { count: x.length } });
  for (const v of x) {
    if (typeof v !== "number" || !Number.isFinite(v)) throw new RotorError("E_MISSING_INPUT", `${tool}: every value must be a finite number`);
  }
  return x as number[];
}

/** Linear-interpolated percentile over a SORTED array, q in [0,1]. */
function percentile(sorted: number[], q: number): number {
  const idx = q * (sorted.length - 1);
  const lo = Math.floor(idx);
  const frac = idx - lo;
  return lo + 1 < sorted.length ? sorted[lo] + frac * (sorted[lo + 1] - sorted[lo]) : sorted[lo];
}

// ---------------------------------------------------------------------------
// the pack
// ---------------------------------------------------------------------------

export function calcPack(): ToolPack {
  const tools: ToolSpec[] = [
    {
      name: "calc.eval",
      version: 1,
      description:
        "Safely evaluate a math expression (+ - * / % ^, parens, unary minus, vars, and sqrt/abs/round/floor/ceil/min/max/pow/log/log10/exp/sin/cos/tan). Returns {value}.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: {
          expression: { type: "string", description: "The expression, e.g. `2 * (x + 1) ^ 2`" },
          vars: { type: "object", additionalProperties: { type: "number" }, description: "Variable bindings referenced by name" },
        },
        required: ["expression"],
      },
      output: { type: "object", properties: { value: { type: "number" } } },
      handler: (args: Row) => {
        const expr = String(args.expression ?? "").trim();
        if (!expr) throw new RotorError("E_MISSING_INPUT", "calc.eval requires an `expression`");
        if (expr.length > MAX_EXPR_LEN) throw new RotorError("E_MISSING_INPUT", `calc.eval: expression exceeds ${MAX_EXPR_LEN} chars`);
        const vars: Record<string, number> = {};
        if (args.vars != null) {
          if (typeof args.vars !== "object" || Array.isArray(args.vars)) throw new RotorError("E_MISSING_INPUT", "calc.eval: `vars` must be an object of numbers");
          for (const [k, v] of Object.entries(args.vars as Record<string, unknown>)) {
            if (typeof v !== "number" || !Number.isFinite(v)) throw new RotorError("E_MISSING_INPUT", `calc.eval: var \`${k}\` must be a finite number`);
            vars[k] = v;
          }
        }
        return { value: requireFiniteResult(evaluate(expr, vars), "calc.eval") };
      },
    },
    {
      name: "stats.describe",
      version: 1,
      description: "Descriptive statistics for a number array: count, sum, mean, median, population std, min, max, p25/p75/p90 (interpolated).",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { values: { type: "array", items: { type: "number" }, minItems: 1 } },
        required: ["values"],
      },
      output: {
        type: "object",
        properties: {
          count: { type: "number" },
          sum: { type: "number" },
          mean: { type: "number" },
          median: { type: "number" },
          std: { type: "number" },
          min: { type: "number" },
          max: { type: "number" },
          p25: { type: "number" },
          p75: { type: "number" },
          p90: { type: "number" },
        },
      },
      handler: (args: Row) => {
        const values = numberArray(args.values, "stats.describe");
        const sorted = [...values].sort((a, b) => a - b);
        const n = sorted.length;
        const sum = sorted.reduce((a, b) => a + b, 0);
        const mean = sum / n;
        const std = Math.sqrt(sorted.reduce((a, b) => a + (b - mean) ** 2, 0) / n);
        return {
          count: n,
          sum,
          mean,
          median: percentile(sorted, 0.5),
          std,
          min: sorted[0],
          max: sorted[n - 1],
          p25: percentile(sorted, 0.25),
          p75: percentile(sorted, 0.75),
          p90: percentile(sorted, 0.9),
        };
      },
    },
    {
      name: "convert.unit",
      version: 1,
      description:
        "Convert a value between units within one category: length (mm cm m km in ft yd mi), mass (mg g kg lb oz), temperature (C F K), data (B KB MB GB TB KiB MiB GiB), time (ms s min h d).",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { value: { type: "number" }, from: { type: "string" }, to: { type: "string" } },
        required: ["value", "from", "to"],
      },
      output: { type: "object", properties: { value: { type: "number" }, from: { type: "string" }, to: { type: "string" } } },
      handler: (args: Row) => {
        const value = args.value;
        if (typeof value !== "number" || !Number.isFinite(value)) throw new RotorError("E_MISSING_INPUT", "convert.unit requires a finite numeric `value`");
        const from = String(args.from ?? "");
        const to = String(args.to ?? "");
        const catFrom = unitCategory(from);
        const catTo = unitCategory(to);
        if (!catFrom) throw new RotorError("E_MISSING_INPUT", `convert.unit: unknown unit \`${from}\``, { context: { unit: from } });
        if (!catTo) throw new RotorError("E_MISSING_INPUT", `convert.unit: unknown unit \`${to}\``, { context: { unit: to } });
        if (catFrom !== catTo) {
          throw new RotorError("E_MISSING_INPUT", `convert.unit: cannot convert ${catFrom} (\`${from}\`) to ${catTo} (\`${to}\`)`, {
            context: { from, to, from_category: catFrom, to_category: catTo },
          });
        }
        const result = catFrom === "temperature" ? convertTemp(value, from, to) : (value * LINEAR_UNITS[catFrom][from]) / LINEAR_UNITS[catFrom][to];
        return { value: requireFiniteResult(result, "convert.unit"), from, to };
      },
    },
    {
      name: "time.now",
      version: 1,
      description: "Read the current wall-clock time. Returns {iso, epoch_ms, tz} (tz is the host IANA zone name).",
      // Wall clock is external state: recorded on the tape and replayed, never recomputed.
      effect: "reading",
      grants: [],
      input: { type: "object", properties: {} },
      output: { type: "object", properties: { iso: { type: "string" }, epoch_ms: { type: "number" }, tz: { type: "string" } } },
      handler: () => {
        const now = Date.now();
        return { iso: new Date(now).toISOString(), epoch_ms: now, tz: Intl.DateTimeFormat().resolvedOptions().timeZone };
      },
    },
    {
      name: "time.parse",
      version: 1,
      description: 'Parse ISO-8601, "YYYY-MM-DD[ HH:mm[:ss]]", or epoch-ms text to {iso, epoch_ms}. UTC unless the text (or `tz` offset like "+02:00") says otherwise.',
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { text: { type: "string" }, tz: { type: "string", description: 'Offset ("+HH:mm"/"-HH:mm"/"Z") applied when the text has no zone' } },
        required: ["text"],
      },
      output: { type: "object", properties: { iso: { type: "string" }, epoch_ms: { type: "number" } } },
      handler: (args: Row) => {
        if (typeof args.text !== "string" || args.text.trim() === "") throw new RotorError("E_MISSING_INPUT", "time.parse requires a `text`");
        const ms = parseWhen(args.text, args.tz, "time.parse");
        return { iso: new Date(ms).toISOString(), epoch_ms: ms };
      },
    },
    {
      name: "time.format",
      version: 1,
      description: "Format a time (epoch_ms or ISO) with pattern tokens YYYY MM DD HH mm ss. UTC unless a `tz` offset is given. Returns {text}.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: {
          time: { description: "Epoch milliseconds (number) or an ISO-8601 string" },
          pattern: { type: "string" },
          tz: { type: "string", description: 'Offset like "+05:30" to format in' },
        },
        required: ["time", "pattern"],
      },
      output: { type: "object", properties: { text: { type: "string" } } },
      handler: (args: Row) => {
        const pattern = String(args.pattern ?? "");
        if (!pattern) throw new RotorError("E_MISSING_INPUT", "time.format requires a `pattern`");
        if (pattern.length > 200) throw new RotorError("E_MISSING_INPUT", "time.format: `pattern` is capped at 200 chars");
        let ms = parseWhen(args.time, undefined, "time.format");
        if (typeof args.tz === "string" && args.tz !== "") {
          const off = offsetMinutes(args.tz);
          if (off === undefined) throw new RotorError("E_MISSING_INPUT", `time.format: bad tz offset \`${args.tz}\``);
          ms += off * 60_000; // shift, then read with UTC getters = local time at that offset
        }
        return { text: formatPattern(ms, pattern) };
      },
    },
    {
      name: "time.add",
      version: 1,
      description: "Add (or subtract, with a negative amount) a duration to a time. Units: ms s min h d w. Returns {iso, epoch_ms}.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: {
          time: { description: "Epoch milliseconds (number) or an ISO-8601 string" },
          amount: { type: "number" },
          unit: { type: "string", enum: Object.keys(TIME_UNIT_MS) },
        },
        required: ["time", "amount", "unit"],
      },
      output: { type: "object", properties: { iso: { type: "string" }, epoch_ms: { type: "number" } } },
      handler: (args: Row) => {
        const ms = parseWhen(args.time, undefined, "time.add");
        if (typeof args.amount !== "number" || !Number.isFinite(args.amount)) throw new RotorError("E_MISSING_INPUT", "time.add requires a finite numeric `amount`");
        const unit = String(args.unit ?? "");
        const size = DUR_UNIT_MS[unit];
        if (!size) throw new RotorError("E_MISSING_INPUT", `time.add: unknown unit \`${unit}\` (want ms|s|min|h|d|w)`, { context: { unit } });
        const result = ms + args.amount * size;
        return { iso: new Date(result).toISOString(), epoch_ms: result };
      },
    },
    {
      name: "time.diff",
      version: 1,
      description: "Signed difference b − a between two times (epoch_ms or ISO) as {ms, seconds, minutes, hours, days}.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { a: { description: "Epoch ms or ISO string" }, b: { description: "Epoch ms or ISO string" } },
        required: ["a", "b"],
      },
      output: {
        type: "object",
        properties: { ms: { type: "number" }, seconds: { type: "number" }, minutes: { type: "number" }, hours: { type: "number" }, days: { type: "number" } },
      },
      handler: (args: Row) => {
        const ms = parseWhen(args.b, undefined, "time.diff") - parseWhen(args.a, undefined, "time.diff");
        return { ms, seconds: ms / 1000, minutes: ms / 60_000, hours: ms / 3_600_000, days: ms / 86_400_000 };
      },
    },
    {
      name: "cron.next",
      version: 1,
      description:
        "Project the next occurrences (UTC) of a 5-field cron expression (minute hour dom month dow; * , - / steps). Pass `from` for deterministic replay; count caps at 10.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: {
          expr: { type: "string" },
          from: { description: "Epoch ms or ISO start; occurrences are strictly after it. Defaults to now (pass it to stay deterministic)." },
          count: { type: "number", minimum: 1, maximum: 10 },
        },
        required: ["expr"],
      },
      output: { type: "object", properties: { next: { type: "array", items: { type: "string" } }, count: { type: "number" } } },
      handler: (args: Row) => {
        const expr = String(args.expr ?? "").trim();
        if (!expr) throw new RotorError("E_MISSING_INPUT", "cron.next requires an `expr`");
        // `from` omitted falls back to the wall clock — pass it explicitly to keep the call pure.
        const fromMs = args.from == null ? Date.now() : parseWhen(args.from, undefined, "cron.next");
        const raw = Number(args.count ?? 1);
        const count = Math.min(10, Math.max(1, Number.isFinite(raw) ? Math.floor(raw) : 1));
        const next = cronNext(expr, fromMs, count);
        return { next, count: next.length };
      },
    },
    {
      name: "duration.parse",
      version: 1,
      description: 'Parse a compact duration like "1h30m", "90s", "2d4h" (units ms s m/min h d w) to {ms, seconds, human}.',
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
      output: { type: "object", properties: { ms: { type: "number" }, seconds: { type: "number" }, human: { type: "string" } } },
      handler: (args: Row) => {
        if (typeof args.text !== "string") throw new RotorError("E_MISSING_INPUT", "duration.parse requires a `text`");
        const ms = parseDuration(args.text);
        return { ms, seconds: ms / 1000, human: humanDuration(ms) };
      },
    },
  ];

  return { name: "calc", version: "1.0.0", tools };
}

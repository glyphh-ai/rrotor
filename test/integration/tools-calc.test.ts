/**
 * `calc` pack — math + time. Every tool gets a happy path and its important
 * error/bound path, dispatched directly through a BasicConnections registry
 * against a throwaway mkdtemp workspace (the pack itself never touches disk).
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { BasicConnections } from "../../src/plugins/connections.js";
import { calcPack } from "../../src/tools/calc.js";

let root: string;
let c: BasicConnections;
beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "glyphh-calc-"));
  c = new BasicConnections();
  for (const t of calcPack().tools) c.register(t.name, t.handler);
});
afterAll(() => rmSync(root, { recursive: true, force: true }));

const call = async (name: string, args: Record<string, unknown>) => {
  const r = await c.dispatch(name, args);
  if (!r.ok) throw new Error(r.error);
  return r.result as Record<string, unknown>;
};
const fails = async (name: string, args: Record<string, unknown>, code = "E_MISSING_INPUT") => {
  const r = await c.dispatch(name, args);
  expect(r).toMatchObject({ ok: false, error: expect.stringMatching(code) });
};

describe("calc.eval", () => {
  it("evaluates precedence, right-assoc ^, unary minus, vars, and functions", async () => {
    expect((await call("calc.eval", { expression: "2 + 3 * 4" })).value).toBe(14);
    expect((await call("calc.eval", { expression: "2^3^2" })).value).toBe(512); // right-assoc
    expect((await call("calc.eval", { expression: "-2^2" })).value).toBe(-4); // unary binds looser than ^
    expect((await call("calc.eval", { expression: "2^-1" })).value).toBe(0.5);
    expect((await call("calc.eval", { expression: "10 % 3" })).value).toBe(1);
    expect((await call("calc.eval", { expression: "(1 + 2) * 3" })).value).toBe(9);
    expect((await call("calc.eval", { expression: "x * 2 + min(3, 1, 8)" , vars: { x: 5 } })).value).toBe(11);
    expect((await call("calc.eval", { expression: "sqrt(9) + abs(-2) + floor(1.9) + ceil(0.1) + round(2.5)" })).value).toBe(10);
    expect((await call("calc.eval", { expression: "pow(2, 10) + max(1, 20)" })).value).toBe(1044);
    expect((await call("calc.eval", { expression: "log(exp(1))" })).value).toBeCloseTo(1);
    expect((await call("calc.eval", { expression: "log10(1000)" })).value).toBeCloseTo(3);
    expect((await call("calc.eval", { expression: "sin(0) + cos(0) + tan(0)" })).value).toBe(1);
    expect((await call("calc.eval", { expression: "1.5e2 / 3" })).value).toBe(50);
  });

  it("rejects unknown identifiers, bad syntax, non-finite results, and oversized input", async () => {
    await fails("calc.eval", { expression: "nope + 1" }); // unknown identifier
    await fails("calc.eval", { expression: "shout(1)" }); // unknown function
    await fails("calc.eval", { expression: "1 + " }); // dangling operator
    await fails("calc.eval", { expression: "(1 + 2" }); // unbalanced paren
    await fails("calc.eval", { expression: "1 2" }); // trailing tokens
    await fails("calc.eval", { expression: "min()" }); // arity
    await fails("calc.eval", { expression: "" });
    await fails("calc.eval", { expression: "x", vars: { x: "str" } });
    await fails("calc.eval", { expression: "1/0", vars: {} }, "E_TOOL"); // non-finite result
    await fails("calc.eval", { expression: "1+".repeat(3000) + "1" }); // length cap
  });
});

describe("stats.describe", () => {
  it("computes count/sum/mean/median/std/min/max and interpolated percentiles", async () => {
    const r = await call("stats.describe", { values: [9, 2, 4, 4, 4, 5, 5, 7] });
    expect(r.count).toBe(8);
    expect(r.sum).toBe(40);
    expect(r.mean).toBe(5);
    expect(r.median).toBe(4.5);
    expect(r.std).toBe(2); // population std
    expect(r.min).toBe(2);
    expect(r.max).toBe(9);
    expect(r.p25).toBe(4);
    expect(r.p75).toBe(5.5);
    expect(r.p90).toBeCloseTo(7.6);
  });

  it("single-element and error paths", async () => {
    const one = await call("stats.describe", { values: [42] });
    expect(one).toMatchObject({ count: 1, mean: 42, median: 42, std: 0, p25: 42, p90: 42 });
    await fails("stats.describe", { values: [] });
    await fails("stats.describe", { values: "nope" });
    await fails("stats.describe", { values: [1, "2"] });
    await fails("stats.describe", { values: [1, NaN] });
  });
});

describe("convert.unit", () => {
  it("converts within length, mass, temperature, data, and time", async () => {
    expect((await call("convert.unit", { value: 1, from: "km", to: "m" })).value).toBe(1000);
    expect((await call("convert.unit", { value: 12, from: "in", to: "ft" })).value).toBeCloseTo(1);
    expect((await call("convert.unit", { value: 1, from: "mi", to: "km" })).value).toBeCloseTo(1.609344);
    expect((await call("convert.unit", { value: 1, from: "lb", to: "oz" })).value).toBeCloseTo(16);
    expect((await call("convert.unit", { value: 32, from: "F", to: "C" })).value).toBeCloseTo(0);
    expect((await call("convert.unit", { value: 0, from: "C", to: "K" })).value).toBeCloseTo(273.15);
    expect((await call("convert.unit", { value: 100, from: "C", to: "F" })).value).toBeCloseTo(212);
    expect((await call("convert.unit", { value: 1, from: "GiB", to: "MB" })).value).toBeCloseTo(1073.741824);
    expect((await call("convert.unit", { value: 1, from: "TB", to: "GB" })).value).toBe(1000);
    expect((await call("convert.unit", { value: 90, from: "min", to: "h" })).value).toBe(1.5);
    const echo = await call("convert.unit", { value: 2, from: "d", to: "s" });
    expect(echo).toMatchObject({ value: 172800, from: "d", to: "s" });
  });

  it("refuses unknown units and cross-category conversion", async () => {
    await fails("convert.unit", { value: 1, from: "furlong", to: "m" });
    await fails("convert.unit", { value: 1, from: "kg", to: "m" }); // cross-category
    await fails("convert.unit", { value: 1, from: "C", to: "GB" }); // cross-category
    await fails("convert.unit", { value: "one", from: "m", to: "km" });
  });
});

describe("time.now", () => {
  it("returns a coherent iso/epoch_ms/tz snapshot", async () => {
    const before = Date.now();
    const r = await call("time.now", {});
    const after = Date.now();
    expect(r.epoch_ms).toBeGreaterThanOrEqual(before);
    expect(r.epoch_ms).toBeLessThanOrEqual(after);
    expect(r.iso).toBe(new Date(r.epoch_ms as number).toISOString());
    expect(typeof r.tz).toBe("string");
    expect(r.tz).toBeTruthy();
  });
});

describe("time.parse", () => {
  it("parses ISO, space-separated, epoch, and tz-offset forms — all UTC unless offset given", async () => {
    expect((await call("time.parse", { text: "2024-03-05" })).epoch_ms).toBe(Date.UTC(2024, 2, 5));
    expect((await call("time.parse", { text: "2024-03-05 12:30" })).epoch_ms).toBe(Date.UTC(2024, 2, 5, 12, 30));
    expect((await call("time.parse", { text: "2024-03-05T12:30:15Z" })).epoch_ms).toBe(Date.UTC(2024, 2, 5, 12, 30, 15));
    expect((await call("time.parse", { text: "2024-03-05T12:00:00+02:00" })).epoch_ms).toBe(Date.UTC(2024, 2, 5, 10));
    // tz applies only when the text carries no zone of its own.
    expect((await call("time.parse", { text: "2024-03-05 12:00", tz: "+02:00" })).epoch_ms).toBe(Date.UTC(2024, 2, 5, 10));
    expect((await call("time.parse", { text: "2024-03-05T12:00:00Z", tz: "+02:00" })).epoch_ms).toBe(Date.UTC(2024, 2, 5, 12));
    expect((await call("time.parse", { text: "1700000000000" })).epoch_ms).toBe(1700000000000);
    expect((await call("time.parse", { text: "2024-03-05" })).iso).toBe("2024-03-05T00:00:00.000Z");
  });

  it("rejects garbage, out-of-range components, and bad offsets", async () => {
    await fails("time.parse", { text: "next tuesday" });
    await fails("time.parse", { text: "2024-13-05" });
    await fails("time.parse", { text: "2024-03-05 25:00" });
    await fails("time.parse", { text: "2024-03-05 12:00", tz: "+9:00" });
    await fails("time.parse", { text: "" });
  });
});

describe("time.format", () => {
  it("formats epoch or ISO input with YYYY MM DD HH mm ss tokens, UTC by default", async () => {
    const t = Date.UTC(2024, 2, 5, 9, 7, 3);
    expect((await call("time.format", { time: t, pattern: "YYYY-MM-DD HH:mm:ss" })).text).toBe("2024-03-05 09:07:03");
    expect((await call("time.format", { time: "2024-03-05T09:07:03Z", pattern: "DD/MM/YYYY" })).text).toBe("05/03/2024");
    // A tz offset shifts the rendered wall time.
    expect((await call("time.format", { time: t, pattern: "HH:mm", tz: "+05:30" })).text).toBe("14:37");
    expect((await call("time.format", { time: t, pattern: "HH:mm", tz: "-02:00" })).text).toBe("07:07");
  });

  it("rejects a missing pattern, bad time, and bad offset", async () => {
    await fails("time.format", { time: 0, pattern: "" });
    await fails("time.format", { time: "yesterday", pattern: "YYYY" });
    await fails("time.format", { time: 0, pattern: "HH", tz: "UTC+2" });
    await fails("time.format", { time: 0, pattern: "Y".repeat(500) }); // pattern cap
  });
});

describe("time.add", () => {
  it("adds and subtracts durations across units", async () => {
    const r = await call("time.add", { time: "2024-03-05T12:00:00Z", amount: 90, unit: "min" });
    expect(r.iso).toBe("2024-03-05T13:30:00.000Z");
    expect(r.epoch_ms).toBe(Date.UTC(2024, 2, 5, 13, 30));
    expect((await call("time.add", { time: 0, amount: 2, unit: "d" })).iso).toBe("1970-01-03T00:00:00.000Z");
    expect((await call("time.add", { time: "2024-03-05", amount: -1, unit: "h" })).iso).toBe("2024-03-04T23:00:00.000Z");
  });

  it("rejects unknown units and non-numeric amounts", async () => {
    await fails("time.add", { time: 0, amount: 1, unit: "fortnight" });
    await fails("time.add", { time: 0, amount: "two", unit: "h" });
    await fails("time.add", { time: "junk", amount: 1, unit: "h" });
  });
});

describe("time.diff", () => {
  it("returns the signed b − a difference in every unit", async () => {
    const r = await call("time.diff", { a: "2024-03-05T00:00:00Z", b: "2024-03-06T12:00:00Z" });
    expect(r).toMatchObject({ ms: 129600000, seconds: 129600, minutes: 2160, hours: 36, days: 1.5 });
    expect((await call("time.diff", { a: 1000, b: 0 })).ms).toBe(-1000); // signed
  });

  it("rejects unparseable endpoints", async () => {
    await fails("time.diff", { a: "soon", b: 0 });
  });
});

describe("cron.next", () => {
  it("projects step, range, and weekday schedules in UTC, strictly after `from`", async () => {
    const r = await call("cron.next", { expr: "*/15 * * * *", from: "2024-01-01T00:07:00Z", count: 3 });
    expect(r.next).toEqual(["2024-01-01T00:15:00.000Z", "2024-01-01T00:30:00.000Z", "2024-01-01T00:45:00.000Z"]);
    expect(r.count).toBe(3);
    // 2024-01-01 was a Monday; an occurrence exactly at `from` is excluded.
    expect((await call("cron.next", { expr: "0 9 * * 1", from: "2024-01-01T00:00:00Z" })).next).toEqual(["2024-01-01T09:00:00.000Z"]);
    expect((await call("cron.next", { expr: "0 9 * * 1", from: "2024-01-01T09:00:00Z" })).next).toEqual(["2024-01-08T09:00:00.000Z"]);
    expect((await call("cron.next", { expr: "30 6 1 * *", from: "2024-01-15T00:00:00Z", count: 2 })).next).toEqual([
      "2024-02-01T06:30:00.000Z",
      "2024-03-01T06:30:00.000Z",
    ]);
    // Restricted dom AND dow use vixie OR semantics: Monday Jan 8 comes before the 1st of Feb.
    expect((await call("cron.next", { expr: "0 0 1 * 1", from: "2024-01-02T00:00:00Z" })).next).toEqual(["2024-01-08T00:00:00.000Z"]);
    // Ranges + lists, and dow 7 as Sunday.
    expect((await call("cron.next", { expr: "0 8-10,18 * * 7", from: "2024-01-01T00:00:00Z", count: 2 })).next).toEqual([
      "2024-01-07T08:00:00.000Z",
      "2024-01-07T09:00:00.000Z",
    ]);
  });

  it("caps count at 10 and refuses bad or impossible expressions", async () => {
    const capped = await call("cron.next", { expr: "* * * * *", from: "2024-01-01T00:00:00Z", count: 50 });
    expect((capped.next as string[]).length).toBe(10);
    expect(capped.count).toBe(10);
    await fails("cron.next", { expr: "* * * *" }); // 4 fields
    await fails("cron.next", { expr: "61 * * * *", from: 0 }); // out of range
    await fails("cron.next", { expr: "*/0 * * * *", from: 0 }); // bad step
    await fails("cron.next", { expr: "" });
    await fails("cron.next", { expr: "0 0 30 2 *", from: "2024-01-01T00:00:00Z" }, "E_TOOL"); // Feb 30 never comes
  });
});

describe("duration.parse", () => {
  it("parses compound durations and renders a human string", async () => {
    expect(await call("duration.parse", { text: "1h30m" })).toMatchObject({ ms: 5400000, seconds: 5400, human: "1h 30m" });
    expect((await call("duration.parse", { text: "90s" })).ms).toBe(90000);
    expect((await call("duration.parse", { text: "2d4h" })).ms).toBe(187200000);
    expect((await call("duration.parse", { text: "1w" })).ms).toBe(604800000);
    expect((await call("duration.parse", { text: "500ms" })).human).toBe("500ms");
    expect((await call("duration.parse", { text: "2min" })).ms).toBe(120000);
    expect((await call("duration.parse", { text: " 1h 30m " })).ms).toBe(5400000); // whitespace tolerated
    expect((await call("duration.parse", { text: "0s" })).human).toBe("0s");
  });

  it("rejects unitless numbers and junk", async () => {
    await fails("duration.parse", { text: "90" });
    await fails("duration.parse", { text: "soonish" });
    await fails("duration.parse", { text: "1h30x" });
    await fails("duration.parse", { text: "" });
  });
});

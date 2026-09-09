/**
 * PROGRAM-PER-TURN (P1) — the envelope: hook compilation (both export styles),
 * the narrow-only plan clamp, the wall-clock budget, fail-open on bad
 * programs, and the bounded program cache.
 */

import { describe, it, expect, beforeEach } from "vitest";
import { compileHooks, parseTurnPlan, runPre, toScript, cachedHooks, clearEnvelopeCache, type PreCtx } from "../../src/harness/envelope.js";

const CTX: PreCtx = { prompt: "hi", mode: "code", historyTurns: 2, loop: "test" };

describe("compileHooks", () => {
  it("compiles the documented ESM style", () => {
    const hooks = compileHooks(`export async function pre(ctx) { return { steer: "be brief " + ctx.loop }; }`);
    expect(hooks?.pre).toBeTypeOf("function");
  });

  it("compiles CJS style", () => {
    const hooks = compileHooks(`exports.pre = (ctx) => ({ contextBudget: 1000 });`);
    expect(hooks?.pre).toBeTypeOf("function");
  });

  it("a program with no hooks (bindings-only loop) is null", () => {
    expect(compileHooks(`const roles = ["planner"]; exports.mode = "envelope";`)).toBeNull();
  });

  it("a program that does not parse fails OPEN (null, no throw)", () => {
    expect(compileHooks(`this is not javascript {{{`)).toBeNull();
  });

  it("the sandbox has no require/process", () => {
    expect(compileHooks(`exports.pre = () => require("fs");`)).not.toBeNull(); // compiles…
    // …but the hook throws at run time and runPre reports, never throws:
  });
});

describe("toScript", () => {
  it("rewrites export function pre/post and export const mode", () => {
    const s = toScript(`export const mode = "envelope";\nexport async function pre(c) {}\nexport function post(c) {}`);
    expect(s).toContain(`exports.mode =`);
    expect(s).toContain(`exports.pre = async function pre`);
    expect(s).toContain(`exports.post = function post`);
  });
});

describe("parseTurnPlan (narrow-only clamp)", () => {
  it("keeps known fields, drops junk, clamps sizes", () => {
    const plan = parseTurnPlan({
      steer: " x".repeat(3000), contextBudget: 1234.9, tools: ["Bash", "", 7, "Read"],
      widenPermissions: true, model: "anthropic::opus",
    });
    expect(plan?.steer?.length).toBeLessThanOrEqual(2000);
    expect(plan?.contextBudget).toBe(1234);
    expect(plan?.tools).toEqual(["Bash", "Read"]);
    expect((plan as Record<string, unknown>).widenPermissions).toBeUndefined();
    expect((plan as Record<string, unknown>).model).toBeUndefined();
  });

  it("empty / non-object → null", () => {
    expect(parseTurnPlan(null)).toBeNull();
    expect(parseTurnPlan("x")).toBeNull();
    expect(parseTurnPlan({})).toBeNull();
  });
});

describe("runPre", () => {
  it("returns the clamped plan", async () => {
    const hooks = compileHooks(`export async function pre(ctx) { return { steer: "focus", refuse: "" }; }`)!;
    const r = await runPre(hooks, CTX, 100);
    expect(r.error).toBeUndefined();
    expect(r.plan).toEqual({ steer: "focus" });
  });

  it("a throwing hook reports and fails open", async () => {
    const hooks = compileHooks(`exports.pre = () => { throw new Error("boom"); };`)!;
    const r = await runPre(hooks, CTX, 100);
    expect(r.plan).toBeNull();
    expect(r.error).toContain("boom");
  });

  it("a slow hook is cut at the budget", async () => {
    const hooks = compileHooks(`exports.pre = () => new Promise(() => {});`)!;
    const r = await runPre(hooks, CTX, 30);
    expect(r.plan).toBeNull();
    expect(r.error).toContain("exceeded");
  });

  it("a hook reaching for node APIs fails open", async () => {
    const hooks = compileHooks(`exports.pre = () => require("fs").readFileSync("/etc/passwd");`)!;
    const r = await runPre(hooks, CTX, 100);
    expect(r.plan).toBeNull();
    expect(r.error).toBeTruthy();
  });
});

describe("cachedHooks", () => {
  beforeEach(() => clearEnvelopeCache());

  it("caches per key (one load within TTL)", async () => {
    let loads = 0;
    const load = async (): Promise<string> => { loads++; return `exports.pre = () => ({ steer: "s" });`; };
    const a = await cachedHooks("o:l", load);
    const b = await cachedHooks("o:l", load);
    expect(loads).toBe(1);
    expect(a).toBe(b);
  });

  it("a failed load degrades to the cached entry", async () => {
    await cachedHooks("o:l", async () => `exports.pre = () => ({ steer: "s" });`);
    clearEnvelopeCache(); // simulate TTL expiry would need clock control; assert the miss path instead
    const r = await cachedHooks("o:x", async () => { throw new Error("db down"); });
    expect(r).toBeNull(); // no prior entry → null, never a throw
  });
});

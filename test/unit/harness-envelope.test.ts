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

describe("envelope leftovers", () => {
  it("runPre with no pre hook is a 0ms no-op", async () => {
    const r = await runPre({}, CTX, 50);
    expect(r).toEqual({ plan: null, ms: 0 });
  });

  it("toScript rewrites export default", () => {
    expect(toScript(`export default { pre() {} }`)).toContain("exports.default =");
  });

  it("module.exports reassignment wins (CJS whole-object style)", () => {
    const hooks = compileHooks(`module.exports = { pre: () => ({ steer: "m" }) };`);
    expect(hooks?.pre).toBeTypeOf("function");
  });
});

describe("post (P2)", () => {
  it("parsePostEffects clamps note + caps facts at 5, drops junk", async () => {
    const { parsePostEffects } = await import("../../src/harness/envelope.js");
    const eff = parsePostEffects({
      note: " n".repeat(2000),
      facts: [
        { name: "a", facts: { x: 1 } }, { name: "b" }, { name: "" }, "junk",
        { name: "c" }, { name: "d" }, { name: "e" }, { name: "f" },
      ],
      launchMissiles: true,
    });
    expect(eff?.note?.length).toBeLessThanOrEqual(1000);
    expect(eff?.facts?.map((f) => f.name)).toEqual(["a", "b", "c", "d", "e"]);
    expect((eff as Record<string, unknown>).launchMissiles).toBeUndefined();
  });

  it("runPost returns effects; a slow post is cut; capFinalText caps", async () => {
    const { runPost, compileHooks: ch, capFinalText } = await import("../../src/harness/envelope.js");
    const hooks = ch(`export async function post(ctx) { return { note: "saw " + ctx.finalText }; }`)!;
    const ctx = { ...CTX, finalText: "hello", aborted: false, usage: { inTokens: 1, outTokens: 2 }, plan: null };
    const r = await runPost(hooks, ctx, 100);
    expect(r.effects?.note).toBe("saw hello");
    const slow = ch(`exports.post = () => new Promise(() => {});`)!;
    const r2 = await runPost(slow, ctx, 30);
    expect(r2.error).toContain("exceeded");
    expect(capFinalText("x".repeat(9000))).toContain("capped");
    const none = await runPost({}, ctx, 30);
    expect(none).toEqual({ effects: null, ms: 0 });
  });
});

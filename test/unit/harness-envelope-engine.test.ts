/**
 * PROGRAM-PER-TURN (P1) — the envelope INSIDE the engine: the loop's `pre`
 * fires before the SDK loop (flag-gated), its plan applies narrow-only
 * (steer → system prompt, tools → intersection), refuse short-circuits the
 * run before the model, and every outcome lands a loop-pre setup frame.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { runHarness, SANDBOX_TOOLS } from "../../src/harness/engine.js";
import type { QueryFn } from "../../src/harness/engine.js";
import { HarnessSession } from "../../src/harness/session.js";
import type { HarnessRunConfig } from "../../src/harness/config.js";
import type { WireFrame } from "../../src/harness/frames.js";
import { clearEnvelopeCache } from "../../src/harness/envelope.js";

function cfg(over: Partial<HarnessRunConfig> = {}): HarnessRunConfig {
  const home = mkdtempSync(join(tmpdir(), "envlp-"));
  return {
    runId: over.runId ?? "run-e",
    sessionId: "sess-e",
    prompt: "do the thing",
    history: [],
    mode: "code",
    permission: "auto",
    gatewayUrl: "https://gw.test",
    runtimeToken: "gy_rt_envelope_secret",
    workdir: join(home, "workspace"),
    attachDir: join(home, "workspace"),
    configDir: join(home, "agent-config"),
    attachments: [],
    attachmentMaxBytes: 1024,
    maxTurns: 0,
    ...over,
  };
}

const okStream: unknown[] = [
  { type: "result", subtype: "success", result: "done", num_turns: 1, usage: { input_tokens: 10, output_tokens: 2 } },
];

const frames = (s: HarnessSession): WireFrame[] => s.framesSince(-1);
const loopPre = (s: HarnessSession): Record<string, unknown> | undefined =>
  frames(s).find((f) => f.type === "setup" && (f as { phase?: string }).phase === "loop-pre") as
    | Record<string, unknown>
    | undefined;

describe("runHarness — the program envelope", () => {
  beforeEach(() => {
    clearEnvelopeCache();
    process.env.ROTOR_TURN_PROGRAM = "1";
  });
  afterEach(() => {
    delete process.env.ROTOR_TURN_PROGRAM;
  });

  it("pre's plan applies: steer rides the system prompt, tools intersect the pack", async () => {
    let captured: { options: Record<string, unknown> } | null = null;
    const queryFn: QueryFn = (args) => {
      captured = args as { options: Record<string, unknown> };
      return (async function* () { for (const m of okStream) yield m; })();
    };
    const s = new HarnessSession({ runId: "run-e" });
    await runHarness(s, cfg({ loop: "steer-loop" }), {
      queryFn,
      loopSource: async () => `
        export async function pre(ctx) {
          return { steer: "Answer in haiku about " + ctx.loop, tools: ["Bash", "NotATool"], contextBudget: 500 };
        }`,
    });
    const pre = loopPre(s);
    expect(pre).toMatchObject({ loop: "steer-loop" });
    expect(pre?.applied).toEqual(expect.arrayContaining(["steer", "tools", "contextBudget"]));
    expect(captured).not.toBeNull();
    const sys = String(captured!.options.systemPrompt ?? "");
    expect(sys).toContain("Loop steer (steer-loop)");
    expect(sys).toContain("Answer in haiku");
    // NARROW-ONLY: intersection with the sandbox pack — a made-up tool never lands.
    expect(captured!.options.tools).toEqual(SANDBOX_TOOLS.filter((t) => t === "Bash"));
    expect(frames(s).pop()).toMatchObject({ type: "done" });
  });

  it("refuse ends the run before the model — queryFn never called", async () => {
    let called = 0;
    const queryFn: QueryFn = () => { called++; return (async function* () { yield okStream[0]; })(); };
    const s = new HarnessSession({ runId: "run-e2" });
    await runHarness(s, cfg({ loop: "gate-loop" }), {
      queryFn,
      loopSource: async () => `exports.pre = () => ({ refuse: "outside working hours" });`,
    });
    expect(called).toBe(0);
    expect(loopPre(s)).toBeTruthy();
    const err = frames(s).find((f) => f.type === "error") as { error?: string } | undefined;
    expect(err?.error).toContain("outside working hours");
  });

  it("flag OFF → no envelope, full pack, no loop-pre frame", async () => {
    delete process.env.ROTOR_TURN_PROGRAM;
    let captured: { options: Record<string, unknown> } | null = null;
    const queryFn: QueryFn = (args) => {
      captured = args as { options: Record<string, unknown> };
      return (async function* () { for (const m of okStream) yield m; })();
    };
    const s = new HarnessSession({ runId: "run-e3" });
    await runHarness(s, cfg({ loop: "steer-loop" }), {
      queryFn,
      loopSource: async () => `exports.pre = () => ({ steer: "never applied" });`,
    });
    expect(loopPre(s)).toBeUndefined();
    expect(captured!.options.tools).toEqual(SANDBOX_TOOLS);
    expect(String(captured!.options.systemPrompt ?? "")).not.toContain("never applied");
  });

  it("a throwing hook FAILS OPEN: loop-pre frame carries the error, the turn runs", async () => {
    const queryFn: QueryFn = () => (async function* () { for (const m of okStream) yield m; })();
    const s = new HarnessSession({ runId: "run-e4" });
    await runHarness(s, cfg({ loop: "broken-loop" }), {
      queryFn,
      loopSource: async () => `exports.pre = () => { throw new Error("hook bug"); };`,
    });
    const pre = loopPre(s);
    expect(String(pre?.error ?? "")).toContain("hook bug");
    expect(frames(s).pop()).toMatchObject({ type: "done" });
  });

  it("a bindings-only loop (no hooks) is a plain turn — no loop-pre frame", async () => {
    const queryFn: QueryFn = () => (async function* () { for (const m of okStream) yield m; })();
    const s = new HarnessSession({ runId: "run-e5" });
    await runHarness(s, cfg({ loop: "plain-loop" }), {
      queryFn,
      loopSource: async () => `exports.mode = "envelope"; // roles only, no pre`,
    });
    expect(loopPre(s)).toBeUndefined();
    expect(frames(s).pop()).toMatchObject({ type: "done" });
  });
});

describe("runHarness — the post half (P2)", () => {
  beforeEach(() => { clearEnvelopeCache(); process.env.ROTOR_TURN_PROGRAM = "1"; });
  afterEach(() => { delete process.env.ROTOR_TURN_PROGRAM; });

  it("post sees the final text and its note lands in a loop-post frame BEFORE done", async () => {
    const queryFn: QueryFn = () => (async function* () {
      yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "Hello" } } };
      yield { type: "result", subtype: "success", result: "Hello", num_turns: 1, usage: { input_tokens: 5, output_tokens: 1 } };
    })();
    const s = new HarnessSession({ runId: "run-p2" });
    await runHarness(s, cfg({ loop: "post-loop" }), {
      queryFn,
      loopSource: async () => `export async function post(ctx) { return { note: "len=" + ctx.finalText.length + " out=" + ctx.usage.outTokens }; }`,
    });
    const all = frames(s);
    const post = all.find((f) => f.type === "setup" && (f as { phase?: string }).phase === "loop-post") as Record<string, unknown> | undefined;
    expect(post).toBeTruthy();
    expect(String(post?.note)).toContain("len=5");
    expect(all.findIndex((f) => f === post)).toBeLessThan(all.findIndex((f) => f.type === "done"));
  });

  it("post runs on a FAILED turn with the error in ctx", async () => {
    const queryFn: QueryFn = () => (async function* () {
      yield { type: "result", subtype: "error_during_execution", errorMessage: "model exploded" };
    })();
    const s = new HarnessSession({ runId: "run-p2e" });
    await runHarness(s, cfg({ loop: "post-loop" }), {
      queryFn,
      loopSource: async () => `exports.post = (ctx) => ({ note: ctx.error ? "failed: yes" : "failed: no" });`,
    });
    const post = frames(s).find((f) => f.type === "setup" && (f as { phase?: string }).phase === "loop-post") as Record<string, unknown> | undefined;
    expect(String(post?.note ?? "")).toContain("failed: yes");
  });
});

describe("runHarness — inline program (P3, local pods)", () => {
  beforeEach(() => { clearEnvelopeCache(); process.env.ROTOR_TURN_PROGRAM = "1"; });
  afterEach(() => { delete process.env.ROTOR_TURN_PROGRAM; });

  it("cfg.loopProgram runs the envelope with NO loopSource (the desktop path)", async () => {
    let captured: { options: Record<string, unknown> } | null = null;
    const queryFn: QueryFn = (args) => {
      captured = args as { options: Record<string, unknown> };
      return (async function* () { for (const m of okStream) yield m; })();
    };
    const s = new HarnessSession({ runId: "run-p3" });
    await runHarness(s, cfg({
      loop: "local-loop",
      loopProgram: `export async function pre() { return { steer: "local program speaking" }; }`,
    }), { queryFn });
    expect(loopPre(s)).toMatchObject({ loop: "local-loop" });
    expect(String(captured!.options.systemPrompt ?? "")).toContain("local program speaking");
  });

  it("an inline program compiles FRESH each turn — an edit applies immediately (no TTL cache)", async () => {
    const run = async (steer: string): Promise<string> => {
      let sys = "";
      const queryFn: QueryFn = (args) => {
        sys = String((args as { options: Record<string, unknown> }).options.systemPrompt ?? "");
        return (async function* () { for (const m of okStream) yield m; })();
      };
      const s = new HarnessSession({ runId: `run-p3-${steer}` });
      await runHarness(s, cfg({ loop: "edited-loop", loopProgram: `exports.pre = () => ({ steer: "${steer}" });` }), { queryFn });
      return sys;
    };
    expect(await run("v1")).toContain("v1");
    expect(await run("v2")).toContain("v2");   // the TTL cache would have served v1
  });
});

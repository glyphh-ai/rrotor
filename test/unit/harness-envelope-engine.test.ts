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

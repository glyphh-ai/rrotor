/**
 * Log-drain unit tests (BUILD_PLAN.md Phase 3): envelope serialization + field
 * redaction, and the BufferedDrain reliability contract (batching, backpressure
 * drop-oldest, retry-with-backoff, drop-after-retries) — all without real I/O.
 */

import { describe, it, expect, vi } from "vitest";

import { BufferedDrain, NoopDrain, toEnvelope } from "../../src/plugins/drain.js";
import type { DrainEnvelope } from "../../src/plugins/interfaces.js";
import type { StepRecord } from "../../src/types.js";

function rec(over: Partial<StepRecord> = {}): StepRecord {
  return {
    run_id: "run-1",
    step_id: "ask",
    attempt: 0,
    logical_tick: 3,
    input_hash: "h",
    idempotency_key: "k",
    space_id: "spaceA",
    principal: { id: "local", kind: "user", scopes: ["memory:read"] },
    agent_identity: { ref: "glyphh/base@0.1.0", run_id: "run-1" },
    status: "ok",
    output: { answer: "42", secret: "hunter2" },
    frames: [{ type: "done" }],
    usage: { input: 10, output: 5 },
    ...over,
  };
}

/** A concrete BufferedDrain that records shipped batches and can be told to fail
 *  the next N ship attempts. */
class CollectDrain extends BufferedDrain {
  shipped: DrainEnvelope[][] = [];
  failNext = 0;
  constructor(opts: ConstructorParameters<typeof BufferedDrain>[0] = {}) {
    super({ sleep: async () => {}, ...opts });
  }
  protected async ship(batch: DrainEnvelope[]): Promise<void> {
    if (this.failNext > 0) {
      this.failNext--;
      throw new Error("sink down");
    }
    this.shipped.push(batch);
  }
}

describe("toEnvelope", () => {
  it("carries the record's audit + telemetry fields", () => {
    const e = toEnvelope(rec());
    expect(e).toMatchObject({
      type: "com.rrotor.step.v0",
      run_id: "run-1",
      step_id: "ask",
      attempt: 0,
      logical_tick: 3,
      status: "ok",
      space_id: "spaceA",
      principal: { id: "local", kind: "user", scopes: ["memory:read"] },
      agent: { ref: "glyphh/base@0.1.0", run_id: "run-1" },
      usage: { input: 10, output: 5 },
    });
  });

  it("redacts named output fields and leaves others intact", () => {
    const e = toEnvelope(rec(), new Set(["secret"]));
    expect(e.output).toEqual({ answer: "42", secret: "[redacted]" });
  });

  it("is deterministic (no wall-clock in the envelope)", () => {
    expect(toEnvelope(rec())).toEqual(toEnvelope(rec()));
  });
});

describe("NoopDrain", () => {
  it("is a ready basic seam that forwards nowhere", async () => {
    const d = new NoopDrain();
    expect(() => d.emit(rec())).not.toThrow();
    await expect(d.flush()).resolves.toBeUndefined();
    expect(d.status()).toMatchObject({ ready: true, tier: "basic" });
  });
});

describe("BufferedDrain — delivery", () => {
  it("ships buffered envelopes in order, batched", async () => {
    const d = new CollectDrain({ batchSize: 2, maxBuffer: 100, autoFlush: false });
    for (let i = 0; i < 5; i++) d.emit(rec({ logical_tick: i }));
    await d.flush();
    const ticks = d.shipped.flat().map((e) => e.logical_tick);
    expect(ticks).toEqual([0, 1, 2, 3, 4]);
    // 5 envelopes at batchSize 2 → batches of [2,2,1].
    expect(d.shipped.map((b) => b.length)).toEqual([2, 2, 1]);
  });

  it("auto-ships once a full batch accumulates", async () => {
    const d = new CollectDrain({ batchSize: 3, maxBuffer: 100 });
    for (let i = 0; i < 3; i++) d.emit(rec({ logical_tick: i }));
    await vi.waitFor(() => expect(d.shipped.flat()).toHaveLength(3));
  });
});

describe("BufferedDrain — backpressure", () => {
  it("drops the oldest when the buffer is full and counts drops", async () => {
    const d = new CollectDrain({ batchSize: 1000, maxBuffer: 3, autoFlush: false });
    for (let i = 0; i < 5; i++) d.emit(rec({ logical_tick: i }));
    expect(d.stats()).toEqual({ buffered: 3, dropped: 2 });
    await d.flush();
    // The 3 most-recent survive (ticks 2,3,4).
    expect(d.shipped.flat().map((e) => e.logical_tick)).toEqual([2, 3, 4]);
  });
});

describe("BufferedDrain — retry", () => {
  it("retries a failing batch and eventually delivers", async () => {
    const d = new CollectDrain({ batchSize: 10, maxBuffer: 100, maxRetries: 3 });
    d.failNext = 2; // fail twice, succeed on the third attempt
    d.emit(rec());
    await d.flush();
    expect(d.shipped.flat()).toHaveLength(1);
    expect(d.stats().dropped).toBe(0);
  });

  it("drops a batch after exhausting retries — never throws into the run", async () => {
    const d = new CollectDrain({ batchSize: 10, maxBuffer: 100, maxRetries: 3 });
    d.failNext = 99; // always fails
    d.emit(rec());
    d.emit(rec());
    await expect(d.flush()).resolves.toBeUndefined();
    expect(d.shipped).toHaveLength(0);
    expect(d.stats().dropped).toBe(2);
  });
});

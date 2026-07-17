/**
 * Log-drain integration (BUILD_PLAN.md Phase 3): the drain observes the real run
 * without perturbing it, replay never re-emits, the HTTP sink delivers batches
 * (retrying transient failures), and shutdown flushes what's buffered.
 */

import { describe, it, expect } from "vitest";
import * as http from "node:http";
import type { AddressInfo } from "node:net";

import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins, HttpDrain } from "../../src/plugins/index.js";
import { toEnvelope } from "../../src/plugins/drain.js";
import { InProcessStore } from "../../src/exec/store.js";
import { startServer, shutdown } from "../../src/server.js";
import { Runtime } from "../../src/runtime/runtime.js";
import type { DrainEnvelope, DrainPlugin } from "../../src/plugins/interfaces.js";
import type { StepRecord } from "../../src/types.js";
import { shapeOf } from "../harness/replay.js";
import { loadFixture, defaultInputs } from "../harness/fixtures.js";

/** A synchronous capturing drain — records every emitted envelope. */
class CapturingDrain implements DrainPlugin {
  readonly name = "drain";
  envelopes: DrainEnvelope[] = [];
  emit(r: StepRecord): void {
    this.envelopes.push(toEnvelope(r));
  }
  async flush(): Promise<void> {}
  async close(): Promise<void> {}
  status() {
    return { ready: true as const, detail: "capture", tier: "basic" as const };
  }
}

const base = loadFixture("rotors/base.rotor.yaml");
const inputs = defaultInputs(base);

describe("drain observes the run", () => {
  it("emits exactly one envelope per StepRecord", async () => {
    const drain = new CapturingDrain();
    const result = await execute(base, inputs, buildBasicPlugins({ drain }));
    expect(drain.envelopes).toHaveLength(result.history.length);
    for (const e of drain.envelopes) expect(e.run_id).toBe(result.run_id);
  });

  it("does not change the run — shape is identical with and without a drain", async () => {
    const withDrain = shapeOf(await execute(base, inputs, buildBasicPlugins({ drain: new CapturingDrain() })));
    const without = shapeOf(await execute(base, inputs, buildBasicPlugins()));
    expect(withDrain).toEqual(without);
  });

  it("never re-emits on replay (no duplicate telemetry)", async () => {
    const store = new InProcessStore();
    const drain = new CapturingDrain();
    const first = await execute(base, inputs, buildBasicPlugins({ store, drain }));
    const afterFirst = drain.envelopes.length;
    expect(afterFirst).toBe(first.history.length);

    // Second run against the shared store is a pure replay → append() never runs.
    await execute(base, inputs, buildBasicPlugins({ store, drain }));
    expect(drain.envelopes.length).toBe(afterFirst);
  });
});

describe("HttpDrain delivery", () => {
  it("POSTs batched NDJSON and retries a transient 500", async () => {
    const received: DrainEnvelope[] = [];
    let calls = 0;
    const sink = http.createServer((req, res) => {
      calls++;
      const chunks: Buffer[] = [];
      req.on("data", (c: Buffer) => chunks.push(c));
      req.on("end", () => {
        if (calls === 1) {
          res.writeHead(500).end("boom"); // fail the first attempt
          return;
        }
        for (const line of Buffer.concat(chunks).toString("utf8").trim().split("\n")) {
          if (line) received.push(JSON.parse(line));
        }
        res.writeHead(200).end("ok");
      });
    });
    await new Promise<void>((r) => sink.listen(0, () => r()));
    const port = (sink.address() as AddressInfo).port;

    const drain = new HttpDrain({ url: `http://127.0.0.1:${port}`, batchSize: 10, sleep: async () => {} });
    await execute(base, inputs, buildBasicPlugins({ drain }));
    await drain.close();

    expect(calls).toBeGreaterThanOrEqual(2); // failed once, retried
    expect(received.length).toBeGreaterThan(0);
    for (const e of received) expect(e.type).toBe("com.rrotor.step.v0");
    await new Promise<void>((r) => sink.close(() => r()));
  });
});

describe("graceful shutdown", () => {
  it("flushes buffered envelopes before closing", async () => {
    const received: DrainEnvelope[] = [];
    let served = false;
    const sink = http.createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on("data", (c: Buffer) => chunks.push(c));
      req.on("end", () => {
        for (const line of Buffer.concat(chunks).toString("utf8").trim().split("\n")) {
          if (line) received.push(JSON.parse(line));
        }
        served = true;
        res.writeHead(200).end("ok");
      });
    });
    await new Promise<void>((r) => sink.listen(0, () => r()));
    const port = (sink.address() as AddressInfo).port;

    // Large batchSize so nothing ships until we flush on shutdown.
    const drain = new HttpDrain({ url: `http://127.0.0.1:${port}`, batchSize: 10_000, sleep: async () => {} });
    const store = new InProcessStore();
    const server = startServer(0, new Runtime(), store, drain);
    await new Promise<void>((r) => server.once("listening", () => r()));

    await execute(base, inputs, buildBasicPlugins({ store, drain }));
    expect(served).toBe(false); // nothing shipped yet

    await shutdown(server, store, drain);
    expect(received.length).toBeGreaterThan(0); // shutdown flushed the buffer
    await new Promise<void>((r) => sink.close(() => r()));
  });
});

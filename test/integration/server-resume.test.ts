/**
 * Server-level resume (BUILD_PLAN.md Phase 7): POST /run pauses at a wait step and
 * persists the run; POST /runs/:id/resume continues it to completion over HTTP.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";

import { startServer } from "../../src/server.js";

const waitRotor = {
  apiVersion: "rotor.glyphh.ai/v0.1",
  kind: "Rotor",
  metadata: { name: "w", version: "0.1.0" },
  spec: {
    inputs: [],
    entry: "a",
    steps: [
      { id: "a", type: "transform", in: {}, out: {}, config: { set: { step: "a" } }, next: "gate" },
      { id: "gate", type: "wait", in: {}, out: {}, config: { on: "approval", on_timeout: "fail" }, next: "done" },
      { id: "done", type: "transform", in: {}, out: {}, config: { set: { final: "complete" } }, next: "end" },
    ],
  },
};

let server: Server;
let base: string;

beforeAll(async () => {
  server = startServer(0);
  await new Promise<void>((r) => server.once("listening", () => r()));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(async () => {
  await new Promise<void>((r) => server.close(() => r()));
});

const post = (path: string, body: unknown) =>
  fetch(`${base}${path}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

describe("POST /run + /runs/:id/resume", () => {
  it("pauses, then resumes to completion", async () => {
    const paused = (await (await post("/run", { rotor: waitRotor })).json()) as {
      run_id: string;
      status: string;
      interrupt?: { stepId: string };
    };
    expect(paused.status).toBe("interrupted");
    expect(paused.interrupt?.stepId).toBe("gate");

    const resumed = (await (await post(`/runs/${paused.run_id}/resume`, { payload: { value: "ok" } })).json()) as {
      status: string;
      terminal: string;
    };
    expect(resumed.status).toBe("ok");
    expect(resumed.terminal).toBe("end");
  });

  it("404s a resume for an unknown run", async () => {
    const res = await post("/runs/run-does-not-exist/resume", {});
    expect(res.status).toBe(404);
  });
});

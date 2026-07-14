/**
 * SSE streaming transport (the session-durable runtime seam the client SDK consumes).
 * Boots the real server and drives the three behaviours that matter:
 *   1. a live run streams `open → step* → terminal → done` in order, with a monotonic
 *      `seq` cursor and the run id named up front;
 *   2. a dropped client reconnects via GET /runs/:id/events and replays the identical
 *      sequence from the persisted tape, resuming after its cursor;
 *   3. an interrupted run streams (and replays) its `interrupt` frame.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { startServer } from "../../src/server.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const baseRotorYaml = readFileSync(resolve(ROOT, "rotors/base.rotor.yaml"), "utf8");

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

interface Frame {
  id: number;
  event: string;
  data: Record<string, unknown>;
}

/** Parse an SSE payload into frames, dropping the `:`-prefixed version comment. */
function parseSse(text: string): Frame[] {
  return text
    .split("\n\n")
    .map((b) => b.trim())
    .filter((b) => b && !b.startsWith(":"))
    .map((block) => {
      const f: Partial<Frame> = {};
      for (const line of block.split("\n")) {
        const i = line.indexOf(":");
        const field = line.slice(0, i);
        const val = line.slice(i + 1).replace(/^ /, "");
        if (field === "id") f.id = Number(val);
        else if (field === "event") f.event = val;
        else if (field === "data") f.data = JSON.parse(val) as Record<string, unknown>;
      }
      return f as Frame;
    });
}

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

const stream = (body: unknown, headers: Record<string, string> = {}) =>
  fetch(`${base}/run`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream", ...headers },
    body: JSON.stringify(body),
  });

describe("live SSE stream", () => {
  it("streams open → steps → terminal → done in order with a monotonic cursor", async () => {
    const res = await stream({ rotor: baseRotorYaml, inputs: { prompt: "where does ada live?" } });
    expect(res.headers.get("content-type")).toContain("text/event-stream");
    const frames = parseSse(await res.text());

    // The run is named in the first frame, before any step — so a client can reconnect.
    expect(frames[0].event).toBe("open");
    expect(frames[0].data.run_id).toMatch(/^run-/);
    expect(frames[0].data.wire).toBe("rotor.stream/v1");
    expect(frames[0].data.trace_id).toMatch(/^[0-9a-f]{32}$/);

    // The base rotor's steps streamed live, in loop order.
    const steps = frames.filter((f) => f.event === "step").map((f) => f.data.step_id);
    expect(steps).toEqual(["ask", "plan", "execute", "test", "deliver"]);

    // Terminal then done, and `seq` is strictly increasing from 0.
    expect(frames.some((f) => f.event === "answer")).toBe(true);
    expect(frames.at(-1)?.event).toBe("done");
    const seqs = frames.map((f) => f.id);
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
    expect(seqs[0]).toBe(0);
    expect(new Set(seqs).size).toBe(seqs.length);
  });
});

describe("durable reconnect", () => {
  it("replays the identical sequence from the tape, resuming after a cursor", async () => {
    const live = parseSse(await (await stream({ rotor: baseRotorYaml, inputs: { prompt: "resume me" } })).text());
    const runId = live[0].data.run_id as string;

    // Full replay (no cursor) reproduces the live sequence event-for-event.
    const full = parseSse(await (await fetch(`${base}/runs/${runId}/events`)).text());
    expect(full.map((f) => f.event)).toEqual(live.map((f) => f.event));
    expect(full.map((f) => f.id)).toEqual(live.map((f) => f.id));

    // Reconnect after cursor 2 → only the later events, none re-delivered.
    const tail = parseSse(await (await fetch(`${base}/runs/${runId}/events?from=2`)).text());
    expect(tail.every((f) => f.id > 2)).toBe(true);
    expect(tail.map((f) => f.event)).toEqual(live.filter((f) => f.id > 2).map((f) => f.event));

    // Last-Event-ID (what a browser EventSource sends) resumes identically to ?from=.
    const viaHeader = parseSse(await (await fetch(`${base}/runs/${runId}/events`, { headers: { "last-event-id": "2" } })).text());
    expect(viaHeader.map((f) => f.id)).toEqual(tail.map((f) => f.id));
  });

  it("404s a reconnect for an unknown run", async () => {
    const res = await fetch(`${base}/runs/run-nope/events`);
    expect(res.status).toBe(404);
  });
});

describe("interrupt streaming", () => {
  it("streams the interrupt frame, and replay reproduces it", async () => {
    const frames = parseSse(await (await stream({ rotor: waitRotor })).text());
    const interrupt = frames.find((f) => f.event === "interrupt");
    expect(interrupt?.data.step_id).toBe("gate");
    expect(frames.at(-1)?.data.status).toBe("interrupted");

    const runId = frames[0].data.run_id as string;
    const replay = parseSse(await (await fetch(`${base}/runs/${runId}/events`)).text());
    expect(replay.find((f) => f.event === "interrupt")?.data.step_id).toBe("gate");
  });
});

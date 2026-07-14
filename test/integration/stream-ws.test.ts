/**
 * WebSocket streaming transport, driven by the standard `ws` client (an independent
 * implementation on the other side of the wire). Proves the bidirectional lane over
 * one socket: a turn streams `open → step* → terminal → done`, a durable `attach`
 * replays from a cursor, `ping` round-trips, and an interrupted run resumes.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import WebSocket from "ws";

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

let server: Server;
let url: string;

beforeAll(async () => {
  server = startServer(0);
  await new Promise<void>((r) => server.once("listening", () => r()));
  url = `ws://127.0.0.1:${(server.address() as AddressInfo).port}/ws`;
});
afterAll(async () => {
  await new Promise<void>((r) => server.close(() => r()));
});

type Msg = Record<string, unknown>;

/** Open a socket, run `drive`, and collect JSON messages until `done(messages)`. */
function session(drive: (ws: WebSocket) => void, done: (msgs: Msg[]) => boolean): Promise<Msg[]> {
  return new Promise((resolvePromise, reject) => {
    const ws = new WebSocket(url);
    const msgs: Msg[] = [];
    ws.on("message", (data) => {
      msgs.push(JSON.parse(data.toString()) as Msg);
      if (done(msgs)) {
        ws.close();
        resolvePromise(msgs);
      }
    });
    ws.on("open", () => drive(ws));
    ws.on("error", reject);
  });
}

const untilDone = (msgs: Msg[]) => msgs.some((m) => m.kind === "done");

describe("WebSocket turn", () => {
  it("greets with ready, then streams a turn to done", async () => {
    const msgs = await session(
      (ws) => ws.send(JSON.stringify({ type: "turn", rotor: baseRotorYaml, inputs: { prompt: "hi" } })),
      untilDone,
    );
    expect(msgs[0].type).toBe("ready");
    expect(msgs[0].wire).toBe("rotor.stream/v1");

    const events = msgs.filter((m) => m.kind);
    expect(events[0].kind).toBe("open");
    expect(events[0].run_id as string).toMatch(/^run-/);
    expect(events.filter((m) => m.kind === "step").map((m) => m.step_id)).toEqual(["ask", "plan", "execute", "test", "deliver"]);
    expect(events.at(-1)?.kind).toBe("done");

    // seq is monotonic across the event frames.
    const seqs = events.map((m) => m.seq as number);
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
  });

  it("answers an application ping with a pong", async () => {
    const msgs = await session(
      (ws) => ws.send(JSON.stringify({ type: "ping" })),
      (m) => m.some((x) => x.type === "pong"),
    );
    expect(msgs.some((m) => m.type === "pong")).toBe(true);
  });
});

describe("WebSocket durable attach", () => {
  it("replays a completed run from a cursor over the socket", async () => {
    const live = await session(
      (ws) => ws.send(JSON.stringify({ type: "turn", rotor: baseRotorYaml, inputs: { prompt: "attach me" } })),
      untilDone,
    );
    const runId = live.find((m) => m.kind === "open")!.run_id as string;

    const replay = await session(
      (ws) => ws.send(JSON.stringify({ type: "attach", run_id: runId, from: 2 })),
      untilDone,
    );
    const events = replay.filter((m) => m.kind);
    expect(events.every((m) => (m.seq as number) > 2)).toBe(true);
    expect(events.at(-1)?.kind).toBe("done");
  });
});

describe("WebSocket error handling", () => {
  const untilError = (m: Msg[]) => m.some((x) => x.type === "error");

  it("reports an invalid rotor without crashing the socket", async () => {
    const msgs = await session(
      (ws) => ws.send(JSON.stringify({ type: "turn", rotor: { kind: "Rotor", metadata: {}, spec: {} } })),
      untilError,
    );
    expect(msgs.find((m) => m.type === "error")?.detail).toMatch(/invalid rotor/);
  });

  it("errors on an unknown run for attach and resume", async () => {
    const attach = await session((ws) => ws.send(JSON.stringify({ type: "attach", run_id: "run-nope" })), untilError);
    expect(attach.some((m) => m.type === "error")).toBe(true);
    const resume = await session((ws) => ws.send(JSON.stringify({ type: "resume", run_id: "run-nope" })), untilError);
    expect(resume.some((m) => m.type === "error")).toBe(true);
  });

  it("errors on malformed JSON and unknown control types", async () => {
    const bad = await session((ws) => ws.send("not json"), untilError);
    expect(bad.some((m) => m.type === "error")).toBe(true);
    const unknown = await session((ws) => ws.send(JSON.stringify({ type: "frobnicate" })), untilError);
    expect(unknown.some((m) => m.type === "error")).toBe(true);
  });
});

describe("WebSocket resume (human-in-the-loop)", () => {
  it("streams an interrupt, then resumes to completion", async () => {
    const paused = await session(
      (ws) => ws.send(JSON.stringify({ type: "turn", rotor: waitRotor })),
      untilDone,
    );
    const interrupt = paused.find((m) => m.kind === "interrupt");
    expect(interrupt?.step_id).toBe("gate");
    const runId = paused.find((m) => m.kind === "open")!.run_id as string;

    const resumed = await session(
      (ws) => ws.send(JSON.stringify({ type: "resume", run_id: runId, payload: { decision: "approve" } })),
      untilDone,
    );
    const done = resumed.find((m) => m.kind === "done");
    expect(done?.status).toBe("ok");
  });
});

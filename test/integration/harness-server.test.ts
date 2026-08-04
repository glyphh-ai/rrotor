/**
 * The harness pod's HTTP/WS face, end to end with the SDK faked: POST /run
 * accepts and streams frames, /runs/:id/frames replays past a cursor, the
 * ask/approval answer endpoint resumes a paused run, WS attach replays + goes
 * live, capacity caps concurrent runs, and the introspection auth gate fails
 * closed on the data plane while probes stay open.
 */

import { describe, it, expect, afterEach } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import WebSocket from "ws";

import { startHarnessServer } from "../../src/harness/server.js";
import type { QueryFn } from "../../src/harness/engine.js";
import type { Introspector } from "../../src/auth/introspect.js";

const servers: Server[] = [];
afterEach(async () => {
  for (const s of servers.splice(0)) await new Promise<void>((r) => s.close(() => r()));
});

function podEnv(): NodeJS.ProcessEnv {
  return {
    GLYPHH_GATEWAY_URL: "https://gw.test",
    GLYPHH_RUNTIME_TOKEN: "gy_rt_pod_secret",
    ROTOR_SESSION_ID: "sess-pod",
    HARNESS_HOME: mkdtempSync(join(tmpdir(), "pod-")),
  } as NodeJS.ProcessEnv;
}

async function boot(queryFn: QueryFn, opts: { auth?: Introspector; env?: NodeJS.ProcessEnv } = {}): Promise<string> {
  const server = startHarnessServer(0, { env: opts.env ?? podEnv(), engine: { queryFn, approvalTimeoutMs: 5000 }, ...(opts.auth ? { auth: opts.auth } : {}) });
  servers.push(server);
  await new Promise<void>((r) => server.once("listening", () => r()));
  return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
}

const happyQuery: QueryFn = () =>
  (async function* () {
    yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "pong" } } };
    yield { type: "result", subtype: "success", result: "pong", usage: { input_tokens: 10, output_tokens: 1 } };
  })();

type Framed = { seq: number; type: string; [k: string]: unknown };

async function framesOf(base: string, runId: string, from = -1): Promise<{ status: string; frames: Framed[] }> {
  const res = await fetch(`${base}/runs/${runId}/frames?from=${from}`);
  return (await res.json()) as { status: string; frames: Framed[] };
}

async function untilDone(base: string, runId: string, timeoutMs = 5000): Promise<Framed[]> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const { frames } = await framesOf(base, runId);
    if (frames.some((f) => f.type === "done" || f.type === "error")) return frames;
    if (Date.now() > deadline) throw new Error(`run ${runId} never finished; frames: ${JSON.stringify(frames)}`);
    await new Promise((r) => setTimeout(r, 20));
  }
}

describe("harness pod — HTTP", () => {
  it("probes are open and name the mode", async () => {
    const base = await boot(happyQuery);
    expect(await (await fetch(`${base}/healthz`)).json()).toEqual({ status: "ok", mode: "harness" });
    const v = (await (await fetch(`${base}/version`)).json()) as { mode: string; wire: string };
    expect(v.mode).toBe("harness");
    expect(v.wire).toBe("glyphh.harness/v1");
  });

  it("POST /run → {runId}; the frame tape replays with cursors", async () => {
    const base = await boot(happyQuery);
    const res = await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "ping" }) });
    expect(res.status).toBe(200);
    const { runId, sessionId } = (await res.json()) as { runId: string; sessionId: string };
    expect(runId).toMatch(/^run-/);
    expect(sessionId).toBe("sess-pod");
    const frames = await untilDone(base, runId);
    expect(frames.map((f) => f.type)).toContain("delta");
    expect(frames[frames.length - 1]).toMatchObject({ type: "done", stopped: false });
    // Cursor replay: from the last-but-one seq, only the tail comes back.
    const tail = await framesOf(base, runId, frames[frames.length - 2].seq);
    expect(tail.frames).toHaveLength(1);
    expect(tail.frames[0].type).toBe("done");
  });

  it("rejects a bad run request with 400 and an actionable detail", async () => {
    const base = await boot(happyQuery);
    const noPrompt = await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({}) });
    expect(noPrompt.status).toBe(400);
    const env = podEnv();
    delete env.GLYPHH_GATEWAY_URL;
    const base2 = await boot(happyQuery, { env });
    const noGw = await fetch(`${base2}/run`, { method: "POST", body: JSON.stringify({ prompt: "x" }) });
    expect(noGw.status).toBe(400);
    expect(((await noGw.json()) as { detail: string }).detail).toMatch(/gateway/);
  });

  it("caps concurrent runs (one session pod) but keeps finished runs replayable", async () => {
    let release: () => void = () => {};
    const gated: QueryFn = () =>
      (async function* () {
        yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "started" } } };
        await new Promise<void>((r) => (release = r));
        yield { type: "result", subtype: "success", result: "ok" };
      })();
    const base = await boot(gated);
    const first = (await (await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "a" }) })).json()) as { runId: string };
    // Wait until the run is provably inside the loop (its first delta landed),
    // so `release` is bound before we use it.
    for (;;) {
      if ((await framesOf(base, first.runId)).frames.some((f) => f.type === "delta")) break;
      await new Promise((r) => setTimeout(r, 10));
    }
    const busy = await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "b" }) });
    expect(busy.status).toBe(409);
    release();
    await untilDone(base, first.runId);
    const again = await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "c" }) });
    expect(again.status).toBe(200);
    // The finished first run still replays.
    expect((await framesOf(base, first.runId)).status).toBe("done");
  });

  it("POST /runs/:id/stop aborts; the run closes with done{stopped:true}", async () => {
    // Parks until aborted — like the real SDK, the stream ends when the run's
    // abort controller fires (the SDK tears the subprocess down).
    const forever: QueryFn = (args) =>
      (async function* () {
        yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "…" } } };
        const ctrl = (args.options as { abortController: AbortController }).abortController;
        await new Promise<void>((r) => ctrl.signal.addEventListener("abort", () => r(), { once: true }));
      })();
    const base = await boot(forever);
    const { runId } = (await (await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "x" }) })).json()) as { runId: string };
    await new Promise((r) => setTimeout(r, 50));
    expect((await fetch(`${base}/runs/${runId}/stop`, { method: "POST" })).status).toBe(200);
    const frames = await untilDone(base, runId);
    expect(frames[frames.length - 1]).toMatchObject({ type: "done", stopped: true });
  });

  it("404s unknown runs and unknown routes", async () => {
    const base = await boot(happyQuery);
    expect((await fetch(`${base}/runs/run-nope/frames`)).status).toBe(404);
    expect((await fetch(`${base}/nope`)).status).toBe(404);
  });
});

describe("harness pod — ask/approval round trip", () => {
  it("an approval frame pauses the run; POST /runs/:id/answer resumes it", async () => {
    const asking: QueryFn = (args) =>
      (async function* () {
        const canUse = (args.options as { canUseTool: (t: string, i: unknown) => Promise<{ behavior: string }> }).canUseTool;
        const decision = await canUse("Bash", { command: "npm run build" });
        yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: `verdict:${decision.behavior}` } } };
        yield { type: "result", subtype: "success", result: "" };
      })();
    const base = await boot(asking);
    const { runId } = (await (
      await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "build it", permission: "ask" }) })
    ).json()) as { runId: string };

    // The run parks on the approval frame.
    let approval: Framed | undefined;
    for (let i = 0; i < 100 && !approval; i++) {
      approval = (await framesOf(base, runId)).frames.find((f) => f.type === "approval");
      if (!approval) await new Promise((r) => setTimeout(r, 20));
    }
    expect(approval).toMatchObject({ type: "approval", kind: "command", title: "Run npm run build" });
    const status = (await (await fetch(`${base}/runs/${runId}`)).json()) as { status: string; pending: string[] };
    expect(status.status).toBe("running");
    expect(status.pending).toEqual([approval!.id]);

    // Answer → resume.
    const ans = await fetch(`${base}/runs/${runId}/answer`, { method: "POST", body: JSON.stringify({ id: approval!.id, allow: true }) });
    expect(ans.status).toBe(200);
    const frames = await untilDone(base, runId);
    expect(frames.map((f) => (f.type === "delta" ? String(f.delta ?? "") : "")).join("")).toContain("verdict:allow");
  });

  it("answers to unknown ids 404; malformed answers 400", async () => {
    const base = await boot(happyQuery);
    const { runId } = (await (await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "x" }) })).json()) as { runId: string };
    await untilDone(base, runId);
    expect((await fetch(`${base}/runs/${runId}/answer`, { method: "POST", body: JSON.stringify({ id: "apr-ghost", allow: true }) })).status).toBe(404);
    expect((await fetch(`${base}/runs/${runId}/answer`, { method: "POST", body: JSON.stringify({}) })).status).toBe(400);
  });
});

describe("harness pod — WS frame stream", () => {
  it("greets with ready, replays on attach, then streams live to done", async () => {
    let release: () => void = () => {};
    const twoPhase: QueryFn = () =>
      (async function* () {
        yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "early" } } };
        await new Promise<void>((r) => (release = r));
        yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: " late" } } };
        yield { type: "result", subtype: "success", result: "" };
      })();
    const base = await boot(twoPhase);
    const { runId } = (await (await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "x" }) })).json()) as { runId: string };
    // Let the "early" frame land before the socket attaches — it must replay.
    for (;;) {
      if ((await framesOf(base, runId)).frames.some((f) => f.type === "delta")) break;
      await new Promise((r) => setTimeout(r, 10));
    }

    const msgs: Record<string, unknown>[] = [];
    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(`${base.replace("http", "ws")}/ws`);
      ws.on("message", (data) => {
        const m = JSON.parse(data.toString()) as Record<string, unknown>;
        msgs.push(m);
        if (m.type === "ready") ws.send(JSON.stringify({ type: "attach", run_id: runId, from: -1 }));
        if (m.type === "delta" && (m as { delta?: string }).delta === "early") release();
        if (m.type === "done") {
          ws.close();
          resolve();
        }
      });
      ws.on("error", reject);
    });
    expect(msgs[0]).toMatchObject({ type: "ready", wire: "glyphh.harness/v1" });
    const deltas = msgs.filter((m) => m.type === "delta").map((m) => (m as { delta: string }).delta);
    expect(deltas.join("")).toBe("early late");
    expect(msgs[msgs.length - 1]).toMatchObject({ type: "done", runId });
  });

  it("attach to an unknown run errors; ping round-trips", async () => {
    const base = await boot(happyQuery);
    const msgs: Record<string, unknown>[] = [];
    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(`${base.replace("http", "ws")}/ws`);
      ws.on("message", (data) => {
        const m = JSON.parse(data.toString()) as Record<string, unknown>;
        msgs.push(m);
        if (m.type === "ready") {
          ws.send(JSON.stringify({ type: "ping" }));
          ws.send(JSON.stringify({ type: "attach", run_id: "run-ghost" }));
        }
        if (m.type === "error") {
          ws.close();
          resolve();
        }
      });
      ws.on("error", reject);
    });
    expect(msgs.map((m) => m.type)).toEqual(["ready", "pong", "error"]);
  });
});

describe("harness pod — auth (introspection)", () => {
  const denyAll: Introspector = { enabled: true, authorize: () => Promise.resolve({ ok: false, status: 401, reason: "no token" }) };
  const allowAll: Introspector = { enabled: true, authorize: () => Promise.resolve({ ok: true, status: 200 }) };

  it("fails closed on the data plane, probes stay open", async () => {
    const base = await boot(happyQuery, { auth: denyAll });
    expect((await fetch(`${base}/healthz`)).status).toBe(200);
    expect((await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "x" }) })).status).toBe(401);
    // The WS upgrade is rejected before the socket opens.
    await expect(
      new Promise((_, reject) => {
        const ws = new WebSocket(`${base.replace("http", "ws")}/ws`);
        ws.on("error", reject);
      }),
    ).rejects.toThrow(/401/);
  });

  it("admits an authorized caller", async () => {
    const base = await boot(happyQuery, { auth: allowAll });
    const res = await fetch(`${base}/run`, { method: "POST", headers: { authorization: "Bearer tok" }, body: JSON.stringify({ prompt: "x" }) });
    expect(res.status).toBe(200);
  });
});

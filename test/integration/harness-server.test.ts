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

import { startHarnessServer, resolveBind } from "../../src/harness/server.js";
import type { QueryFn } from "../../src/harness/engine.js";
import type { Introspector } from "../../src/auth/introspect.js";
import { createLogger } from "../../src/obs/logger.js";

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

describe("harness pod — mid-run permission change", () => {
  // A run started in `plan` (read-only): the first tool decision is DENIED. We
  // then POST /runs/:id/permission {auto} and the SECOND decision is ALLOWED —
  // proving the gate re-reads the LIVE mode per call, not the start snapshot.
  it("POST /runs/:id/permission changes the live gate mode; the next decision uses it", async () => {
    let flipped: (() => void) | null = null;
    const gate: QueryFn = (args) =>
      (async function* () {
        const canUse = (args.options as { canUseTool: (t: string, i: unknown) => Promise<{ behavior: string }> }).canUseTool;
        const first = await canUse("Edit", { file_path: "a.ts" });      // plan → deny
        // Signal the test that the first decision landed, then wait for the flip.
        await new Promise<void>((r) => { flipped = r; });
        const second = await canUse("Edit", { file_path: "b.ts" });     // auto → allow
        yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: `first:${first.behavior} second:${second.behavior}` } } };
        yield { type: "result", subtype: "success", result: "" };
      })();
    const base = await boot(gate);
    const { runId } = (await (
      await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "edit", permission: "plan" }) })
    ).json()) as { runId: string };

    // Wait until the first (denied) decision resolved and the run is parked on `flipped`.
    for (let i = 0; i < 200 && !flipped; i++) await new Promise((r) => setTimeout(r, 10));
    expect(flipped).toBeTruthy();

    // Flip the live mode → auto, then release the run to make its second decision.
    const res = await fetch(`${base}/runs/${runId}/permission`, { method: "POST", body: JSON.stringify({ permission: "auto" }) });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ ok: true, permission: "auto" });
    (flipped as unknown as () => void)();

    const frames = await untilDone(base, runId);
    const text = frames.map((f) => (f.type === "delta" ? String(f.delta ?? "") : "")).join("");
    expect(text).toContain("first:deny");
    expect(text).toContain("second:allow");
    // The change fanned out on the stream so observers repaint their chip.
    expect(frames.find((f) => f.type === "permission")).toMatchObject({ type: "permission", permission: "auto" });
  });

  it("rejects an invalid permission with 400", async () => {
    const base = await boot(happyQuery);
    const { runId } = (await (await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "x" }) })).json()) as { runId: string };
    const bad = await fetch(`${base}/runs/${runId}/permission`, { method: "POST", body: JSON.stringify({ permission: "yolo" }) });
    expect(bad.status).toBe(400);
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

  it("WS upgrade accepts the bearer as ?token= (browsers cannot set WS headers); bad or absent still rejects", async () => {
    const requireTok: Introspector = {
      enabled: true,
      authorize: (bearer) =>
        Promise.resolve(bearer === "tok" ? { ok: true, status: 200 } : { ok: false, status: 401, reason: "bad token" }),
    };
    const base = await boot(happyQuery, { auth: requireTok });
    // A run to stream (header auth on the HTTP side, unchanged).
    const { runId } = (await (
      await fetch(`${base}/run`, { method: "POST", headers: { authorization: "Bearer tok" }, body: JSON.stringify({ prompt: "x" }) })
    ).json()) as { runId: string };

    // The query-token socket admits, then replay-from--1 + live carry it to
    // done regardless of how far the run has already streamed.
    const msgs: Record<string, unknown>[] = [];
    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(`${base.replace("http", "ws")}/ws?token=tok`);
      ws.on("message", (data) => {
        const m = JSON.parse(data.toString()) as Record<string, unknown>;
        msgs.push(m);
        if (m.type === "ready") ws.send(JSON.stringify({ type: "attach", run_id: runId, from: -1 }));
        if (m.type === "done") {
          ws.close();
          resolve();
        }
      });
      ws.on("error", reject);
    });
    expect(msgs[0]).toMatchObject({ type: "ready", wire: "glyphh.harness/v1" });
    expect(msgs.some((m) => m.type === "delta")).toBe(true);

    // A wrong query token — and no token at all — still reject pre-open.
    for (const suffix of ["/ws?token=wrong", "/ws"]) {
      await expect(
        new Promise((_, reject) => {
          const ws = new WebSocket(`${base.replace("http", "ws")}${suffix}`);
          ws.on("error", reject);
        }),
      ).rejects.toThrow(/401/);
    }
  });
});

describe("harness pod — CORS (browser clients call cross-origin)", () => {
  it("OPTIONS preflight answers 204 with the CORS grant, before the auth gate", async () => {
    const denyAll: Introspector = { enabled: true, authorize: () => Promise.resolve({ ok: false, status: 401, reason: "no token" }) };
    const base = await boot(happyQuery, { auth: denyAll });
    const res = await fetch(`${base}/run`, { method: "OPTIONS" });
    expect(res.status).toBe(204);
    expect(res.headers.get("access-control-allow-origin")).toBe("*");
    expect(res.headers.get("access-control-allow-methods")).toBe("GET, POST, PUT, DELETE, OPTIONS");
    expect(res.headers.get("access-control-allow-headers")).toBe("authorization, content-type");
    expect(res.headers.get("access-control-max-age")).toBe("86400");
  });

  it("every response carries ACAO — success and auth failure alike", async () => {
    const base = await boot(happyQuery);
    const ok = await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "x" }) });
    expect(ok.status).toBe(200);
    expect(ok.headers.get("access-control-allow-origin")).toBe("*");

    const denyAll: Introspector = { enabled: true, authorize: () => Promise.resolve({ ok: false, status: 401, reason: "no token" }) };
    const gated = await boot(happyQuery, { auth: denyAll });
    const denied = await fetch(`${gated}/run`, { method: "POST", body: JSON.stringify({ prompt: "x" }) });
    expect(denied.status).toBe(401);
    expect(denied.headers.get("access-control-allow-origin")).toBe("*"); // a 401 must read as auth, not a CORS mystery
  });
});

describe("harness pod — bind address (local-mode loopback safety)", () => {
  it("auth OFF binds loopback only; auth ON binds wide", async () => {
    const local = startHarnessServer(0, { env: podEnv(), engine: { queryFn: happyQuery } }); // no introspect env → auth off
    servers.push(local);
    await new Promise<void>((r) => local.once("listening", () => r()));
    expect((local.address() as AddressInfo).address).toBe("127.0.0.1");

    const allowAll: Introspector = { enabled: true, authorize: () => Promise.resolve({ ok: true, status: 200 }) };
    const cloud = startHarnessServer(0, { env: podEnv(), engine: { queryFn: happyQuery }, auth: allowAll });
    servers.push(cloud);
    await new Promise<void>((r) => cloud.once("listening", () => r()));
    expect((cloud.address() as AddressInfo).address).toBe("0.0.0.0");
  });

  it("ROTOR_BIND overrides — binding wide with auth off warns loudly", () => {
    const lines: string[] = [];
    const logger = createLogger({ level: "warn", write: (l) => lines.push(l) });
    expect(resolveBind({ ROTOR_BIND: "0.0.0.0" } as NodeJS.ProcessEnv, false, logger)).toBe("0.0.0.0");
    expect(lines.join("\n")).toMatch(/bound WIDE with auth OFF/);
    // The quiet paths: defaults and auth-on overrides never warn.
    expect(resolveBind({} as NodeJS.ProcessEnv, false, logger)).toBe("127.0.0.1");
    expect(resolveBind({} as NodeJS.ProcessEnv, true, logger)).toBe("0.0.0.0");
    expect(resolveBind({ ROTOR_BIND: "10.0.0.7" } as NodeJS.ProcessEnv, true, logger)).toBe("10.0.0.7");
    expect(lines).toHaveLength(1);
  });

  it("rejects malformed mcpServers with 400 (validation lives in resolveRunConfig)", async () => {
    const base = await boot(happyQuery);
    const bad = [
      [{ name: "Desk Top", url: "https://ok.example/mcp" }],
      [{ name: "d", url: "http://pod.internal/mcp" }],
      "not-an-array",
    ];
    for (const mcpServers of bad) {
      const res = await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "x", mcpServers }) });
      expect(res.status).toBe(400);
      expect(((await res.json()) as { detail: string }).detail).toMatch(/mcpServers/);
    }
    // A loopback-http lend is accepted.
    const ok = await fetch(`${base}/run`, {
      method: "POST",
      body: JSON.stringify({ prompt: "x", mcpServers: [{ name: "desktop", url: "http://127.0.0.1:4820/mcp" }] }),
    });
    expect(ok.status).toBe(200);
  });

  it("workdir: a LOCAL pod (auth off) honors it; an auth-ON pod rejects it 400", async () => {
    const dir = mkdtempSync(join(tmpdir(), "user-folder-"));
    // Auth off → local mode → the caller's own folder becomes the run's cwd.
    const local = await boot(happyQuery);
    const ok = await fetch(`${local}/run`, { method: "POST", body: JSON.stringify({ prompt: "x", workdir: dir }) });
    expect(ok.status).toBe(200);

    // Auth on → a shared/cloud pod must never be pointed at a host path.
    const allowAll: Introspector = { enabled: true, authorize: () => Promise.resolve({ ok: true, status: 200 }) };
    const cloud = await boot(happyQuery, { auth: allowAll });
    const denied = await fetch(`${cloud}/run`, {
      method: "POST",
      headers: { authorization: "Bearer tok" },
      body: JSON.stringify({ prompt: "x", workdir: dir }),
    });
    expect(denied.status).toBe(400);
    expect(((await denied.json()) as { detail: string }).detail).toMatch(/local pod/);

    // A nonexistent path is refused even locally.
    const missing = await fetch(`${local}/run`, {
      method: "POST",
      body: JSON.stringify({ prompt: "x", workdir: join(dir, "nope") }),
    });
    expect(missing.status).toBe(400);
  });
});

// ── Concurrency follows the pod's SHAPE ────────────────────────────────────
// A shared pod (auth on, no bound session) serves a whole region: at 1 it 409s
// every concurrent turn, and a PARKED approval holds the only slot for its full
// timeout — which reads to users as "the runtime is down". A dedicated/local
// pod keeps 1, matching the one session it exists for.
describe("harness pod concurrency defaults", () => {
  const allowAll = { enabled: true, authorize: async () => ({ ok: true as const, status: 200 }) };

  it("a SHARED pod (auth on, no bound session) admits many runs by default", () => {
    const server = startHarnessServer(0, { env: {}, auth: allowAll as never });
    const reg = (server as never as { __registry: { capacity?: number; max?: number } }).__registry;
    server.close();
    expect(JSON.stringify(reg)).toMatch(/24/);
  });

  it("a DEDICATED pod (bound session) keeps 1", () => {
    const server = startHarnessServer(0, { env: { ROTOR_SESSION_ID: "sess_x" }, auth: allowAll as never });
    const reg = (server as never as { __registry: unknown }).__registry;
    server.close();
    expect(JSON.stringify(reg)).not.toMatch(/24/);
  });

  it("HARNESS_MAX_RUNS overrides either shape", () => {
    const server = startHarnessServer(0, { env: { HARNESS_MAX_RUNS: "7" }, auth: allowAll as never });
    const reg = (server as never as { __registry: unknown }).__registry;
    server.close();
    expect(JSON.stringify(reg)).toMatch(/7/);
  });
});

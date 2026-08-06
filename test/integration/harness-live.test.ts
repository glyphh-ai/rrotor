/**
 * LIVE harness run — gated behind env, like the pgvector-real suite: set
 *
 *   GLYPHH_GATEWAY_URL    the control-plane gateway (Anthropic-compatible)
 *   GLYPHH_RUNTIME_TOKEN  a live session runtime token
 *   HARNESS_MODEL         (optional) model id to pin
 *
 * and this drives ONE real Claude Agent SDK run through the pod server:
 * subprocess spawn, gateway-routed model call, frame stream to done. Skipped
 * entirely when the env is absent so `npm test` stays hermetic.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { mkdtempSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { startHarnessServer } from "../../src/harness/server.js";

const GATEWAY = process.env.GLYPHH_GATEWAY_URL;
const TOKEN = process.env.GLYPHH_RUNTIME_TOKEN;

describe.skipIf(!GATEWAY || !TOKEN)("harness pod — LIVE gateway run", () => {
  let server: Server;
  let base: string;
  let home: string;

  beforeAll(async () => {
    home = mkdtempSync(join(tmpdir(), "harness-live-"));
    server = startHarnessServer(0, {
      env: { ...process.env, HARNESS_HOME: home } as NodeJS.ProcessEnv,
    });
    await new Promise<void>((r) => server.once("listening", () => r()));
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });
  afterAll(async () => {
    await new Promise<void>((r) => server.close(() => r()));
  });

  it("runs one chat turn end to end and streams to done", { timeout: 180_000 }, async () => {
    const res = await fetch(`${base}/run`, {
      method: "POST",
      body: JSON.stringify({
        prompt: "Reply with exactly one word: pong",
        mode: "chat",
        ...(process.env.HARNESS_MODEL ? { model: process.env.HARNESS_MODEL } : {}),
      }),
    });
    expect(res.status).toBe(200);
    const { runId } = (await res.json()) as { runId: string };

    const deadline = Date.now() + 150_000;
    let frames: Array<{ type: string; delta?: string; error?: string; stopped?: boolean }> = [];
    for (;;) {
      const body = (await (await fetch(`${base}/runs/${runId}/frames`)).json()) as { frames: typeof frames };
      frames = body.frames;
      if (frames.some((f) => f.type === "done" || f.type === "error")) break;
      if (Date.now() > deadline) throw new Error(`live run stalled; frames: ${JSON.stringify(frames.map((f) => f.type))}`);
      await new Promise((r) => setTimeout(r, 500));
    }
    const terminal = frames[frames.length - 1];
    expect(terminal.type, `terminal frame: ${JSON.stringify(terminal)}`).toBe("done");
    const text = frames.filter((f) => f.type === "delta").map((f) => f.delta ?? "").join("");
    expect(text.toLowerCase()).toContain("pong");
  });

  // ── the permission gate, against a REAL agent loop ────────────────────────
  //
  // The unit suite proves the pod stops pre-approving (`allowedTools` unset);
  // only a live run proves the SDK then actually CONSULTS canUseTool. Each
  // case drives a real model that genuinely wants to use the tool.

  type LiveFrame = { type: string; delta?: string; id?: string; kind?: string; error?: string };

  /** Poll a run's tape until `stop` says enough (or it terminates). */
  async function tape(runId: string, stop: (f: LiveFrame[]) => boolean, ms = 150_000): Promise<LiveFrame[]> {
    const deadline = Date.now() + ms;
    for (;;) {
      const { frames } = (await (await fetch(`${base}/runs/${runId}/frames`)).json()) as { frames: LiveFrame[] };
      if (stop(frames) || frames.some((f) => f.type === "done" || f.type === "error")) return frames;
      if (Date.now() > deadline) throw new Error(`live run stalled; frames: ${JSON.stringify(frames.map((f) => f.type))}`);
      await new Promise((r) => setTimeout(r, 500));
    }
  }

  const start = async (body: Record<string, unknown>): Promise<string> => {
    const res = await fetch(`${base}/run`, {
      method: "POST",
      body: JSON.stringify({ ...(process.env.HARNESS_MODEL ? { model: process.env.HARNESS_MODEL } : {}), ...body }),
    });
    expect(res.status).toBe(200);
    return ((await res.json()) as { runId: string }).runId;
  };

  /** Where a run's own sandbox lands (config.ts: HARNESS_HOME/sessions/<scope>/
   *  workspace, scope = the run id when no session is bound). */
  const sandboxFile = (runId: string, name: string): string => join(home, "sessions", runId, "workspace", name);

  it("plan mode is READ-ONLY: the file is never created", { timeout: 180_000 }, async () => {
    const runId = await start({
      prompt: "Create a file named must-not-exist.txt containing the word hello. Use the Write tool.",
      mode: "code",
      permission: "plan",
    });
    await tape(runId, () => false);
    // The gate must have refused — nothing on disk, whatever the model tried.
    expect(existsSync(sandboxFile(runId, "must-not-exist.txt")), "plan mode wrote a file — the gate did not hold").toBe(false);
  });

  it("ask mode raises an approval frame and parks; answering resolves it", { timeout: 180_000 }, async () => {
    const runId = await start({
      prompt: "Create a file named approved.txt containing the word hello. Use the Write tool.",
      mode: "code",
      permission: "ask",
    });
    const target = sandboxFile(runId, "approved.txt");
    const parked = await tape(runId, (f) => f.some((x) => x.type === "approval"));
    const approval = parked.find((f) => f.type === "approval");
    expect(approval, "ask mode emitted NO approval frame — the gate was bypassed").toBeDefined();

    // The run is parked: the file cannot exist yet.
    expect(existsSync(target)).toBe(false);
    const status = (await (await fetch(`${base}/runs/${runId}`)).json()) as { status: string; pending: string[] };
    expect(status.status).toBe("running");
    expect(status.pending).toContain(approval!.id);

    // Allow → the tool runs for real.
    const answered = await fetch(`${base}/runs/${runId}/answer`, {
      method: "POST",
      body: JSON.stringify({ id: approval!.id, allow: true }),
    });
    expect(answered.status).toBe(200);
    await tape(runId, () => false);
    expect(existsSync(target), "an APPROVED write did not happen").toBe(true);
  });

  it("auto mode runs clean: the write happens with no approval frame", { timeout: 180_000 }, async () => {
    const runId = await start({
      prompt: "Create a file named auto.txt containing the word hello. Use the Write tool.",
      mode: "code",
      permission: "auto",
    });
    const frames = await tape(runId, () => false);
    expect(frames.filter((f) => f.type === "approval")).toHaveLength(0);
    expect(existsSync(sandboxFile(runId, "auto.txt"))).toBe(true);
  });
});

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
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { startHarnessServer } from "../../src/harness/server.js";

const GATEWAY = process.env.GLYPHH_GATEWAY_URL;
const TOKEN = process.env.GLYPHH_RUNTIME_TOKEN;

describe.skipIf(!GATEWAY || !TOKEN)("harness pod — LIVE gateway run", () => {
  let server: Server;
  let base: string;

  beforeAll(async () => {
    server = startHarnessServer(0, {
      env: {
        ...process.env,
        HARNESS_HOME: mkdtempSync(join(tmpdir(), "harness-live-")),
      } as NodeJS.ProcessEnv,
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
});

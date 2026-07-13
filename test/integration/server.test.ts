/**
 * HTTP server integration tests (BUILD_PLAN.md Phase 1). Boots the real server on
 * an ephemeral port and drives the probe + run surface over the wire, proving
 * `/readyz` tells the truth and `POST /run` executes a rotor.
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

let server: Server;
let base: string;

beforeAll(async () => {
  server = startServer(0);
  await new Promise<void>((r) => server.once("listening", () => r()));
  const port = (server.address() as AddressInfo).port;
  base = `http://127.0.0.1:${port}`;
});

afterAll(async () => {
  await new Promise<void>((r) => server.close(() => r()));
});

describe("probes", () => {
  it("GET /healthz is ok", async () => {
    const res = await fetch(`${base}/healthz`);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ status: "ok" });
  });

  it("GET /readyz reports ready with the true per-seam tier map", async () => {
    const res = await fetch(`${base}/readyz`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as {
      status: string;
      capabilities: Record<string, { ready: boolean; tier: string }>;
    };
    expect(body.status).toBe("ready");
    // Every advertised seam is ready + basic-tier (real plugins).
    for (const [name, cap] of Object.entries(body.capabilities)) {
      expect(cap.ready, name).toBe(true);
      expect(cap.tier, name).toBe("basic");
    }
    expect(Object.keys(body.capabilities)).toContain("memory");
  });

  it("GET /version returns build identity", async () => {
    const res = await fetch(`${base}/version`);
    const body = (await res.json()) as { name: string; version: string };
    expect(body.name).toBe("openrotor");
    expect(typeof body.version).toBe("string");
  });
});

describe("POST /run", () => {
  it("executes an inline YAML rotor and returns a run summary", async () => {
    const res = await fetch(`${base}/run`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ rotor: baseRotorYaml, inputs: { prompt: "hello" } }),
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as {
      run_id: string;
      status: string;
      history: unknown[];
    };
    expect(body.run_id).toMatch(/^run-/);
    expect(["ok", "refused", "failed", "interrupted"]).toContain(body.status);
    expect(body.history.length).toBeGreaterThan(0);
  });

  it("rejects a missing rotor with 400", async () => {
    const res = await fetch(`${base}/run`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ inputs: {} }),
    });
    expect(res.status).toBe(400);
  });

  it("404s an unknown route", async () => {
    const res = await fetch(`${base}/nope`);
    expect(res.status).toBe(404);
  });

  it("replays a run across two requests against the shared stator (§17.1)", async () => {
    const body = JSON.stringify({ rotor: baseRotorYaml, inputs: { prompt: "shared-state" } });
    const post = () =>
      fetch(`${base}/run`, { method: "POST", headers: { "content-type": "application/json" }, body });

    const first = (await (await post()).json()) as { run_id: string; outputs: unknown; history: unknown[] };
    const second = (await (await post()).json()) as { run_id: string; outputs: unknown; history: unknown[] };

    // Same doc + inputs → same run id; the second request replays the first's
    // recorded history from the shared stator, returning an identical result.
    expect(second.run_id).toBe(first.run_id);
    expect(second.outputs).toEqual(first.outputs);
    expect(second.history).toEqual(first.history);
  });
});

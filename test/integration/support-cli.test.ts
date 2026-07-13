/**
 * E4 — traceability & supportability. The `errors` catalog command and the
 * `support <run_id>` bundle give a human or an AI dev-ops agent everything needed
 * to trace a failure and fix it: code → remediation, and a run's trace + timeline.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";

import { main } from "../../src/cli.js";

/** Capture stdout across a main() invocation. */
async function capture(argv: string[]): Promise<{ code: number; out: string }> {
  const lines: string[] = [];
  const orig = console.log;
  console.log = (...a: unknown[]) => lines.push(a.join(" "));
  try {
    const code = await main(argv);
    return { code, out: lines.join("\n") };
  } finally {
    console.log = orig;
  }
}

describe("openrotor errors", () => {
  it("prints the whole catalog", async () => {
    const { code, out } = await capture(["errors"]);
    expect(code).toBe(0);
    expect(out).toMatch(/E_UNGROUNDED/);
    expect(out).toMatch(/codes\./);
  });

  it("prints one code's remediation", async () => {
    const { out } = await capture(["errors", "E_SPACE_MISMATCH"]);
    expect(out).toMatch(/grounding/);
    expect(out).toMatch(/fix:/);
  });

  it("emits machine-readable JSON with --json", async () => {
    const { out } = await capture(["errors", "--json"]);
    const parsed = JSON.parse(out) as Array<{ code: string; remediation: string }>;
    expect(parsed.length).toBeGreaterThan(20);
    expect(parsed.every((e) => e.code && e.remediation)).toBe(true);
  });
});

describe("openrotor support", () => {
  const url = "/tmp/claude-0/-home-user-openrotor/e7e3ce1b-c868-5ec6-9b72-65265b041e19/scratchpad/support-test.db";
  beforeEach(() => {
    process.env.ROTOR_STATOR_BACKEND = "sqlite";
    process.env.ROTOR_STATOR_URL = url;
  });
  afterEach(() => {
    delete process.env.ROTOR_STATOR_BACKEND;
    delete process.env.ROTOR_STATOR_URL;
  });

  it("runs a rotor durably, then produces a support bundle for that run", async () => {
    const run = await capture(["run", "rotors/base.rotor.yaml", "prompt=hi"]);
    const runId = /run-[a-f0-9]+/.exec(run.out)?.[0];
    expect(runId).toBeTruthy();

    const bundle = await capture(["support", runId!, "--json"]);
    const parsed = JSON.parse(bundle.out) as { run_id: string; trace_id: string; steps: number; timeline: unknown[] };
    expect(parsed.run_id).toBe(runId);
    expect(parsed.trace_id).toMatch(/^[0-9a-f]{32}$/);
    expect(parsed.steps).toBeGreaterThan(0);
    expect(parsed.timeline.length).toBe(parsed.steps);
  });
});

/**
 * Harness frame envelope + replay ring + the session's pause/resume registry.
 * The session is the pod-side identity of one run: frames get monotonic seq
 * cursors, late subscribers replay from their cursor, and an approval/ask
 * frame parks the run on a promise that POST /runs/:id/answer resolves.
 */

import { describe, it, expect } from "vitest";

import { FrameRing, isTerminal, HARNESS_WIRE_VERSION, toolTitle, redactTitle } from "../../src/harness/frames.js";
import type { WireFrame } from "../../src/harness/frames.js";
import { HarnessSession, mintRunId } from "../../src/harness/session.js";

const wf = (seq: number): WireFrame => ({ type: "delta", delta: `d${seq}`, seq, runId: "run-x", at: 0 });

describe("FrameRing", () => {
  it("replays everything from -1 and only past a cursor otherwise", () => {
    const ring = new FrameRing();
    for (let i = 0; i < 5; i++) ring.push(wf(i));
    expect(ring.since(-1)).toHaveLength(5);
    expect(ring.since(2).map((f) => f.seq)).toEqual([3, 4]);
    expect(ring.since(99)).toHaveLength(0);
  });

  it("drops the oldest frames past its cap", () => {
    const ring = new FrameRing(3);
    for (let i = 0; i < 10; i++) ring.push(wf(i));
    expect(ring.since(-1).map((f) => f.seq)).toEqual([7, 8, 9]);
  });
});

describe("HarnessSession — emit + subscribe", () => {
  it("stamps monotonic seq + runId and fans out live", () => {
    const s = new HarnessSession({ runId: "run-a", now: () => 42 });
    const seen: WireFrame[] = [];
    s.subscribe((f) => seen.push(f));
    s.emit({ type: "delta", delta: "hi" });
    s.emit({ type: "progress", inTokens: 1, outTokens: 2 });
    expect(seen.map((f) => f.seq)).toEqual([0, 1]);
    expect(seen[0]).toMatchObject({ type: "delta", delta: "hi", runId: "run-a", at: 42 });
  });

  it("replays the tape to a late subscriber from its cursor", () => {
    const s = new HarnessSession({ runId: "run-b" });
    s.emit({ type: "delta", delta: "a" });
    s.emit({ type: "delta", delta: "b" });
    s.emit({ type: "delta", delta: "c" });
    const seen: WireFrame[] = [];
    s.subscribe((f) => seen.push(f), 0); // cursor 0 → replay 1,2 then live
    expect(seen.map((f) => f.seq)).toEqual([1, 2]);
    s.emit({ type: "done", stopped: false });
    expect(seen.map((f) => f.seq)).toEqual([1, 2, 3]);
  });

  it("a broken subscriber never blocks the others", () => {
    const s = new HarnessSession({});
    const seen: string[] = [];
    s.subscribe(() => {
      throw new Error("boom");
    });
    s.subscribe((f) => seen.push(f.type));
    s.emit({ type: "delta", delta: "x" });
    expect(seen).toEqual(["delta"]);
  });

  it("tracks terminal status from done/error frames", () => {
    const a = new HarnessSession({});
    a.emit({ type: "done", stopped: false });
    expect(a.status).toBe("done");
    const b = new HarnessSession({});
    b.emit({ type: "done", stopped: true });
    expect(b.status).toBe("stopped");
    const c = new HarnessSession({});
    c.emit({ type: "error", error: "nope" });
    expect(c.status).toBe("error");
    expect(isTerminal({ type: "done", stopped: false })).toBe(true);
    expect(isTerminal({ type: "delta", delta: "x" })).toBe(false);
  });
});

describe("HarnessSession — pause/resume", () => {
  it("answer() resolves a parked waitAnswer", async () => {
    const s = new HarnessSession({});
    const p = s.waitAnswer("apr-1", 5000, { allow: false });
    expect(s.pendingIds()).toEqual(["apr-1"]);
    expect(s.answer("apr-1", { allow: true })).toBe(true);
    await expect(p).resolves.toEqual({ allow: true });
    expect(s.pendingIds()).toEqual([]);
  });

  it("answers to unknown ids report false", () => {
    const s = new HarnessSession({});
    expect(s.answer("nope", { allow: true })).toBe(false);
  });

  it("times out to the fallback (deny/empty) — never hangs", async () => {
    const s = new HarnessSession({});
    await expect(s.waitAnswer("apr-2", 10, { allow: false })).resolves.toEqual({ allow: false });
  });

  it("a terminal frame resolves everything still parked", async () => {
    const s = new HarnessSession({});
    const p = s.waitAnswer("ask-1", 60_000, { answers: [] });
    s.emit({ type: "error", error: "run died" });
    await expect(p).resolves.toEqual({});
  });

  it("coerces answer payloads (allow bool, answers strings)", async () => {
    const s = new HarnessSession({});
    const p = s.waitAnswer("ask-2", 5000, {});
    s.answer("ask-2", { allow: 1 as unknown as boolean, answers: [2, "b"] as unknown as string[] });
    await expect(p).resolves.toEqual({ allow: true, answers: ["2", "b"] });
  });
});

describe("mintRunId", () => {
  it("mints desktop-format run ids", () => {
    expect(mintRunId()).toMatch(/^run-[a-z0-9]+-[a-z0-9]+$/);
    expect(HARNESS_WIRE_VERSION).toBe("glyphh.harness/v1");
  });
});

describe("toolTitle — the breadcrumb's key input", () => {
  it("derives per-tool: command, file path, pattern", () => {
    expect(toolTitle("Bash", { command: "npm run  build" })).toBe("Run: npm run build");
    expect(toolTitle("Read", { file_path: "/w/src/a.ts" })).toBe("Read /w/src/a.ts");
    expect(toolTitle("Write", { file_path: "/w/out.txt", content: "x" })).toBe("Write /w/out.txt");
    expect(toolTitle("Edit", { file_path: "/w/b.ts" })).toBe("Edit /w/b.ts");
    expect(toolTitle("Grep", { pattern: "toolTitle", path: "src/" })).toBe('Grep "toolTitle" in src/');
    expect(toolTitle("Glob", { pattern: "**/*.ts" })).toBe('Glob "**/*.ts"');
  });

  it("strips MCP prefixes and picks the salient arg of unknown tools", () => {
    expect(toolTitle("mcp__glyphh_apps__build_app", { slug: "my-app", deploy: true })).toBe("build_app — my-app");
    expect(toolTitle("mcp__desktop__open_browser", { url: "https://x.dev" })).toBe("open_browser — https://x.dev");
  });

  it("returns empty when nothing salient exists, and truncates long commands", () => {
    expect(toolTitle("Bash", {})).toBe("");
    expect(toolTitle("KillShell", {})).toBe("");
    const long = toolTitle("Bash", { command: "x".repeat(500) });
    expect(long.length).toBeLessThanOrEqual(170);
    expect(long.startsWith("Run: ")).toBe(true);
  });

  it("NEVER carries secret-looking values", () => {
    expect(toolTitle("Bash", { command: "curl -H api_key=sk-live-123 https://api" })).toBe("Run: curl -H api_key=[redacted] https://api");
    expect(redactTitle("TOKEN: abc123 password=hunter2")).toBe("TOKEN: [redacted] password=[redacted]");
    expect(redactTitle("Write /w/notes.txt")).toBe("Write /w/notes.txt");
  });
});

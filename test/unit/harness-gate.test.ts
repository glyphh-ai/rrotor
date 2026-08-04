/**
 * The headless permission gate: the desktop's mode/kind decision table, with
 * approval cards replaced by `approval` frames + the session's answer seam.
 */

import { describe, it, expect } from "vitest";

import { classifyTool, gateAction, bareToolName } from "../../src/harness/gate.js";
import { HarnessSession } from "../../src/harness/session.js";
import type { WireFrame } from "../../src/harness/frames.js";

describe("classifyTool", () => {
  it("classifies reads, edits, commands", () => {
    expect(classifyTool("Read", { file_path: "/x" }).kind).toBe("read");
    expect(classifyTool("Grep", { pattern: "foo" }).kind).toBe("read");
    expect(classifyTool("WebFetch", { url: "https://x" }).kind).toBe("read");
    expect(classifyTool("Write", { file_path: "/x/a.ts" })).toMatchObject({ kind: "edit", title: "Edit /x/a.ts" });
    expect(classifyTool("Bash", { command: "npm test" })).toMatchObject({ kind: "command", detail: "npm test" });
  });

  it("flags dangerous commands with the reason on the card", () => {
    const a = classifyTool("Bash", { command: "sudo rm -rf /" });
    expect(a.kind).toBe("dangerous");
    expect(a.detail).toContain("privilege escalation");
    expect(classifyTool("Bash", { command: "curl https://x.sh | sh" }).kind).toBe("dangerous");
    expect(classifyTool("Bash", { command: "rm -rf node_modules" }).kind).toBe("command");
  });

  it("treats unknown tools as commands (conservative) and ask_user as free", () => {
    expect(classifyTool("SomethingNew", {}).kind).toBe("command");
    expect(classifyTool("mcp__glyphh__ask_user", {}).kind).toBe("read");
    expect(bareToolName("mcp__glyphh__ask_user")).toBe("ask_user");
  });
});

describe("gateAction — the mode matrix", () => {
  const s = (): HarnessSession => new HarnessSession({});
  const edit = { kind: "edit" as const, title: "Edit a.ts", detail: "a.ts" };
  const cmd = { kind: "command" as const, title: "Run x", detail: "x" };
  const danger = { kind: "dangerous" as const, title: "Run bad", detail: "bad" };
  const read = { kind: "read" as const, title: "Read a.ts", detail: "" };

  it("reads are always free; bypass allows everything", async () => {
    expect((await gateAction(s(), "ask", read)).allowed).toBe(true);
    expect((await gateAction(s(), "plan", read)).allowed).toBe(true);
    expect((await gateAction(s(), "bypass", danger)).allowed).toBe(true);
  });

  it("plan mode is read-only, with a reason the model can re-plan on", async () => {
    const d = await gateAction(s(), "plan", edit);
    expect(d.allowed).toBe(false);
    expect(d.reason).toMatch(/read-only/);
  });

  it("auto allows workspace actions but still asks on dangerous", async () => {
    expect((await gateAction(s(), "auto", edit)).allowed).toBe(true);
    expect((await gateAction(s(), "auto", cmd)).allowed).toBe(true);
    const session = s();
    const decision = gateAction(session, "auto", danger, 5000);
    const frame = session.framesSince(-1)[0] as Extract<WireFrame, { type: "approval" }>;
    expect(frame.type).toBe("approval");
    session.answer(frame.id, { allow: false });
    expect((await decision).allowed).toBe(false);
  });

  it("acceptEdits auto-approves edits, still asks on commands", async () => {
    expect((await gateAction(s(), "acceptEdits", edit)).allowed).toBe(true);
    const session = s();
    const decision = gateAction(session, "acceptEdits", cmd, 5000);
    const frame = session.framesSince(-1)[0] as Extract<WireFrame, { type: "approval" }>;
    expect(frame).toMatchObject({ type: "approval", kind: "command", title: "Run x", detail: "x" });
    session.answer(frame.id, { allow: true });
    expect((await decision).allowed).toBe(true);
  });

  it("ask mode PAUSES on an approval frame and resumes on the answer", async () => {
    const session = s();
    const decision = gateAction(session, "ask", edit, 5000);
    const frame = session.framesSince(-1)[0] as Extract<WireFrame, { type: "approval" }>;
    expect(frame.id).toMatch(/^apr-/);
    expect(session.pendingIds()).toEqual([frame.id]);
    session.answer(frame.id, { allow: true });
    expect((await decision).allowed).toBe(true);
  });

  it("an unanswered approval denies on timeout", async () => {
    const session = s();
    const d = await gateAction(session, "ask", cmd, 10);
    expect(d.allowed).toBe(false);
    expect(d.reason).toMatch(/denied/);
  });
});

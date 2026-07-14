/**
 * The TUI session engine + renderer. The interactive readline skin (shell.ts) is not
 * unit-tested; its logic (turn/command/streaming/rendering) is these pure pieces.
 */

import { describe, it, expect } from "vitest";

import { InProcessStore } from "../../src/exec/store.js";
import { Session, loadRotors, type TurnEvent } from "../../src/tui/session.js";
import { renderEvent, header } from "../../src/tui/render.js";
import type { RotorDocument } from "../../src/types.js";

function newSession(rotor = "base") {
  const rotors = loadRotors("rotors");
  return { session: new Session({ store: new InProcessStore(), workspace: process.cwd(), rotors, rotor }), rotors };
}

describe("loadRotors", () => {
  it("loads the shipped rotors by name", () => {
    const rotors = loadRotors("rotors");
    expect(rotors.has("base")).toBe(true);
    expect(rotors.size).toBeGreaterThanOrEqual(1);
  });
  it("returns an empty map for a missing directory (no throw)", () => {
    expect(loadRotors("does/not/exist").size).toBe(0);
  });
});

describe("Session.turn streams the loop", () => {
  it("runs the selected rotor and emits step events then an answer", async () => {
    const { session } = newSession();
    const events: TurnEvent[] = [];
    const result = await session.turn("Where does Ada live?", (e) => events.push(e));

    expect(result?.status).toBe("ok");
    const stepIds = events.filter((e) => e.kind === "step").map((e) => (e as { step_id: string }).step_id);
    // The base rotor's loop streamed step-by-step, in order.
    expect(stepIds).toEqual(["ask", "plan", "execute", "test", "deliver"]);
    expect(events.some((e) => e.kind === "answer")).toBe(true);
  });

  it("records the prompt into memory (recall works across the session)", async () => {
    const store = new InProcessStore();
    const session = new Session({ store, workspace: process.cwd(), rotors: loadRotors("rotors"), rotor: "base" });
    await session.turn("the config parser is called parseConfig", () => {});
    expect((await store.turns()).join(" ")).toMatch(/parseConfig/);
  });
});

describe("Session.command", () => {
  it("switches rotor, sets model, and reports status", () => {
    const { session, rotors } = newSession();
    expect(session.command("/model gpt-x").message).toMatch(/gpt-x/);
    expect(session.model).toBe("gpt-x");

    const other = [...rotors.keys()].find((n) => n !== "base");
    if (other) {
      expect(session.command(`/rotor ${other}`).message).toMatch(new RegExp(other));
      expect(session.rotorName).toBe(other);
    }
    expect(session.command("/rotor nope").message).toMatch(/unknown rotor/);
    expect(session.command("/status").message).toMatch(/rotor=/);
    expect(session.command("/rotors").message).toBeTruthy();
    expect(session.command("/help").message).toMatch(/\/rotor/);
    expect(session.command("/bogus").message).toMatch(/unknown command/);
    expect(session.command("/exit")).toMatchObject({ quit: true });
  });

  it("exposes the rotor's declared mode", () => {
    const { session } = newSession();
    expect(["chat", "cowork", "code"]).toContain(session.mode);
  });
});

describe("Session — human-in-the-loop resume", () => {
  const approver = {
    apiVersion: "rotor.glyphh.ai/v0.1",
    kind: "Rotor",
    metadata: { name: "approver", version: "0.1.0", labels: { mode: "chat" } },
    spec: {
      entry: "a",
      steps: [
        { id: "a", type: "transform", in: {}, out: {}, config: { set: { step: "a" } }, next: "gate" },
        { id: "gate", type: "wait", in: {}, out: {}, config: { on: "approval", on_timeout: "fail" }, next: "done" },
        { id: "done", type: "transform", in: {}, out: {}, config: { set: { final: "complete" } }, next: "end" },
      ],
    },
  } as unknown as RotorDocument;

  it("pauses at a wait step, then resumes to completion", async () => {
    const store = new InProcessStore();
    const session = new Session({ store, workspace: process.cwd(), rotors: new Map([["approver", approver]]), rotor: "approver" });
    const events: TurnEvent[] = [];
    await session.turn("please do the thing", (e) => events.push(e));
    expect(events.some((e) => e.kind === "interrupt")).toBe(true);
    expect(session.awaiting).toBe(true);

    const resumed = await session.resume({ decision: "approve" }, (e) => events.push(e));
    expect(resumed?.status).toBe("ok");
    expect(session.awaiting).toBe(false);
  });
});

describe("renderEvent", () => {
  it("renders each event kind as plain text when color is off", () => {
    expect(renderEvent({ kind: "step", step_id: "ask", type: "prompt", status: "ok", frames: ["done"] }, { color: false })).toMatch(/ask.*ok.*done/);
    expect(renderEvent({ kind: "answer", text: "hello" }, { color: false })).toBe("hello");
    expect(renderEvent({ kind: "answer", text: "" }, { color: false })).toMatch(/no answer/);
    expect(renderEvent({ kind: "interrupt", step_id: "gate", awaiting: null }, { color: false })).toMatch(/awaiting approval/);
    expect(renderEvent({ kind: "error", code: "E_TOOL", detail: "boom", remediation: "fix it" }, { color: false })).toMatch(/E_TOOL.*boom/s);
    expect(renderEvent({ kind: "info", text: "hi" }, { color: false })).toBe("hi");
    expect(header({ rotor: "base", mode: "code", model: "auto" }, false)).toMatch(/base.*code.*auto/);
  });
});

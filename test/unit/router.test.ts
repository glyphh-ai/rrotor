/**
 * The router rotor + bundled resolver: one chat surface dispatching to domain
 * rotors as sub-rotor calls. On the stub lane the route never parses (the
 * model echoes prose, and the bare-word pattern matches only a class alone on
 * a line), so the branch takes its default — conversation, the lane that
 * cannot mutate anything — and the child's answer projects to outputs.answer.
 */

import { describe, it, expect } from "vitest";

import { bundledRotorResolver } from "../../src/rotors.js";
import { runInProcess } from "../../src/embed.js";
import type { WireEvent } from "../../src/transport/events.js";

describe("bundledRotorResolver", () => {
  it("resolves bare and namespaced bundled names, validated", () => {
    expect(bundledRotorResolver("code")?.metadata.name).toBe("code");
    expect(bundledRotorResolver("glyphh/base-memory")?.metadata.name).toBe("base-memory");
  });

  it("unknown or path-shaped refs resolve to undefined", () => {
    expect(bundledRotorResolver("no-such-rotor")).toBeUndefined();
    expect(bundledRotorResolver("../etc/passwd")).toBeUndefined();
  });
});

describe("router rotor", () => {
  it("defaults an unparseable route to the chat lane and projects its answer", async () => {
    const doc = bundledRotorResolver("router")!;
    const events: WireEvent[] = [];
    await runInProcess(doc, { prompt: "hello there" }, (ev) => events.push(ev));

    const steps = events.filter((e) => e.kind === "step").map((e) => (e as { step_id: string }).step_id);
    expect(steps).toContain("subchat");
    expect(steps).not.toContain("subcode");

    const done = events.find((e) => e.kind === "done") as { status: string; outputs: { answer?: string } };
    expect(done.status).toBe("ok");
    expect(done.outputs.answer).toBeTruthy();
  });
});

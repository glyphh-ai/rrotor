/**
 * Host tool-pack injection through the embed face. A product wrapping the
 * runtime (desktop, CLI) defines its own tools with `defineTool`/`definePack`
 * and passes them to `runInProcess({tools})`; a rotor's `tool` step dispatches
 * them by name exactly like a stdlib tool, still gated by the rotor's
 * permission mode.
 */

import { describe, it, expect } from "vitest";

import { runInProcess } from "../../src/embed.js";
import { defineTool, definePack } from "../../src/tools/index.js";
import type { WireEvent } from "../../src/transport/events.js";

const HOST_PACK = definePack("desktop", "1.0.0", [
  defineTool({
    name: "desktop.open_window",
    version: 1,
    description: "Open an app window in the host desktop.",
    effect: "mutating",
    grants: ["cowork.write"],
    input: { type: "object", properties: { app: { type: "string" } } },
    handler: async (args) => ({ opened: args.app }),
  }),
]);

/** A minimal rotor whose single step calls the host tool. `mode: cowork` grants
 *  `cowork.write`; `mode: chat` (read-only) must NOT see the tool. */
const rotorWithMode = (mode: string): string => `
apiVersion: rotor.glyphh.ai/v0.1
kind: Rotor
metadata:
  name: host-tool-demo
  version: 1.0.0
  namespace: test
  labels: { mode: "${mode}" }
spec:
  inputs:
    - { name: app, type: string, required: true }
  entry: open
  steps:
    - id: open
      type: tool
      in: { app: $.inputs.app }
      out: { result: object }
      config: { flavor: app, name: desktop.open_window }
      next: end
  outputs:
    - { name: result, from: $.steps.open.result }
`;

async function run(mode: string): Promise<WireEvent[]> {
  const events: WireEvent[] = [];
  await runInProcess(rotorWithMode(mode), { app: "browser" }, (ev) => events.push(ev), {
    tools: [HOST_PACK],
  });
  return events;
}

describe("host tool packs via runInProcess", () => {
  it("a rotor tool step dispatches a host-defined tool by name", async () => {
    const events = await run("cowork");
    const done = events.find((e) => e.kind === "done");
    expect(done).toMatchObject({ status: "ok" });
    expect((done as { outputs: { result?: unknown } }).outputs.result).toEqual({ opened: "browser" });
  });

  it("the rotor's permission mode still gates host tools (chat mode: no mutating tools)", async () => {
    const events = await run("chat");
    const step = events.find((e) => e.kind === "step" && e.step_id === "open");
    expect(step).toMatchObject({ status: "failed" });
  });
});

describe("per-child tool re-gating (§13.4)", () => {
  const CHILD = `
apiVersion: rotor.glyphh.ai/v0.1
kind: Rotor
metadata:
  name: child
  version: 1.0.0
  namespace: test
  labels: { mode: cowork }
spec:
  inputs:
    - { name: app, type: string, required: true }
  entry: open
  steps:
    - id: open
      type: tool
      in: { app: $.inputs.app }
      out: { result: object }
      config: { flavor: app, name: desktop.open_window }
      next: end
  outputs:
    - { name: result, from: $.steps.open.result }
`;

  const PARENT = `
apiVersion: rotor.glyphh.ai/v0.1
kind: Rotor
metadata:
  name: parent
  version: 1.0.0
  namespace: test
  labels: { mode: chat }
spec:
  inputs:
    - { name: app, type: string, required: true }
  entry: dispatch
  steps:
    - id: dispatch
      type: sub-rotor
      out: { result: object }
      config:
        ref: child
        mode: call
        inputs: { app: $.inputs.app }
      next: end
  outputs:
    - { name: result, from: $.steps.dispatch.result }
`;

  it("a chat-mode parent's cowork child gets ITS OWN gated tool surface", async () => {
    const { parseRotor } = await import("../../src/parser/index.js");
    const childDoc = parseRotor(CHILD);
    const events: WireEvent[] = [];
    await runInProcess(PARENT, { app: "browser" }, (ev) => events.push(ev), {
      tools: [HOST_PACK],
      rotors: (ref) => (ref === "child" ? childDoc : undefined),
    });
    const done = events.find((e) => e.kind === "done") as { status: string; outputs: { result?: unknown } };
    // The parent (chat mode) is denied this mutating tool — the earlier test
    // proves that — but the child re-gates to cowork and succeeds.
    expect(done.status).toBe("ok");
    expect(done.outputs.result).toEqual({ opened: "browser" });
  });
});

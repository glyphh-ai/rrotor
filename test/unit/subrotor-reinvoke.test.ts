/**
 * Sub-rotor re-invocation (§7.19 × §16.3): the child run is addressed by the
 * caller's run AND the resolved inputs. Re-invoking the same ref with NEW
 * inputs inside one parent run (the conductor's revise round) must execute
 * fresh — not replay the first invocation's tape. And a composed prompt that
 * hits its `max_tokens` cap must say so with a visible degrade frame, never
 * truncate silently (the reviewer once judged a build it was never shown).
 */

import { describe, it, expect } from "vitest";

import { runInProcess } from "../../src/embed.js";
import { parseRotor } from "../../src/parser/index.js";
import type { WireEvent } from "../../src/transport/events.js";

const CHILD = `
apiVersion: rotor.glyphh.ai/v0.1
kind: Rotor
metadata:
  name: echo
  version: 1.0.0
  namespace: test
  labels: { mode: chat }
spec:
  inputs:
    - { name: x, type: string, required: true }
  entry: say
  steps:
    - id: say
      type: prompt
      in: { x: $.inputs.x }
      out: { text: string }
      config: { template: "got {{x}}" }
      next: end
  outputs:
    - { name: text, from: $.steps.say.text }
`;

const PARENT = `
apiVersion: rotor.glyphh.ai/v0.1
kind: Rotor
metadata:
  name: twice
  version: 1.0.0
  namespace: test
  labels: { mode: chat }
spec:
  inputs:
    - { name: a, type: string, required: true }
    - { name: b, type: string, required: true }
  entry: c1
  steps:
    - id: c1
      type: sub-rotor
      out: { text: string }
      config: { ref: test/echo, mode: call, inputs: { x: $.inputs.a } }
      next: c2
    - id: c2
      type: sub-rotor
      out: { text: string }
      config: { ref: test/echo, mode: call, inputs: { x: $.inputs.b } }
      next: end
  outputs:
    - { name: first, from: $.steps.c1.text }
    - { name: second, from: $.steps.c2.text }
`;

describe("sub-rotor re-invocation with new inputs", () => {
  it("the second call with different inputs runs fresh, not a replay of the first", async () => {
    const events: WireEvent[] = [];
    await runInProcess(parseRotor(PARENT), { a: "one", b: "two" }, (ev) => events.push(ev), {
      rotors: (ref) => (ref === "test/echo" ? parseRotor(CHILD) : undefined),
    });

    const done = events.find((e) => e.kind === "done") as {
      status: string;
      outputs: { first?: string; second?: string };
    };
    expect(done.status).toBe("ok");
    expect(done.outputs.first).toBe("got one");
    // The regression: both invocations shared one child run id, so the second
    // replayed the first's tape and answered "got one".
    expect(done.outputs.second).toBe("got two");

    // Both child executions actually streamed — no silent zero-step replay.
    const says = events.filter((e) => e.kind === "step" && (e as { step_id: string }).step_id === "say");
    expect(says.length).toBe(2);
  });
});

const CAPPED = `
apiVersion: rotor.glyphh.ai/v0.1
kind: Rotor
metadata:
  name: capped
  version: 1.0.0
  namespace: test
  labels: { mode: chat }
spec:
  inputs:
    - { name: body, type: string, required: true }
  entry: compose
  steps:
    - id: compose
      type: prompt
      in: { body: $.inputs.body }
      out: { text: string }
      config:
        template: "doc: {{body}}"
        max_tokens: 4
      next: end
  outputs:
    - { name: text, from: $.steps.compose.text }
`;

describe("prompt max_tokens cap", () => {
  it("truncation is loud: a degrade frame names the cut", async () => {
    const events: WireEvent[] = [];
    await runInProcess(parseRotor(CAPPED), { body: "x".repeat(400) }, (ev) => events.push(ev));

    const step = events.find(
      (e) => e.kind === "step" && (e as { step_id: string }).step_id === "compose",
    ) as { frames: string[]; output?: { text?: string; lane_notes?: string[] } };
    expect(step.frames).toContain("degrade");
    expect(String(step.output?.lane_notes?.[0])).toMatch(/truncated/);
    expect(step.output?.text?.length).toBeLessThan(60);
  });
});

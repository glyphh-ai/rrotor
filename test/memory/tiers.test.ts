/**
 * Retention tiers (docs/memory.md): short-term memory does NOT span sessions,
 * long-term does forever, mid-term survives a session-count window. Deterministic —
 * session distance is measured in ordinals, never wall-clock.
 */

import { describe, it, expect } from "vitest";

import { InProcessStore } from "../../src/exec/store.js";
import { createStator } from "../../src/exec/stator.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { absorbText } from "../../src/handlers/memory.js";
import { assembleRecall } from "../../src/exec/recall.js";
import { execute } from "../../src/exec/executor.js";
import type { Stator } from "../../src/exec/store.js";
import type { RotorDocument } from "../../src/types.js";

const doc = (spec: Record<string, unknown>): RotorDocument =>
  ({ apiVersion: "rotor.glyphh.ai/v0.1", kind: "Rotor", metadata: { name: "t", version: "0.1.0" }, spec }) as unknown as RotorDocument;

function mem(store: Stator) {
  return buildBasicPlugins({ store }).memory;
}
const fillers = (facts: { filler: string }[]) => facts.map((f) => f.filler);

describe("tier scoping across sessions", () => {
  it("short stays in its session; mid + long cross into the next", () => {
    const store = new InProcessStore();
    const m = mem(store);
    store.touchSession("s1"); // ordinal 0
    m.write([{ entity: "user", role: "note", filler: "focus on pool.ts" }], { session: "s1", tier: "short" });
    m.write([{ entity: "user", role: "topic", filler: "auth flow" }], { session: "s1", tier: "mid" });
    m.write([{ entity: "user", role: "pref", filler: "tabs" }], { session: "s1", tier: "long" });

    // Within s1: all three visible.
    expect(fillers(m.recall({ session: "s1" }))).toEqual(expect.arrayContaining(["focus on pool.ts", "auth flow", "tabs"]));

    // A new session s2: long + mid cross over; the s1 short-term note does NOT.
    store.touchSession("s2"); // ordinal 1
    const s2 = fillers(m.recall({ session: "s2", midWindow: 5 }));
    expect(s2).toContain("tabs"); // long
    expect(s2).toContain("auth flow"); // mid, 1 session away
    expect(s2).not.toContain("focus on pool.ts"); // short — belongs to s1
  });

  it("mid falls out of the window; long persists forever", () => {
    const store = new InProcessStore();
    const m = mem(store);
    store.touchSession("s1");
    m.write([{ entity: "user", role: "topic", filler: "auth flow" }], { session: "s1", tier: "mid" });
    m.write([{ entity: "user", role: "pref", filler: "tabs" }], { session: "s1", tier: "long" });

    // Advance well past the window.
    for (const s of ["s2", "s3", "s4", "s5", "s6", "s7", "s8"]) store.touchSession(s);
    const far = fillers(m.recall({ session: "s8", midWindow: 5 })); // 8 sessions from s1
    expect(far).toContain("tabs"); // long survives
    expect(far).not.toContain("auth flow"); // mid aged out
  });
});

describe("default tiers from absorb", () => {
  it("directives are long (recalled in a later session); task facts are mid", () => {
    const store = new InProcessStore();
    const m = mem(store);
    store.touchSession("s1");
    m.write(absorbText("Always use tabs.", "user"), { session: "s1" }); // directive → long
    m.write(absorbText("Ada's role is engineer.", "user"), { session: "s1" }); // task fact → mid

    store.touchSession("s2");
    const ctx = assembleRecall(m, "", { session: "s2", midWindow: 5 });
    expect(ctx.directives.join(" ")).toMatch(/tabs/i); // long directive crosses sessions
    // The mid task fact is visible one session later...
    expect(fillers(m.recall({ session: "s2", role: "role", midWindow: 5 }))).toContain("engineer");
  });

  it("no session ⇒ everything visible (backward compatible)", () => {
    const store = new InProcessStore();
    const m = mem(store);
    m.write(absorbText("Always cite sources.", "user"), {});
    // No session context → tier filtering is permissive (long always, mid/no-session visible).
    expect(assembleRecall(m, "").directives.join(" ")).toMatch(/cite/i);
  });
});

describe("explicit tier tagging via the write step (deterministic override)", () => {
  it("a write step's explicit tier scopes facts and overrides the enricher default", async () => {
    const store = new InProcessStore();
    store.touchSession("s1");
    // absorb would tag this directive `long`; the step explicitly forces `short`.
    const d = doc({
      entry: "w",
      steps: [{ id: "w", type: "write", in: { text: "Always use tabs." }, out: {}, config: { mode: "absorb", key: "user", tier: "short" }, next: "end" }],
    });
    await execute(d, {}, buildBasicPlugins({ store }), { session: "s1" });

    const m = mem(store);
    // Within s1 the directive is visible...
    expect(m.recall({ session: "s1" }).map((f) => f.tier)).toContain("short");
    // ...but a new session does NOT see it — the explicit `short` won over `long`.
    store.touchSession("s2");
    expect(assembleRecall(m, "", { session: "s2" }).directives.join(" ")).not.toMatch(/tabs/i);
  });

  it("the run's session stamps absorbed facts so short-tier stays put", async () => {
    const store = new InProcessStore();
    store.touchSession("s1");
    const d = doc({
      entry: "w",
      steps: [{ id: "w", type: "write", in: { text: "The build uses vitest." }, out: {}, config: { mode: "absorb", tier: "short" }, next: "end" }],
    });
    await execute(d, {}, buildBasicPlugins({ store }), { session: "s1" });
    const m = mem(store);
    store.touchSession("s2");
    // short-tier fact belongs to s1 only.
    expect(fillers(m.recall({ session: "s1" })).join(" ")).toMatch(/vitest/i);
    expect(fillers(m.recall({ session: "s2" })).join(" ")).not.toMatch(/vitest/i);
  });
});

describe("SQLite backend parity for tiers", () => {
  it("scopes tiers identically on the durable store", () => {
    const store = createStator({ backend: "sqlite" });
    const m = mem(store);
    store.touchSession("s1");
    m.write([{ entity: "user", role: "note", filler: "ephemeral" }], { session: "s1", tier: "short" });
    m.write([{ entity: "user", role: "pref", filler: "tabs" }], { session: "s1", tier: "long" });
    store.touchSession("s2");
    const s2 = fillers(m.recall({ session: "s2" }));
    expect(s2).toContain("tabs");
    expect(s2).not.toContain("ephemeral");
    store.close?.();
  });
});

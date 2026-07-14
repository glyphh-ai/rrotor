/**
 * Pool + affinity tests (BUILD_PLAN.md Phase 11, SPEC §17.2–§17.4). Routing honors
 * affinity, warming stays within the `maxHot` budget, and — the load-bearing
 * invariant — instance state is telemetry that NEVER changes what a run computes
 * (§17.6).
 */

import { describe, it, expect } from "vitest";

import { BasicPool } from "../../src/plugins/pool.js";
import { execute } from "../../src/exec/executor.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { shapeOf } from "../harness/replay.js";
import { loadFixture, defaultInputs } from "../harness/fixtures.js";

describe("affinity routing (§17.4)", () => {
  it("routes the same affinity key back to the same instance", () => {
    const pool = new BasicPool({ maxHot: 4 });
    const a1 = pool.route({ key: "tenant:A" });
    const b = pool.route({ key: "tenant:B" });
    const a2 = pool.route({ key: "tenant:A" });
    expect(a1).toBe(a2);
    expect(b).not.toBe(a1);
    expect(pool.instanceState(a1)).toBe("hot");
  });
});

describe("warming budget (§17.3)", () => {
  it("never provisions more than maxHot instances", () => {
    const pool = new BasicPool({ maxHot: 2 });
    for (const k of ["a", "b", "c", "d", "e"]) pool.route({ key: k });
    expect(pool.snapshot().length).toBeLessThanOrEqual(2);
    // The first two keys keep their dedicated instances; later keys share.
    expect(pool.route({ key: "a" })).toBe("local-0");
    expect(pool.route({ key: "b" })).toBe("local-1");
  });

  it("pre-warms up to minHot, bounded by maxHot", () => {
    const pool = new BasicPool({ minHot: 3, maxHot: 5 });
    pool.provision();
    expect(pool.snapshot().length).toBe(3);
    expect(pool.snapshot().every((i) => i.state === "warm")).toBe(true);
    pool.provision({ minHot: 99 }); // capped at maxHot
    expect(pool.snapshot().length).toBe(5);
  });
});

describe("determinism neutrality (§17.6)", () => {
  it("instance state never changes what a run computes", async () => {
    const doc = loadFixture("rotors/base.rotor.yaml");
    const inputs = defaultInputs(doc);
    const plugins = buildBasicPlugins();

    const before = shapeOf(await execute(doc, inputs, plugins));
    // Heavily exercise the pool — provision, route many keys, flip states.
    plugins.pool.provision({ minHot: 4 });
    for (const k of ["x", "y", "z", "w"]) plugins.pool.route({ key: k, mode: "require" });
    const after = shapeOf(await execute(doc, inputs, plugins));

    expect(after).toEqual(before);
  });
});

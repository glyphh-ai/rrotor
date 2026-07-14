/**
 * Backend parity (BUILD_PLAN.md Phase 2). The golden replay guardrail must hold
 * identically on the in-process AND the durable SQLite backend, and a run's shape
 * must be byte-identical across the two — proving the SQLite store is a faithful,
 * determinism-preserving swap-in behind the same Stator interface.
 */

import { describe, it, expect } from "vitest";

import { InProcessStore } from "../../src/exec/store.js";
import { createStator } from "../../src/exec/stator.js";
import { checkDeterminism, checkReplay, runFresh, shapeOf, type StoreFactory } from "../harness/replay.js";
import { REFERENCE_ROTORS, loadFixture, defaultInputs } from "../harness/fixtures.js";

const BACKENDS: Array<{ name: string; factory: StoreFactory }> = [
  { name: "memory", factory: () => new InProcessStore() },
  { name: "sqlite", factory: () => createStator({ backend: "sqlite" }) },
];

for (const backend of BACKENDS) {
  describe(`reference rotors on the ${backend.name} backend`, () => {
    for (const relPath of REFERENCE_ROTORS) {
      const doc = loadFixture(relPath);
      const inputs = defaultInputs(doc);

      it(`${relPath} runs deterministically`, async () => {
        const { a, b, identical } = await checkDeterminism(doc, inputs, {}, backend.factory);
        expect(b).toEqual(a);
        expect(identical).toBe(true);
      });

      it(`${relPath} replays without re-executing`, async () => {
        const { first, replay, appendedDuringReplay } = await checkReplay(doc, inputs, {}, backend.factory);
        expect(replay).toEqual(first);
        expect(appendedDuringReplay).toBe(0);
      });
    }
  });
}

describe("cross-backend parity", () => {
  for (const relPath of REFERENCE_ROTORS) {
    it(`${relPath} produces an identical run shape on memory and sqlite`, async () => {
      const doc = loadFixture(relPath);
      const inputs = defaultInputs(doc);
      const mem = shapeOf(await runFresh(doc, inputs, {}, () => new InProcessStore()));
      const sql = shapeOf(await runFresh(doc, inputs, {}, () => createStator({ backend: "sqlite" })));
      expect(sql).toEqual(mem);
    });
  }
});

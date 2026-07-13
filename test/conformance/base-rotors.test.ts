/**
 * Conformance smoke suite over the shipped reference rotors.
 *
 * This pins the current happy path BEFORE any later phase changes runtime
 * behavior: every reference rotor must validate, execute to a terminal, run
 * deterministically, and replay from its recorded history without re-executing.
 * If a later phase breaks determinism or replay, this suite goes red first.
 */

import { describe, it, expect } from "vitest";

import { validateRotor } from "../../src/parser/index.js";
import { checkDeterminism, checkReplay } from "../harness/replay.js";
import { REFERENCE_ROTORS, loadFixture, defaultInputs } from "../harness/fixtures.js";

describe("reference rotors — conformance", () => {
  for (const relPath of REFERENCE_ROTORS) {
    describe(relPath, () => {
      const doc = loadFixture(relPath);
      const inputs = defaultInputs(doc);

      it("validates against schema + static graph checks", () => {
        const { valid, errors } = validateRotor(doc);
        expect(errors).toEqual([]);
        expect(valid).toBe(true);
      });

      it("executes to a terminal with a non-empty event history", async () => {
        const { a } = await checkDeterminism(doc, inputs);
        expect(a.history.length).toBeGreaterThan(0);
        expect(["ok", "refused", "failed", "interrupted"]).toContain(a.status);
        expect(typeof a.terminal).toBe("string");
      });

      it("runs deterministically (two fresh runs are byte-identical)", async () => {
        const { a, b, identical } = await checkDeterminism(doc, inputs);
        // Surface the first divergence in the failure message if any.
        expect(b).toEqual(a);
        expect(identical).toBe(true);
      });

      it("replays from recorded history without re-executing", async () => {
        const { first, replay, appendedDuringReplay, identical } = await checkReplay(doc, inputs);
        expect(replay).toEqual(first);
        expect(identical).toBe(true);
        // Pure replay appends nothing to the stator (SPEC.md §5.4).
        expect(appendedDuringReplay).toBe(0);
      });
    });
  }
});

/**
 * Parser / validation tests. Rather than hand-roll a schema-perfect document,
 * these start from a known-valid reference rotor and mutate it, so they exercise
 * the static graph checks (dangling edges, duplicate ids) without coupling to the
 * full JSON Schema surface.
 */

import { describe, it, expect } from "vitest";

import { validateRotor } from "../../src/parser/index.js";
import { loadFixture } from "../harness/fixtures.js";

describe("validateRotor", () => {
  it("accepts a shipped reference rotor", () => {
    const { valid, errors } = validateRotor(loadFixture("rotors/base.rotor.yaml"));
    expect(errors).toEqual([]);
    expect(valid).toBe(true);
  });

  it("rejects a dangling `next` edge", () => {
    const bad = structuredClone(loadFixture("rotors/base.rotor.yaml"));
    bad.spec.steps[0].next = "does-not-exist";
    const { valid, errors } = validateRotor(bad);
    expect(valid).toBe(false);
    expect(errors.length).toBeGreaterThan(0);
    expect(errors.some((e) => JSON.stringify(e).includes("does-not-exist"))).toBe(true);
  });

  it("rejects duplicate step ids", () => {
    const bad = structuredClone(loadFixture("rotors/base.rotor.yaml"));
    bad.spec.steps.push(structuredClone(bad.spec.steps[0]));
    const { valid, errors } = validateRotor(bad);
    expect(valid).toBe(false);
    expect(errors.length).toBeGreaterThan(0);
  });
});

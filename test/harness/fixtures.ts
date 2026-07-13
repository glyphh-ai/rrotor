/**
 * Fixture loader for the reference rotors shipped in `rotors/`. Using the real
 * shipped documents as fixtures means the smoke/determinism suite pins the exact
 * artifacts users run, not synthetic stand-ins.
 */

import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { loadRotor } from "../../src/parser/index.js";
import type { RotorDocument } from "../../src/types.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

export const REFERENCE_ROTORS = [
  "rotors/base.rotor.yaml",
  "rotors/web-dev.rotor.yaml",
  "rotors/corp-data-slim.rotor.yaml",
] as const;

export function loadFixture(relPath: string): RotorDocument {
  return loadRotor(resolve(ROOT, relPath));
}

/**
 * Fill every required input with a deterministic placeholder (mirrors the CLI's
 * `fillRequired`), so a rotor reaches a terminal without the caller needing to
 * know each document's input schema. Determinism only requires the two runs use
 * the same inputs — which they do, being derived purely from the document.
 */
export function defaultInputs(doc: RotorDocument): Record<string, unknown> {
  const inputs: Record<string, unknown> = {};
  for (const p of doc.spec.inputs ?? []) {
    if (p.required && p.default === undefined) inputs[p.name] = `<${p.name}>`;
  }
  return inputs;
}

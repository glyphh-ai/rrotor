/**
 * REPL command tests (BUILD_PLAN.md Phase 1). `execCommand` is the TTY-free core
 * of the REPL; these prove `validate`/`run` do real work rather than printing the
 * old "lands next build phase" placeholders.
 */

import { describe, it, expect } from "vitest";

import { execCommand } from "../../src/repl.js";
import { Runtime } from "../../src/runtime/runtime.js";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const BASE = resolve(ROOT, "rotors/base.rotor.yaml");

const text = (lines: string[]) => lines.join("\n");

describe("REPL execCommand", () => {
  it("validate reports a real verdict for a valid rotor", async () => {
    const out = text(await execCommand(new Runtime(), "validate", BASE));
    expect(out).toContain("valid RotorSpec document");
    expect(out).not.toContain("lands next build phase");
  });

  it("validate surfaces load errors for a missing file", async () => {
    const out = text(await execCommand(new Runtime(), "validate", "/no/such/file.yaml"));
    expect(out.toLowerCase()).toContain("cannot load");
  });

  it("run actually executes and reports run id + status", async () => {
    const out = text(await execCommand(new Runtime(), "run", `${BASE} prompt=hello`));
    expect(out).toMatch(/run run-[0-9a-f]/);
    expect(out).toMatch(/status (ok|refused|failed|interrupted)/);
    expect(out).not.toContain("lands next build phase");
  });

  it("status prints the capability manifest", async () => {
    const out = text(await execCommand(new Runtime(), "status", ""));
    expect(out).toContain("grounding");
    expect(out).toContain("memory");
  });

  it("unknown command hints at help", async () => {
    const out = text(await execCommand(new Runtime(), "frobnicate", ""));
    expect(out).toContain("unknown");
  });
});

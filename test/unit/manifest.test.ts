/**
 * rotorManifest — the client config contract. A product reads it to know what
 * to bind (model roles), grant (permission mode), and collect (inputs) before
 * running a rotor.
 */

import { describe, it, expect } from "vitest";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { rotorManifest } from "../../src/manifest.js";
import { loadRotor } from "../../src/parser/index.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const rotor = (name: string) => loadRotor(resolve(ROOT, `rotors/${name}.rotor.yaml`));

describe("rotorManifest", () => {
  it("base-single: one assistant role on the local lane, chat-default mode, no tools", () => {
    const m = rotorManifest(rotor("base-single"));
    expect(m.rotor).toBe("glyphh/base@1.0.0");
    expect(m.roles).toEqual([{ role: "assistant", lane: "local", steps: ["respond"] }]);
    expect(m.tools).toEqual([]);
    expect(m.inputs.map((i) => i.name)).toEqual(["prompt", "entity"]);
    expect(m.inputs[0].required).toBe(true);
  });

  it("code (conductor): frontier planner only; build delegated to the worker", () => {
    const m = rotorManifest(rotor("code"));
    expect(m.mode).toBe("code");
    expect(m.roles.map((r) => r.role)).toEqual(["planner"]);
    expect(m.roles[0].lane).toBe("frontier");
    expect(m.tools.map((t) => t.name).sort()).toEqual(["file.read", "file.write"]);
    expect(m.inputs.filter((i) => i.required).map((i) => i.name)).toEqual(["task"]);
  });

  it("build (worker): local coder + the mechanical workbench", () => {
    const m = rotorManifest(rotor("build"));
    expect(m.roles.map((r) => r.role)).toEqual(["coder"]);
    expect(m.tools.map((t) => t.name).sort()).toEqual(["file.read", "file.write", "shell.bash"]);
    expect(m.inputs.filter((i) => i.required).map((i) => i.name)).toEqual(["task", "plan_path", "file", "test_cmd"]);
  });

  it("base: surfaces the tool dispatch", () => {
    const m = rotorManifest(rotor("base"));
    expect(m.tools).toEqual([{ name: "query", flavor: "mcp", steps: ["execute"] }]);
  });
});

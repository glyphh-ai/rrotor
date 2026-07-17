/**
 * manifest.ts — what a CLIENT must provide/render to run a rotor.
 *
 * A rotor document declares everything a run needs — model ROLES (bound to
 * endpoints via the control plane's registry), a permission MODE (the tool
 * capability ladder), TOOL dispatches, and typed INPUTS. `rotorManifest`
 * extracts that contract in one pass so a product (CLI, desktop) can render a
 * config surface before running: bind each role, request the mode's grants,
 * collect the inputs. This is the static twin of the wire event stream — the
 * stream tells the client what IS happening, the manifest what WILL BE needed.
 */

import { toolModeFromLabels, type ModeName } from "./tools/index.js";
import type { ModelConfig, RotorDocument, Step, ToolConfig } from "./types.js";

/** One model ROLE the rotor requires; the client binds it to an endpoint. */
export interface ManifestRole {
  /** The role name a `model` step declares (`config.model`) — the registry key. */
  role: string;
  /** The lane the role runs on (`local` free-by-construction; `frontier` metered). */
  lane: "local" | "frontier";
  /** The step ids that call this role. */
  steps: string[];
}

/** One tool dispatch the rotor performs. */
export interface ManifestTool {
  /** The registry method name (`file.write`, `desktop.open_window`, …). */
  name: string;
  flavor?: string;
  steps: string[];
}

export interface RotorManifest {
  /** `namespace/name@version`. */
  rotor: string;
  description?: string;
  /** The permission mode (`metadata.labels.mode`) — the tool capability ladder
   *  the host must grant. */
  mode: ModeName;
  /** Model roles to bind (deduped, in first-use order). Empty ⇒ no model steps. */
  roles: ManifestRole[];
  /** Tool dispatches (deduped by name). Empty ⇒ no tool steps. */
  tools: ManifestTool[];
  /** Declared run inputs, verbatim from the spec. */
  inputs: Array<{ name: string; type?: string; required?: boolean; description?: string }>;
  /** Whether the rotor binds an HDC space (memory/grounding steps present). */
  space: boolean;
}

/** Extract the client-facing config contract from a rotor document. */
export function rotorManifest(doc: RotorDocument): RotorManifest {
  const ns = doc.metadata.namespace ? `${doc.metadata.namespace}/` : "";
  const roles = new Map<string, ManifestRole>();
  const tools = new Map<string, ManifestTool>();

  for (const step of doc.spec.steps as Step[]) {
    if (step.type === "model") {
      const cfg = (step.config ?? {}) as ModelConfig;
      const role = String(cfg.model ?? "assistant");
      const entry = roles.get(role) ?? { role, lane: cfg.lane ?? "local", steps: [] };
      entry.steps.push(step.id);
      roles.set(role, entry);
    }
    if (step.type === "tool") {
      const cfg = (step.config ?? {}) as ToolConfig;
      const name = String(cfg.method ?? cfg.name ?? "");
      if (!name) continue;
      const entry = tools.get(name) ?? { name, ...(cfg.flavor ? { flavor: cfg.flavor } : {}), steps: [] };
      entry.steps.push(step.id);
      tools.set(name, entry);
    }
  }

  return {
    rotor: `${ns}${doc.metadata.name}@${doc.metadata.version}`,
    ...(doc.metadata.description ? { description: doc.metadata.description } : {}),
    mode: toolModeFromLabels(doc.metadata.labels),
    roles: [...roles.values()],
    tools: [...tools.values()],
    inputs: (doc.spec.inputs ?? []).map((i) => ({
      name: i.name,
      ...(i.type ? { type: String(i.type) } : {}),
      ...(i.required !== undefined ? { required: i.required } : {}),
      ...(i.description ? { description: i.description } : {}),
    })),
    space: doc.spec.space !== undefined,
  };
}

/**
 * The BASIC plugin bundle + the `buildBasicPlugins()` factory the executor and
 * CLI use (docs/runtime.md §3). Every seam reports `status().tier === "basic"`
 * and degrades (`ready` stays true, work falls back) rather than raising.
 *
 * The grounding and memory plugins SHARE one {@link InProcessStore} — grounding
 * reads the fact store the memory plugin owns, exactly as the premium tier reads
 * one substrate. Pass your own store to seed facts or share history across runs.
 */

import { InProcessStore, type Stator } from "../exec/store.js";
import { BasicGrounding } from "./grounding.js";
import { BasicMemory } from "./memory.js";
import { BasicModels, type BasicModelsOptions } from "./models.js";
import { BasicConnections } from "./connections.js";
import { BasicGateway } from "./gateway.js";
import { BasicGovernance } from "./governance.js";
import { BasicPool } from "./pool.js";
import { NoopDrain } from "./drain.js";
import type { DrainPlugin, Plugins } from "./interfaces.js";
import { installStdlib, kvFromStore, toolModeFromLabels, type Capability, type ModeName, type ToolPack } from "../tools/index.js";
import type { RotorDocument } from "../types.js";

export * from "./interfaces.js";
export { BasicGrounding } from "./grounding.js";
export { BasicMemory } from "./memory.js";
export { BasicModels, type BasicModelsOptions } from "./models.js";
export { BasicConnections } from "./connections.js";
export { BasicGateway } from "./gateway.js";
export { BasicGovernance } from "./governance.js";
export { BasicPool } from "./pool.js";
export { NoopDrain, HttpDrain, FileDrain, BufferedDrain, drainFromEnv, toEnvelope } from "./drain.js";
export { McpClient, connectMcp, type McpConnection, type McpToolResult, type HeaderProvider } from "./mcp.js";

export interface BuildBasicPluginsOptions {
  /** A pre-seeded / shared stator. Defaults to a fresh {@link InProcessStore}. */
  store?: Stator;
  /** Model lane configuration (local endpoint, timeout, default model). */
  models?: BasicModelsOptions;
  /** A log drain (shared across runs). Defaults to a {@link NoopDrain}. */
  drain?: DrainPlugin;
  /** Install the tool standard library into `connections`, gated by permission
   *  mode. Off by default (bare bundle). `root` is the workspace sandbox; `packs`
   *  adds the host product's own tools behind the same contract + gating. */
  tools?: { root: string; mode?: ModeName; granted?: ReadonlySet<Capability>; packs?: ToolPack[] };
}

/** The eight basic capabilities, wired against one shared in-process stator. */
export function buildBasicPlugins(opts: BuildBasicPluginsOptions = {}): Plugins {
  const store = opts.store ?? new InProcessStore();
  const plugins: Plugins = {
    grounding: new BasicGrounding(store),
    memory: new BasicMemory(store),
    models: new BasicModels(opts.models),
    connections: new BasicConnections(),
    gateway: new BasicGateway(),
    governance: new BasicGovernance(),
    pool: new BasicPool(),
    drain: opts.drain ?? new NoopDrain(),
  };
  if (opts.tools) {
    installStdlib(plugins.connections, {
      root: opts.tools.root,
      memory: plugins.memory,
      kv: kvFromStore(store),
      mode: opts.tools.mode,
      granted: opts.tools.granted,
      packs: opts.tools.packs,
    });
  }
  return plugins;
}

/**
 * Per-child tool re-gating (§13.4 least privilege for sub-rotors). A sub-rotor
 * shares the caller's substrate — stator, memory, grounding, models, gateway,
 * governance — but its TOOL surface must be its own: gated by the CHILD
 * document's `metadata.labels.mode`, not inherited from the parent. This
 * factory rebuilds only the connections registry for the callee; everything
 * else passes through by reference.
 */
export function childPluginsFactory(opts: {
  store: Stator;
  root: string;
  packs?: ToolPack[];
}): (doc: RotorDocument, parent: Plugins) => Plugins {
  return (doc, parent) => {
    const connections = new BasicConnections();
    installStdlib(connections, {
      root: opts.root,
      memory: parent.memory,
      kv: kvFromStore(opts.store),
      mode: toolModeFromLabels(doc.metadata.labels),
      packs: opts.packs,
    });
    return { ...parent, connections };
  };
}

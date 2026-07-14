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
import { installStdlib, kvFromStore, type Capability, type ModeName } from "../tools/index.js";

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
   *  mode. Off by default (bare bundle). `root` is the workspace sandbox. */
  tools?: { root: string; mode?: ModeName; granted?: ReadonlySet<Capability> };
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
    });
  }
  return plugins;
}

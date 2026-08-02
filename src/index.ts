/**
 * rrotor — the open runtime for RotorSpec.
 *
 * Public surface (grows as the runtime is built out).
 */

export { printBanner } from "./banner.js";
export { Runtime } from "./runtime/runtime.js";
export {
  CapabilityRegistry,
  CAPABILITY_NAMES,
  type Capability,
  type CapabilityName,
  type CapabilityStatus,
  type CapabilityTier,
} from "./runtime/registry.js";
export { VERSION } from "./version.js";

// Core types (SPEC.md §2, §4, §5.4, §7).
export * from "./types.js";

// Loader + validator (docs/runtime.md §4.2) — the L1 surface for `validate`.
export {
  loadRotor,
  parseRotor,
  validateRotor,
  getRotorValidator,
  type RotorError,
  type ValidationResult,
} from "./parser/index.js";

// Executor (SPEC.md §5) — the run loop for `run`.
export {
  execute,
  type ExecuteOptions,
  type RunResult,
} from "./exec/executor.js";

// Basic-tier plugin bundle + capability seams (docs/runtime.md §3).
export {
  buildBasicPlugins,
  type BuildBasicPluginsOptions,
} from "./plugins/index.js";

// The model control surface — a control plane injects a role→endpoint registry so each
// model step resolves its bound provider/model/key. Every endpoint speaks the OpenAI wire.
export { BasicModels, type BasicModelsOptions, type ModelEndpoint } from "./plugins/models.js";
export type { Plugins } from "./plugins/interfaces.js";

// HTTP runtime face (SPEC.md §17) — for `serve` and Kubernetes.
export { startServer } from "./server.js";

// In-process runtime face — the embedded twin of `serve` (CLI / desktop link this).
export { runInProcess, openSqliteStator, type EmbedOptions } from "./embed.js";

// The transport-agnostic streaming core both faces sit on, + the wire event type.
export { executeToEvents, resumeToEvents, type StreamContext, type Emit } from "./transport/run.js";
export { WIRE_VERSION, type WireEvent } from "./transport/events.js";

// Stator (durable memory) — factory + backends, so an embedder controls where memory
// lives (the shared local SQLite file, an in-process store, or Postgres/pgvector).
export {
  createStator,
  initStator,
  statorFromEnvAsync,
  type StatorOptions,
  type StatorBackend,
} from "./exec/stator.js";
export {
  HashEmbedder,
  HttpEmbedder,
  embedderFromEnv,
  DEFAULT_EMBED_DIM,
  type Embedder,
  type EmbedBackend,
  type HttpEmbedderOptions,
} from "./exec/embedder.js";
export { SqliteStore } from "./exec/sqlite-store.js";
export { InProcessStore } from "./exec/store.js";
export type { Stator } from "./exec/store.js";

// The tool SDK surface — a host product (desktop, CLI) defines its own tools behind
// the same contract as the stdlib and passes them to `runInProcess({tools})` /
// `executeToEvents(ctx.packs)`; the rotor's permission mode gates them identically.
export {
  defineTool,
  definePack,
  buildStdlib,
  installStdlib,
  toolModeFromLabels,
  MODES,
  CAPABILITIES,
  type ToolSpec,
  type ToolPack,
  type ToolEffect,
  // `Capability` (runtime seam) is taken by runtime/registry — alias the tool grant.
  type Capability as ToolCapability,
  type ModeName,
  type InstallResult,
} from "./tools/index.js";

// The client config contract — what a product must bind/grant/collect to run a
// rotor (model roles, permission mode, tools, inputs). The static twin of the
// wire event stream.
export { rotorManifest, type RotorManifest, type ManifestRole, type ManifestTool } from "./manifest.js";

// The bundled rotor registry — the default `sub-rotor` ref resolver (§7.19).
export { bundledRotorResolver } from "./rotors.js";

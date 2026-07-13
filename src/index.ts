/**
 * OpenRotor — the open runtime for RotorSpec.
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
export type { Plugins } from "./plugins/interfaces.js";

// HTTP runtime face (SPEC.md §17) — for `serve` and Kubernetes.
export { startServer } from "./server.js";

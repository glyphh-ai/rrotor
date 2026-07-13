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

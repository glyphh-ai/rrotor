/**
 * The capability registry — the plugin seam from docs/runtime.md §3.
 *
 * A rotor DECLARES the capabilities it needs; a runtime ADVERTISES what it
 * provides via each capability's `status()`; load-time reconciliation yields
 * satisfied | graceful-degradation | clean-refuse. This is the open-core seam:
 * rrotor ships BASIC implementations; glyphh swaps in PREMIUM ones behind the
 * same interfaces.
 */

export type CapabilityTier = "basic" | "premium" | "none";

export interface CapabilityStatus {
  ready: boolean;
  detail: string;
  tier: CapabilityTier;
}

export interface Capability {
  readonly name: string;
  status(): CapabilityStatus;
}

/** The eight capability seams (docs/runtime.md §3, §3.8). */
export const CAPABILITY_NAMES = [
  "grounding",
  "memory",
  "models",
  "connections",
  "gateway",
  "governance",
  "pool",
  "drain",
] as const;

export type CapabilityName = (typeof CAPABILITY_NAMES)[number];

export class CapabilityRegistry {
  private caps = new Map<string, Capability>();

  register(cap: Capability): void {
    this.caps.set(cap.name, cap);
  }

  get(name: string): Capability | undefined {
    return this.caps.get(name);
  }

  /** The capability manifest a runtime advertises. */
  manifest(): Record<string, CapabilityStatus> {
    const out: Record<string, CapabilityStatus> = {};
    for (const [name, cap] of this.caps) out[name] = cap.status();
    return out;
  }
}

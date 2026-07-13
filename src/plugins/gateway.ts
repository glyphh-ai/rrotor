/**
 * BasicGateway — the bare-box identity-adapter default (docs/runtime.md §3.5).
 * An absent gateway IS this: identity in/out adapters, a table-driven translate
 * that passes internal↔internal through and flags anything else as
 * `E_UNTRANSLATABLE`, no-op prompt-cache lowering, and a simple metering counter.
 * Local calls are unmetered by construction — only frontier usage accrues cost.
 * The gateway sits INSIDE the checkpoint boundary, so its transforms are
 * deterministic and recorded.
 */

import type { CapabilityStatus } from "../runtime/registry.js";
import type { Usage } from "../types.js";
import type { GatewayPlugin, MeterSnapshot } from "./interfaces.js";

export class BasicGateway implements GatewayPlugin {
  readonly name = "gateway";
  private readonly counter: MeterSnapshot = {
    calls: 0,
    input: 0,
    output: 0,
    cache_read: 0,
    cache_write: 0,
    cost: 0,
  };

  status(): CapabilityStatus {
    return { ready: true, detail: "identity adapters; best-effort metering", tier: "basic" };
  }

  inAdapter(wire: unknown): unknown {
    return wire;
  }

  outAdapter(io: unknown): unknown {
    return io;
  }

  translate(from: string, to: string, payload: unknown): unknown {
    if (from === to) return payload;
    // Basic tier only knows the identity translation; a real translation table
    // is the premium swap-in. A missing entry is explicit, never a silent drop.
    if (from === "internal" && to === "internal") return payload;
    throw new Error(`E_UNTRANSLATABLE: ${from} → ${to}`);
  }

  lowerPromptCache(_breakpoints: string[] | undefined, _provider: string): unknown {
    // Unhonorable on a bare box → no-op, never error (§3.5).
    return undefined;
  }

  recordUsage(usage: Usage, lane?: string): void {
    this.counter.calls += 1;
    this.counter.input += usage.input ?? 0;
    this.counter.output += usage.output ?? 0;
    this.counter.cache_read += usage.cache_read ?? 0;
    this.counter.cache_write += usage.cache_write ?? 0;
    // Local is unmetered by construction; only frontier accrues cost.
    if (lane === "frontier") this.counter.cost += usage.cost ?? 0;
  }

  meter(): MeterSnapshot {
    return { ...this.counter };
  }
}

/**
 * BasicGateway — the transport + governance layer (docs/runtime.md §3.5, SPEC §8).
 * It normalizes I/O across the three representations of a turn (§8.3), lowers
 * prompt-cache breakpoints to provider wire forms (§8.6), and meters usage (§8.5).
 * It sits INSIDE the checkpoint boundary, so its transforms are deterministic and
 * recorded.
 *
 * Translation is table-driven via a canonical **internal** form: `toInternal` then
 * `fromInternal`. Any representation the table does not know is a typed
 * `E_UNTRANSLATABLE`, never a silent drop. Prompt caching is determinism-neutral
 * (§17.6): it changes only `usage`, never model output — first sight of a prefix
 * is a write, later sights are hits.
 */

import type { CapabilityStatus } from "../runtime/registry.js";
import type { Usage } from "../types.js";
import { RotorError } from "../errors.js";
import type { GatewayPlugin, MeterSnapshot, PromptCacheResult } from "./interfaces.js";

/** The canonical internal tool-call form the table pivots through. */
interface InternalCall {
  name: string;
  args: Record<string, unknown>;
}

const REPRESENTATIONS = new Set(["internal", "mcp", "provider"]);

function parseArgs(v: unknown): Record<string, unknown> {
  if (v && typeof v === "object") return v as Record<string, unknown>;
  if (typeof v === "string") {
    try {
      return JSON.parse(v) as Record<string, unknown>;
    } catch {
      return {};
    }
  }
  return {};
}

/** Any known representation → the internal form. */
function toInternal(from: string, p: any): InternalCall {
  switch (from) {
    case "internal":
      return { name: String(p?.name ?? ""), args: parseArgs(p?.args) };
    case "mcp":
      return { name: String(p?.name ?? ""), args: parseArgs(p?.arguments) };
    case "provider":
      // OpenAI function-call / Anthropic tool_use shapes.
      return {
        name: String(p?.function?.name ?? p?.name ?? ""),
        args: parseArgs(p?.function?.arguments ?? p?.input),
      };
    default:
      throw untranslatable(from, "internal");
  }
}

/** The internal form → any known representation. */
function fromInternal(to: string, p: InternalCall): unknown {
  switch (to) {
    case "internal":
      return { name: p.name, args: p.args };
    case "mcp":
      return { name: p.name, arguments: p.args };
    case "provider":
      return { function: { name: p.name, arguments: JSON.stringify(p.args) } };
    default:
      throw untranslatable("internal", to);
  }
}

function untranslatable(from: string, to: string): RotorError {
  return new RotorError("E_UNTRANSLATABLE", `${from} → ${to}`, { context: { from, to } });
}

export class BasicGateway implements GatewayPlugin {
  readonly name = "gateway";
  private readonly counter: MeterSnapshot = { calls: 0, input: 0, output: 0, cache_read: 0, cache_write: 0, cost: 0 };
  /** Prefix keys already written this instance (prompt cache is per-run). */
  private readonly seenPrefixes = new Set<string>();

  status(): CapabilityStatus {
    return { ready: true, detail: "MCP↔API↔provider translation; prompt-cache lowering; metering", tier: "basic" };
  }

  inAdapter(wire: unknown): unknown {
    return wire;
  }
  outAdapter(io: unknown): unknown {
    return io;
  }

  /** §8.3 — table-driven translation through the internal canonical form. */
  translate(from: string, to: string, payload: unknown): unknown {
    if (from === to) return payload;
    if (!REPRESENTATIONS.has(from) || !REPRESENTATIONS.has(to)) throw untranslatable(from, to);
    return fromInternal(to, toInternal(from, payload));
  }

  /** §8.6 — lower abstract breakpoints to a provider's prefix-cache wire form.
   *  A provider that cannot honor them degrades to a no-op (never an error). */
  lowerPromptCache(breakpoints: string[] | undefined, provider: string): unknown {
    if (!breakpoints || breakpoints.length === 0) return undefined;
    switch (provider) {
      case "anthropic":
        // cache_control on the last block of each cacheable prefix segment.
        return { provider, directives: breakpoints.map((segment) => ({ segment, cache_control: { type: "ephemeral" } })) };
      case "openai":
        // Automatic prefix caching: no annotation, just a stable leading order.
        return { provider, order: [...breakpoints], annotation: null };
      case "local":
        // llama-server KV-prefix reuse.
        return { provider, cache_prompt: true, segments: [...breakpoints] };
      default:
        return undefined; // unhonored → no-op
    }
  }

  /** §8.6 — prompt-prefix cache accounting. First sight writes; later sights hit. */
  accountPromptCache(prefixKey: string, promptTokens: number): PromptCacheResult {
    if (this.seenPrefixes.has(prefixKey)) {
      this.counter.cache_read += promptTokens;
      return { disposition: "hit", cache_read: promptTokens, cache_write: 0 };
    }
    this.seenPrefixes.add(prefixKey);
    this.counter.cache_write += promptTokens;
    return { disposition: "write", cache_read: 0, cache_write: promptTokens };
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

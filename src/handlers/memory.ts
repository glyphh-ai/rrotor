/**
 * The memory/grounding-backed handlers (docs/runtime.md §2.3): `write` (§7.4),
 * the three `retrieve.*` recall primitives (§7.5–§7.7), `hdc.map` (§7.3), and
 * `cascade` (§7.18). Each is deterministic-given-store (or, for the embedding
 * boundary of `retrieve.vector`, checkpointed) and carries/validates `space_id`.
 */

import type {
  CascadeConfig,
  Frame,
  HdcMapConfig,
  RetrieveKbConfig,
  RetrieveSqlConfig,
  RetrieveVectorConfig,
  StepResult,
  WriteConfig,
} from "../types.js";
import type { HandlerArgs, StepHandler } from "./types.js";

export const writeHandler: StepHandler = {
  type: "write",
  async execute({ step, input, env, plugins }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? {}) as WriteConfig;
    let facts: Array<Record<string, unknown>>;
    if (Array.isArray(input.facts)) {
      facts = input.facts as Array<Record<string, unknown>>;
    } else if (input.text !== undefined && cfg.mode === "absorb") {
      // Deterministic NL enricher (§7.4 absorb): extract structured (entity, role,
      // filler) triples from free text; fall back to a raw.text slot if nothing
      // matches. A model enricher is the premium swap-in.
      facts = absorbText(String(input.text), cfg.key);
    } else if (input.text !== undefined) {
      // raw mode: store the text verbatim under the key's entity.
      facts = [{ entity: cfg.key ?? "unknown", role: "raw.text", filler: String(input.text) }];
    } else {
      facts = [];
    }
    const written = plugins.memory.write(facts, {
      key: cfg.key,
      mode: cfg.mode,
      speaker: cfg.speaker,
      spaceId: env.space_id,
      tick: env.logical_tick,
    });
    return {
      output: { written, key: cfg.key, space_id: env.space_id },
      frames: [{ type: "done", data: { written } }],
      status: "ok",
    };
  },
};

/**
 * Deterministic NL → (entity, role, filler) extractor for `write` absorb mode.
 * Pure pattern matching over sentences — no model — so absorbed facts are
 * replay-safe. Recognized shapes: `X's R is Z`, `X lives in Y`, `X has Y`,
 * `X is Y`. Unmatched text falls back to a single `raw.text` slot.
 */
export function absorbText(text: string, key?: string): Array<Record<string, unknown>> {
  const facts: Array<Record<string, unknown>> = [];
  for (const raw of text.split(/[.;\n]+/)) {
    const s = raw.trim();
    if (!s) continue;
    let m: RegExpExecArray | null;
    // Default tiers (docs/memory.md): directives + self-facts are lifelong (long);
    // incidental task facts decay (mid). A `write` step's `tier` still overrides.
    const add = (entity: string, role: string, filler: string, tier: "long" | "mid", factKey?: string) =>
      // A distinct per-slot key so multiple absorbed facts never supersede one another.
      facts.push({ entity, role, filler, tier, key: factKey ?? `${entity.toLowerCase()}:${role.toLowerCase()}` });
    if ((m = /^(?:always|never|from now on,?|going forward,?|remember to|make sure to|be sure(?: to)?|i told you to|i asked you to|please always) .+$/i.exec(s))) {
      // A STANDING DIRECTIVE (§7.4): always-injected, not similarity-recalled.
      // Keyed by content so restating dedupes but distinct directives coexist.
      add(key ?? "user", "directive", s, "long", `directive:${s.toLowerCase().trim()}`);
    } else if ((m = /^my ([\w ]+?) (?:is|are) (?:called |named )?(.+)$/i.exec(s))) {
      // First-person self-fact → the session user's own slot: `my name is Ada`.
      add(key ?? "user", m[1].trim(), m[2].trim(), "long");
    } else if ((m = /^(.+?)'s ([\w. ]+?) (?:is|are|was|were) (.+)$/i.exec(s))) {
      add(m[1].trim(), m[2].trim(), m[3].trim(), "mid");
    } else if ((m = /^(.+?) (?:lives?|lived|resides?) in (.+)$/i.exec(s))) {
      add(m[1].trim(), "rel.city", m[2].trim(), "mid");
    } else if ((m = /^(.+?) has (?:an? |the )?(.+)$/i.exec(s))) {
      add(m[1].trim(), "rel.has", m[2].trim(), "mid");
    } else if ((m = /^(.+?) (?:is|are|was|were) (?:an? |the )?(.+)$/i.exec(s))) {
      add(m[1].trim(), "attr.is", m[2].trim(), "mid");
    }
  }
  if (facts.length === 0) facts.push({ entity: key ?? "unknown", role: "raw.text", filler: text });
  return facts;
}

export const retrieveSqlHandler: StepHandler = {
  type: "retrieve.sql",
  async execute({ step, input, env, plugins }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? { op: "refuse" }) as RetrieveSqlConfig;
    const params: Record<string, unknown> = {
      person: input.person ?? cfg.person,
      entity: input.person ?? input.entity ?? cfg.person,
      slot: input.slot ?? cfg.slot,
      role: input.role ?? cfg.slot,
      value: input.value ?? cfg.value,
      a: input.a ?? cfg.a,
      b: input.b ?? cfg.b,
      k: input.k ?? cfg.k,
      conditions: input.conditions ?? cfg.conditions,
    };
    const r = plugins.memory.executeOp(cfg.op, params, env.space_id);
    return {
      output: { rows: r.rows, count: r.count, matched: r.matched },
      frames: [{ type: "done", data: { op: cfg.op, count: r.count } }],
      status: "ok",
    };
  },
};

export const retrieveKbHandler: StepHandler = {
  type: "retrieve.kb",
  async execute({ step, input, env, plugins }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? { mode: "verify" }) as RetrieveKbConfig;
    const entity = String(input.entity ?? cfg.entity ?? "");
    const role = String(input.role ?? cfg.role ?? "");
    if (cfg.mode === "verify") {
      const filler = String(input.filler ?? "");
      const v = plugins.grounding.verify(entity, role, filler, cfg.margin ?? 0.05, env.space_id);
      return {
        output: { membership: v.membership, margin: v.margin, top: v.top, grounded: v.grounded },
        frames: [{ type: "done", data: { mode: "verify", grounded: v.grounded } }],
        status: "ok",
      };
    }
    if (cfg.mode === "node") {
      // The entity's full node: all its current (role, filler) edges.
      const rows = plugins.memory.executeOp("lookup", { person: entity }, env.space_id).rows;
      return {
        output: { node: entity, edges: rows, count: rows.length },
        frames: [{ type: "done", data: { mode: "node", edges: rows.length } }],
        status: "ok",
      };
    }
    if (cfg.mode === "neighbors") {
      // Graph neighbors: distinct OTHER entities that share a filler value with
      // this entity (co-reference over the fact graph).
      const own = plugins.memory.executeOp("lookup", { person: entity }, env.space_id).rows;
      const fillers = [...new Set(own.map((r) => String(r.filler)))];
      const neighbors = new Set<string>();
      for (const filler of fillers) {
        for (const r of plugins.memory.executeOp("who", { value: filler }, env.space_id).rows) {
          const e = String(r.entity);
          if (e.toLowerCase() !== entity.toLowerCase()) neighbors.add(e);
        }
      }
      const list = [...neighbors].sort();
      return {
        output: { neighbors: list, count: list.length },
        frames: [{ type: "done", data: { mode: "neighbors", count: list.length } }],
        status: "ok",
      };
    }
    // probe: HDC associative recall of the role's filler from the entity's cortex.
    const p = plugins.grounding.probe(entity, role, env.space_id);
    return {
      output: { filler: p.filler, membership: p.membership, margin: p.margin, top: p.top },
      frames: [{ type: "done", data: { mode: cfg.mode } }],
      status: "ok",
    };
  },
};

export const retrieveVectorHandler: StepHandler = {
  type: "retrieve.vector",
  async execute({ step, input, plugins }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? {}) as RetrieveVectorConfig;
    const query = String(input.query ?? input.text ?? "");
    const hits = plugins.memory.semanticRecall(query, cfg.top_k ?? 8, cfg.threshold ?? 0.35);
    return {
      output: { hits, scores: hits.map((h) => h.score) },
      frames: [{ type: "done", data: { hits: hits.length } }],
      status: "ok",
    };
  },
};

export const hdcMapHandler: StepHandler = {
  type: "hdc.map",
  async execute({ step, input, env, plugins }: HandlerArgs): Promise<StepResult> {
    const _cfg = (step.config ?? {}) as HdcMapConfig;
    // Structured facts encode directly; free text has no enricher on the basic
    // tier, so it maps to a single `raw.text` slot.
    let roleFillers: Record<string, string>;
    if (input.facts && typeof input.facts === "object" && !Array.isArray(input.facts)) {
      roleFillers = input.facts as Record<string, string>;
    } else if (Array.isArray(input.facts)) {
      roleFillers = {};
      for (const f of input.facts as Array<Record<string, unknown>>) {
        if (f.role) roleFillers[String(f.role)] = String(f.filler ?? f.value ?? "");
      }
    } else {
      roleFillers = { "raw.text": String(input.text ?? "") };
    }
    const enc = plugins.grounding.encode(roleFillers, env.space_id);
    return {
      output: { cortex: enc.cortex, slots: enc.slots, dropped: enc.dropped },
      frames: [{ type: "parse", data: { slots: enc.slots.length } }, { type: "done" }],
      status: "ok",
    };
  },
};

export const cascadeHandler: StepHandler = {
  type: "cascade",
  async execute({ step, plugins }: HandlerArgs): Promise<StepResult> {
    const cfg = (step.config ?? {}) as CascadeConfig;
    const counts = plugins.memory.cascade(cfg.span);
    const frames: Frame[] = [{ type: "delta", data: counts }, { type: "done", data: counts }];
    return { output: { consolidated: counts }, frames, status: "ok" };
  },
};

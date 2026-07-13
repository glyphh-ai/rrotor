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
    } else if (input.text !== undefined) {
      // Basic tier has no NL enricher: store the raw text under the key's entity.
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
    const _cfg = (step.config ?? {}) as CascadeConfig;
    const counts = plugins.memory.cascade();
    const frames: Frame[] = [{ type: "done", data: counts }];
    return { output: { consolidated: counts }, frames, status: "ok" };
  },
};

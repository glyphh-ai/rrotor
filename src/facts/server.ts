/**
 * facts/server.ts — the `glyphh_facts` in-process MCP server: the six tools
 * the highest-ranked LLM drives the fact substrate with.
 *
 *   build_fact      — encode + find neighbors, PERSIST NOTHING (the reasoning
 *                     step's input: "is this new, or an update to one of these?")
 *   create_fact     — append a NEW glyphh to the org ledger
 *   search_facts    — cosine-gated candidates (≥ threshold), FULL glyphh JSON —
 *                     the LLM reasons over the collection; it is never pure sim
 *   update_fact     — append a new version and supersede the old id (the ledger
 *                     never mutates; history is the product)
 *   delete_fact     — tombstone (the fact leaves every future turn; the record stays)
 *   build_fact_tree — persist a DERIVED glyphh composed by the LLM from cited
 *                     sources; returns the auditable FactTree rendering
 *
 * Facts are GLYPH OBJECTS in the universal 7×33 schema (universal-schema.ts),
 * encoded by the faithful TS port of the canon (src/glyph — oracle-verified
 * byte parity), values expressed toward NSM primes (exec/glyph/primes.ts).
 * The ledger is org-schema Postgres (facts/store.ts): concept JSON is the
 * authority, vectors are derived and re-encoded on hydration.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import type { Principal } from "../auth/introspect.js";
import { GlyphEncoder } from "../glyph/encoder.js";
import { EncoderConfig, type LayerConfig } from "../glyph/config.js";
import { concept, type Glyph } from "../glyph/types.js";
import { cosineSimilarity, type Bipolar } from "../glyph/ops.js";
import { UNIVERSAL_SCHEMA, sanitizeUniversal } from "../glyph/universal-schema.js";
import { ALL_PRIMES, decompose } from "../exec/glyph/primes.js";
import { FactTree } from "../glyph/fact-tree.js";
import { GlyphStore, glyphStoreFromEnv, type GlyphRow } from "./store.js";
import { log } from "../obs/logger.js";

/** Dimension/seed of the org fact space — the canonical defaults. */
const DIM = 10_000;
const SEED = 42;

/** Measured on the universal role skeleton: unrelated facts share structure
 *  and sit well above zero; same-idea restatements in NSM-normalized fillers
 *  land higher still. The gate is deliberately permissive — the LLM composer
 *  filters candidates; pure similarity is never the verdict. */
const DEFAULT_THRESHOLD = 0.55;
const SEARCH_LIMIT_MAX = 24;

/** The universal schema AS the explicit encoder config: seven layers, each
 *  one `roles` segment carrying that layer's roles; `entity.name` is the
 *  identifier's key part. Role names are unique across the whole schema, so
 *  bare role atoms cannot collide. */
function universalEncoderConfig(): EncoderConfig {
  const layers: LayerConfig[] = Object.entries(UNIVERSAL_SCHEMA).map(([layerName, roles]) => ({
    name: layerName,
    similarityWeight: 1.0,
    securityWeight: 1.0,
    segments: [{
      name: "roles",
      similarityWeight: 1.0,
      securityWeight: 1.0,
      roles: roles.map((r) => ({
        name: r,
        keyPart: layerName === "entity" && r === "name",
        similarityWeight: 1.0,
        securityWeight: 1.0,
      })),
    }],
  }));
  return new EncoderConfig({ dimension: DIM, seed: SEED, layers });
}

const encoder = new GlyphEncoder(universalEncoderConfig());

/** Per-org in-memory item index (id → cortex), hydrated from the ledger by
 *  RE-ENCODING concepts — vectors are derived, the concept JSON is authority. */
const indexes = new Map<string, Map<string, Bipolar>>();
let storePromise: Promise<GlyphStore | null> | null = null;
const store = (): Promise<GlyphStore | null> => (storePromise ??= glyphStoreFromEnv());
/** Test seam: inject a store (PGlite) and reset the derived indexes. */
export function setFactsStoreForTests(p: Promise<GlyphStore | null>): void {
  storePromise = p;
  indexes.clear();
}

interface FactInput { name: string; facts: unknown }

function encodeFact(input: FactInput): { glyph: Glyph; clean: Record<string, Record<string, string>>; primes: string[] } {
  const clean = sanitizeUniversal(input.facts);
  const attributes: Record<string, string> = {};
  for (const roles of Object.values(clean)) for (const [role, value] of Object.entries(roles)) attributes[role] = value;
  if (!Object.keys(attributes).length) {
    throw new Error("facts must fill at least one universal slot — layers entity/perceptual/spatial/temporal/relational/quantitative/epistemic, schema-valid roles only");
  }
  const glyph = encoder.encode(concept({ name: input.name, attributes, metadata: {} }));
  // Primes STAMPED AT WRITE TIME (the doctrine): recall selects by the
  // exchange's own primes against these.
  const primes = decompose(`${input.name} ${Object.values(attributes).join(" ")}`);
  return { glyph, clean, primes };
}

async function orgIndex(s: GlyphStore, p: Principal): Promise<Map<string, Bipolar>> {
  let idx = indexes.get(p.orgId);
  if (idx) return idx;
  idx = new Map();
  const rows = await s.live({ orgId: p.orgId, userId: p.userId });
  for (const row of rows) {
    try {
      const c = row.concept as { name?: string; facts?: unknown };
      idx.set(row.id, encodeFact({ name: String(c.name ?? row.name), facts: c.facts }).glyph.globalCortex.data);
    } catch { /* a malformed historical row never blocks hydration */ }
  }
  indexes.set(p.orgId, idx);
  log.info("fact index hydrated", { org: p.orgId, facts: idx.size });
  return idx;
}

async function neighbors(
  s: GlyphStore, p: Principal, probe: Bipolar, threshold: number, limit: number,
): Promise<Array<{ row: GlyphRow; cos: number }>> {
  const idx = await orgIndex(s, p);
  const hits: Array<{ id: string; cos: number }> = [];
  for (const [id, vec] of idx) {
    const cos = cosineSimilarity(probe, vec);
    if (cos >= threshold) hits.push({ id, cos });
  }
  hits.sort((a, b) => b.cos - a.cos);
  const top = hits.slice(0, Math.min(limit, SEARCH_LIMIT_MAX));
  const rows = await s.byIds({ orgId: p.orgId, userId: p.userId }, top.map((h) => h.id));
  const byId = new Map(rows.map((r) => [r.id, r]));
  return top.flatMap((h) => {
    const row = byId.get(h.id);
    return row ? [{ row, cos: h.cos }] : [];
  });
}

const asFactJson = (row: GlyphRow, cos?: number): Record<string, unknown> => ({
  id: row.id,
  name: row.name,
  ...(cos !== undefined ? { cos: Number(cos.toFixed(4)) } : {}),
  confidence: row.confidence,
  scope: row.scope,
  derived: row.derived,
  ...(row.citations.length ? { citations: row.citations } : {}),
  concept: row.concept,
  createdAt: row.createdAt,
});

const text = (t: string) => ({ content: [{ type: "text" as const, text: t }] });
const errText = (t: string) => ({ content: [{ type: "text" as const, text: `ERROR: ${t}` }], isError: true });

const FACTS_SHAPE =
  "facts: {layer: {role: value}} over the UNIVERSAL schema — layers entity/perceptual/spatial/temporal/relational/quantitative/epistemic; " +
  "express values in NSM primes where possible (I/YOU/SOMEONE/THIS/GOOD/BAD/KNOW/WANT/DO/HAPPEN/BECAUSE/NOT/CAN/...; names, numbers and domain terms stay literal); " +
  "epistemic.certainty carries your confidence in words. Unknown layers/roles are dropped; empty values refused.";

export function buildFactsServer(principal: Principal): McpServer {
  const mcp = new McpServer({ name: "glyphh_facts", version: "1.0.0" }, { capabilities: { tools: {} } });
  const p = principal;

  mcp.server.setRequestHandler(ListToolsRequestSchema, () =>
    Promise.resolve({
      tools: [
        {
          name: "build_fact",
          description:
            `build_fact({name, facts}) — encode a distilled fact as a glyphh and return its NEIGHBORS from the org's fact ledger WITHOUT persisting anything. ` +
            `THE reasoning step: call this first with the fact you distilled from the turn, read the returned candidates, then decide — genuinely new → create_fact; a restatement or change of a candidate → update_fact with its id. ${FACTS_SHAPE}`,
          inputSchema: { type: "object" as const, additionalProperties: true },
        },
        {
          name: "create_fact",
          description:
            `create_fact({name, facts, confidence?, scope?}) — append a NEW fact glyphh to the org ledger (id stamped name@timestamp#v1; auditable forever). ` +
            `confidence in [0,1] (default 0.7); scope 'org' (shared, default) or 'user' (yours). Only after build_fact showed no candidate this duplicates. ${FACTS_SHAPE}`,
          inputSchema: { type: "object" as const, additionalProperties: true },
        },
        {
          name: "search_facts",
          description:
            `search_facts({name?, facts, threshold?, limit?}) — cosine-gated candidates (default gate ${DEFAULT_THRESHOLD}) from the org ledger, each with its FULL glyphh JSON, confidence and timestamps. ` +
            `Similarity only PROPOSES — you reason over the collection; compose your answer from the facts, never from the scores. ${FACTS_SHAPE}`,
          inputSchema: { type: "object" as const, additionalProperties: true },
        },
        {
          name: "update_fact",
          description:
            "update_fact({supersedes, name, facts, confidence?}) — record a fact CHANGING: appends a new glyphh (fresh id + timestamp) and marks the old id superseded. " +
            "The ledger never mutates — the chain of ids IS the fact's auditable history. Use the id build_fact/search_facts returned.",
          inputSchema: { type: "object" as const, additionalProperties: true },
        },
        {
          name: "delete_fact",
          description:
            "delete_fact({id}) — tombstone a fact: it leaves every future turn and index immediately; the ledger row remains for audit. Use for facts that are wrong or that the user asks to forget.",
          inputSchema: { type: "object" as const, additionalProperties: true },
        },
        {
          name: "build_fact_tree",
          description:
            "build_fact_tree({name, facts, citations, confidence?}) — persist a DERIVED glyphh you composed by reasoning over retrieved facts. " +
            "citations: the source glyph ids it derives from (build_fact/search_facts results). Returns the new id plus the auditable FactTree rendering (every source cited with its cosine to the derived cortex).",
          inputSchema: { type: "object" as const, additionalProperties: true },
        },
      ],
    }),
  );

  mcp.server.setRequestHandler(CallToolRequestSchema, async (rq) => {
    const args = (rq.params.arguments ?? {}) as Record<string, unknown>;
    const s = await store();
    if (!s) return errText("the fact ledger is not configured on this runtime (ROTOR_STATOR_URL unset)");
    try {
      switch (rq.params.name) {
        case "build_fact": {
          const { glyph, clean } = encodeFact({ name: String(args.name ?? ""), facts: args.facts });
          const near = await neighbors(s, p, glyph.globalCortex.data, DEFAULT_THRESHOLD, 8);
          return text(JSON.stringify({
            preview: { identifier: glyph.identifier, layers: Object.keys(clean), spaceId: glyph.spaceId },
            candidates: near.map((n) => asFactJson(n.row, n.cos)),
            guidance: near.length
              ? "Reason over the candidates: same fact → update_fact with its id; related but distinct → create_fact; contradictory → update_fact the old one."
              : "No candidates above the gate — create_fact if this is worth remembering.",
          }, null, 1));
        }
        case "create_fact": {
          const name = String(args.name ?? "");
          const { glyph, clean, primes } = encodeFact({ name, facts: args.facts });
          const scope = args.scope === "user" ? "user" as const : "org" as const;
          const confidence = clampConfidence(args.confidence);
          await s.insert({ orgId: p.orgId, userId: p.userId }, {
            id: glyph.identifier, name, scope,
            concept: { name, facts: clean },
            confidence, primes, citations: [], derived: false,
            cortexB64: Buffer.from(glyph.globalCortex.data.buffer, glyph.globalCortex.data.byteOffset, glyph.globalCortex.data.byteLength).toString("base64"),
          });
          indexes.get(p.orgId)?.set(glyph.identifier, glyph.globalCortex.data);
          return text(JSON.stringify({ id: glyph.identifier, confidence, scope }));
        }
        case "search_facts": {
          const threshold = typeof args.threshold === "number" ? args.threshold : DEFAULT_THRESHOLD;
          const limit = typeof args.limit === "number" ? args.limit : 12;
          const { glyph } = encodeFact({ name: String(args.name ?? "probe"), facts: args.facts });
          const near = await neighbors(s, p, glyph.globalCortex.data, threshold, limit);
          return text(JSON.stringify({ facts: near.map((n) => asFactJson(n.row, n.cos)) }, null, 1));
        }
        case "update_fact": {
          const supersedes = String(args.supersedes ?? "");
          if (!supersedes) return errText("update_fact needs { supersedes } — the old fact's id");
          const name = String(args.name ?? "");
          const { glyph, clean, primes } = encodeFact({ name, facts: args.facts });
          const confidence = clampConfidence(args.confidence);
          const old = (await s.byIds({ orgId: p.orgId, userId: p.userId }, [supersedes]))[0];
          if (!old) return errText(`no such fact: ${supersedes}`);
          await s.insert({ orgId: p.orgId, userId: p.userId }, {
            id: glyph.identifier, name: name || old.name, scope: old.scope,
            concept: { name: name || old.name, facts: clean },
            confidence, primes, citations: [supersedes], derived: old.derived,
            cortexB64: Buffer.from(glyph.globalCortex.data.buffer, glyph.globalCortex.data.byteOffset, glyph.globalCortex.data.byteLength).toString("base64"),
          });
          await s.supersede({ orgId: p.orgId, userId: p.userId }, supersedes, glyph.identifier);
          const idx = indexes.get(p.orgId);
          idx?.delete(supersedes);
          idx?.set(glyph.identifier, glyph.globalCortex.data);
          return text(JSON.stringify({ id: glyph.identifier, supersedes, confidence }));
        }
        case "delete_fact": {
          const id = String(args.id ?? "");
          if (!id) return errText("delete_fact needs { id }");
          const removed = await s.tombstone({ orgId: p.orgId, userId: p.userId }, id);
          if (!removed) return errText(`no such live fact: ${id}`);
          indexes.get(p.orgId)?.delete(id);
          return text(JSON.stringify({ deleted: id }));
        }
        case "build_fact_tree": {
          const name = String(args.name ?? "");
          const citations = (Array.isArray(args.citations) ? args.citations : []).map(String).filter(Boolean);
          if (!citations.length) return errText("build_fact_tree needs { citations } — the source glyph ids the derivation reasons from");
          const { glyph, clean, primes } = encodeFact({ name, facts: args.facts });
          const confidence = clampConfidence(args.confidence);
          const sources = await s.byIds({ orgId: p.orgId, userId: p.userId }, citations);
          if (sources.length !== citations.length) {
            const found = new Set(sources.map((r) => r.id));
            return errText(`unknown citation ids: ${citations.filter((c) => !found.has(c)).join(", ")}`);
          }
          await s.insert({ orgId: p.orgId, userId: p.userId }, {
            id: glyph.identifier, name, scope: "org",
            concept: { name, facts: clean },
            confidence, primes, citations, derived: true,
            cortexB64: Buffer.from(glyph.globalCortex.data.buffer, glyph.globalCortex.data.byteOffset, glyph.globalCortex.data.byteLength).toString("base64"),
          });
          indexes.get(p.orgId)?.set(glyph.identifier, glyph.globalCortex.data);

          const tree = new FactTree(name || "Derived fact");
          const idx = await orgIndex(s, p);
          for (const src of sources) {
            const vec = idx.get(src.id);
            tree.addFact({
              path: ["derivation", src.id],
              description: src.name,
              value: vec ? Number(cosineSimilarity(glyph.globalCortex.data, vec).toFixed(4)) : null,
              citations: [{ glyphId: src.id, component: "cortex", timestamp: src.createdAt, version: "v1", dataHash: glyph.spaceId }],
              mathExplanation: "cos(derived, source) = (d . s) / dim",
            });
          }
          return text(JSON.stringify({ id: glyph.identifier, confidence, tree: tree.toText() }, null, 1));
        }
        default:
          return errText(`unknown tool: ${rq.params.name}`);
      }
    } catch (err) {
      return errText((err as Error).message);
    }
  });
  return mcp;
}

function clampConfidence(v: unknown): number {
  const n = typeof v === "number" ? v : 0.7;
  return Math.max(0, Math.min(1, n));
}

/** True when NSM discipline is worth nudging — exported for the prompt layer. */
export function nsmPrimeCount(): number {
  return ALL_PRIMES.size;
}

// ── THE INJECTION HALF: the fixed-shape fact block every forward pass opens
// with. Selection is DETERMINISTIC and sub-millisecond (no model call): the
// exchange's own primes — decomposed from the newest turns of the resent
// history — select and rank the org's facts ("the prompt's own primes select
// what comes back; that grouping is the fact tree"), with confidence and
// recency breaking ties, and the highest-confidence standing facts always in
// contention (the "things I repeat constantly" cure). The block's SHAPE is
// constant: stable header, ≤ FACT_BLOCK_MAX facts, one line each, hard char
// cap — a fixed-size window whose CONTENT redistributes, never grows. ──

const FACT_BLOCK_MAX = 12;
const FACT_BLOCK_CHAR_CAP = 2400;
const FACT_BLOCK_HEADER = "## Org facts (glyphh ledger — cite ids when you rely on one)";

function factLine(row: GlyphRow): string {
  const c = row.concept as { facts?: Record<string, Record<string, string>> };
  const slots: string[] = [];
  for (const [layer, roles] of Object.entries(c.facts ?? {})) {
    for (const [role, value] of Object.entries(roles)) slots.push(`${layer}.${role}=${value}`);
  }
  return `- [${row.id}] (conf ${row.confidence.toFixed(2)}${row.derived ? ", derived" : ""}) ${slots.join("; ")}`;
}

/**
 * Render the per-turn fact block for one principal, selected against the
 * exchange text (the tail of the client's resent history). Null when the
 * ledger is unavailable or empty — the turn simply runs without memory.
 */
export async function renderFactBlock(principal: Principal, exchangeText: string): Promise<string | null> {
  const s = await store();
  if (!s) return null;
  try {
    const rows = await s.live({ orgId: principal.orgId, userId: principal.userId });
    if (!rows.length) return null;
    const wanted = new Set(decompose(exchangeText));
    const now = Date.now();
    const scored = rows.map((row) => {
      const overlap = row.primes.reduce((n, pr) => n + (wanted.has(pr) ? 1 : 0), 0);
      const ageDays = Math.max(0, (now - Date.parse(row.createdAt)) / 86_400_000);
      // Prime overlap dominates; confidence separates peers; a gentle recency
      // decay keeps stale invariants from crowding out fresher ones forever.
      const score = overlap * 10 + row.confidence * 5 - Math.min(ageDays / 30, 3);
      return { row, score };
    });
    scored.sort((a, b) => b.score - a.score);
    const lines: string[] = [FACT_BLOCK_HEADER];
    let chars = FACT_BLOCK_HEADER.length;
    let taken = 0;
    for (const { row } of scored) {
      if (taken >= FACT_BLOCK_MAX) break;
      const line = factLine(row);
      if (chars + line.length + 1 > FACT_BLOCK_CHAR_CAP) continue;
      lines.push(line);
      chars += line.length + 1;
      taken++;
    }
    return taken > 0 ? lines.join("\n") : null;
  } catch (err) {
    log.warn("fact block render failed (turn runs without memory)", { detail: (err as Error).message });
    return null;
  }
}

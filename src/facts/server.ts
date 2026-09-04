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
import {
  conceptWords, dictCosine, directiveOf, exchangeVector,
  factDictVector, glossVectors, tokenize, wordsVector, type Lexicon,
} from "./dict-lane.js";
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

/** One indexed fact: the canonical cortex (glyph space) + the dict-lane
 *  vector (docs/dict-vector.md). Both DERIVED — concept JSON is authority. */
interface IndexedFact {
  cortex: Bipolar;
  dict: Float32Array;
}

/**
 * A per-org EPOCH: the hydrated item index plus the lexicon snapshot its IDF
 * weights (and gloss vectors) were built from. The epoch IS the determinism
 * boundary — same ledger + same snapshot → same selection, replayable; the
 * live lexicon keeps counting underneath and takes effect at the next
 * hydration.
 */
interface OrgEpoch {
  items: Map<string, IndexedFact>;
  lexicon: Lexicon;
  glosses: Map<string, Float32Array>;
  hydratedAt: string;
}

const indexes = new Map<string, OrgEpoch>();
let storePromise: Promise<GlyphStore | null> | null = null;
const store = (): Promise<GlyphStore | null> => (storePromise ??= glyphStoreFromEnv());
/** Test seam: inject a store (PGlite) and reset the derived indexes. */
export function setFactsStoreForTests(p: Promise<GlyphStore | null>): void {
  storePromise = p;
  indexes.clear();
}

interface FactInput { name: string; facts: unknown }

/** Everything non-empty the caller said that sanitize did NOT keep — unknown
 *  layers and unknown roles alike, verbatim under the names the caller used.
 *  Loose capture: a fact's CONTENT is never silently dropped (2026-08-25: a
 *  model wrote relational.codename/owner — off-schema — and the values
 *  vanished; the persisted fact was an empty shell that recall then honestly,
 *  uselessly reported as "nothing stored"). */
function collectExtras(
  facts: unknown,
  clean: Record<string, Record<string, string>>,
): Record<string, Record<string, string>> {
  const extra: Record<string, Record<string, string>> = {};
  if (!facts || typeof facts !== "object" || Array.isArray(facts)) return extra;
  for (const [layer, roles] of Object.entries(facts as Record<string, unknown>)) {
    if (!roles || typeof roles !== "object" || Array.isArray(roles)) continue;
    const kept: Record<string, string> = {};
    for (const [role, value] of Object.entries(roles as Record<string, unknown>)) {
      if (clean[layer]?.[role] !== undefined) continue; // already encoded
      if (value == null) continue;
      const v = String(value).trim();
      if (!v || ["none", "null", "n/a"].includes(v.toLowerCase())) continue;
      kept[role] = v;
    }
    if (Object.keys(kept).length) extra[layer] = kept;
  }
  return extra;
}

/**
 * LOOSE CAPTURE, STRICT ENCODING, HONEST REPORTING.
 *  - clean:  schema-valid slots → the ONLY thing the encoder binds (the vector
 *    space stays canon-stable; the schema IS the space_id).
 *  - extra:  every other non-empty slot, preserved verbatim in the concept
 *    JSON (the authority) — rendered in the per-turn fact block, selectable
 *    by primes, just not vector-addressable by that slot.
 *  - primes: stamped from ALL values (clean + extra + name), so the injection
 *    half can select a fact whose salient content lives off-schema.
 *  - backfill: no encodable slot but a name → entity.name carries the cortex
 *    rather than refusing (this also un-breaks the /facts/recall probe, whose
 *    {query:{text}} shape sanitized to nothing and threw).
 */
function encodeFact(input: FactInput): {
  glyph: Glyph;
  clean: Record<string, Record<string, string>>;
  extra: Record<string, Record<string, string>>;
  stored: Record<string, Record<string, string>>;
  primes: string[];
  backfilled: boolean;
} {
  let clean = sanitizeUniversal(input.facts);
  const extra = collectExtras(input.facts, clean);
  let backfilled = false;
  if (!Object.values(clean).some((roles) => Object.keys(roles).length)) {
    const name = input.name.trim();
    if (!name && !Object.keys(extra).length) {
      throw new Error("facts must carry a name or at least one non-empty value — layers entity/perceptual/spatial/temporal/relational/quantitative/epistemic preferred; other slots are preserved verbatim");
    }
    clean = { entity: { name: name || "unnamed fact" } };
    backfilled = true;
  }
  const attributes: Record<string, string> = {};
  for (const roles of Object.values(clean)) for (const [role, value] of Object.entries(roles)) attributes[role] = value;
  const glyph = encoder.encode(concept({ name: input.name, attributes, metadata: {} }));
  // Primes STAMPED AT WRITE TIME (the doctrine): recall selects by the
  // exchange's own primes against these — extras included, so preserved
  // content stays reachable by the fact block's selection.
  const allValues = [
    ...Object.values(clean).flatMap((r) => Object.values(r)),
    ...Object.values(extra).flatMap((r) => Object.values(r)),
  ];
  const primes = decompose(`${input.name} ${allValues.join(" ")}`);
  // The stored concept is the union — clean and extra are disjoint by
  // construction (extras skip anything clean kept).
  const stored: Record<string, Record<string, string>> = {};
  for (const [layer, roles] of Object.entries(clean)) stored[layer] = { ...roles };
  for (const [layer, roles] of Object.entries(extra)) stored[layer] = { ...(stored[layer] ?? {}), ...roles };
  return { glyph, clean, extra, stored, primes, backfilled };
}

/** `layer.role` names of preserved-but-unencoded slots — the honesty payload. */
function extraSlotNames(extra: Record<string, Record<string, string>>): string[] {
  return Object.entries(extra).flatMap(([layer, roles]) => Object.keys(roles).map((r) => `${layer}.${r}`));
}

const factsOf = (row: GlyphRow): Record<string, Record<string, string>> =>
  ((row.concept as { facts?: Record<string, Record<string, string>> })?.facts ?? {});

async function orgIndex(s: GlyphStore, p: Principal): Promise<OrgEpoch> {
  let epoch = indexes.get(p.orgId);
  if (epoch) return epoch;
  const gp = { orgId: p.orgId, userId: p.userId };
  const lexicon = await s.lexiconCounts(gp);
  epoch = { items: new Map(), lexicon, glosses: glossVectors(lexicon), hydratedAt: new Date().toISOString() };
  const rows = await s.live(gp);
  for (const row of rows) {
    try {
      const c = row.concept as { name?: string; facts?: unknown };
      const name = String(c.name ?? row.name);
      epoch.items.set(row.id, {
        cortex: encodeFact({ name, facts: c.facts }).glyph.globalCortex.data,
        dict: factDictVector(name, factsOf(row), row.primes, lexicon),
      });
    } catch { /* a malformed historical row never blocks hydration */ }
  }
  indexes.set(p.orgId, epoch);
  log.info("fact index hydrated", { org: p.orgId, facts: epoch.items.size, lexicon: lexicon.size, epoch: epoch.hydratedAt });
  return epoch;
}

/** Add a fresh write to the hydrated epoch (if one exists) under ITS lexicon
 *  snapshot — epoch determinism holds; the new words land at next hydration. */
function indexNewFact(orgId: string, id: string, cortex: Bipolar, name: string, facts: Record<string, Record<string, string>>, primes: string[]): void {
  const epoch = indexes.get(orgId);
  if (!epoch) return;
  epoch.items.set(id, { cortex, dict: factDictVector(name, facts, primes, epoch.lexicon) });
}

async function neighbors(
  s: GlyphStore, p: Principal, probe: Bipolar, threshold: number, limit: number,
): Promise<Array<{ row: GlyphRow; cos: number }>> {
  const { items } = await orgIndex(s, p);
  const hits: Array<{ id: string; cos: number }> = [];
  for (const [id, item] of items) {
    const cos = cosineSimilarity(probe, item.cortex);
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
  "epistemic.certainty carries your confidence in words. PREFER schema roles — they strengthen similarity — but NEVER omit a salient value: " +
  "any other slot is preserved verbatim and prime-indexed (reported back as preservedSlots), just not vector-bound. A fact without its content is worse than no fact. Empty values refused. " +
  "STANDING RULES ('always/never/prefer X') get the rule layer: rule: {action: 'must-use'|'never'|'prefer', object: <the thing, literal>, applies_to: <space-separated taxonomy labels, e.g. 'ui.forms code.style'>, condition?: <when>} — " +
  "rules with applies_to are guaranteed into the model's context whenever the exchange matches those activities; rules without applies_to ride every turn.";

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
          const { glyph, clean, extra, backfilled } = encodeFact({ name: String(args.name ?? ""), facts: args.facts });
          const near = await neighbors(s, p, glyph.globalCortex.data, DEFAULT_THRESHOLD, 8);
          const preserved = extraSlotNames(extra);
          return text(JSON.stringify({
            preview: { identifier: glyph.identifier, layers: Object.keys(clean), spaceId: glyph.spaceId },
            ...(preserved.length ? { preservedSlots: preserved } : {}),
            ...(backfilled ? { note: "no schema-valid slot filled — entity.name carries the encoding; consider re-slotting values into schema roles for stronger similarity" } : {}),
            candidates: near.map((n) => asFactJson(n.row, n.cos)),
            guidance: near.length
              ? "Reason over the candidates: same fact → update_fact with its id; related but distinct → create_fact; contradictory → update_fact the old one."
              : "No candidates above the gate — create_fact if this is worth remembering.",
          }, null, 1));
        }
        case "create_fact": {
          const created = await persistFact(s, p, {
            name: String(args.name ?? ""),
            facts: args.facts,
            ...(typeof args.confidence === "number" ? { confidence: args.confidence } : {}),
            ...(args.scope === "user" ? { scope: "user" as const } : {}),
          });
          return text(JSON.stringify(created));
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
          const { glyph, stored, primes } = encodeFact({ name, facts: args.facts });
          const confidence = clampConfidence(args.confidence);
          const old = (await s.byIds({ orgId: p.orgId, userId: p.userId }, [supersedes]))[0];
          if (!old) return errText(`no such fact: ${supersedes}`);
          await s.insert({ orgId: p.orgId, userId: p.userId }, {
            id: glyph.identifier, name: name || old.name, scope: old.scope,
            concept: { name: name || old.name, facts: stored },
            confidence, primes, citations: [supersedes], derived: old.derived,
            cortexB64: Buffer.from(glyph.globalCortex.data.buffer, glyph.globalCortex.data.byteOffset, glyph.globalCortex.data.byteLength).toString("base64"),
          });
          await s.supersede({ orgId: p.orgId, userId: p.userId }, supersedes, glyph.identifier);
          indexes.get(p.orgId)?.items.delete(supersedes);
          indexNewFact(p.orgId, glyph.identifier, glyph.globalCortex.data, name || old.name, stored, primes);
          void s.bumpLexicon({ orgId: p.orgId, userId: p.userId }, conceptWords(name || old.name, stored));
          return text(JSON.stringify({ id: glyph.identifier, supersedes, confidence }));
        }
        case "delete_fact": {
          const id = String(args.id ?? "");
          if (!id) return errText("delete_fact needs { id }");
          const removed = await s.tombstone({ orgId: p.orgId, userId: p.userId }, id);
          if (!removed) return errText(`no such live fact: ${id}`);
          indexes.get(p.orgId)?.items.delete(id);
          return text(JSON.stringify({ deleted: id }));
        }
        case "build_fact_tree": {
          const name = String(args.name ?? "");
          const citations = (Array.isArray(args.citations) ? args.citations : []).map(String).filter(Boolean);
          if (!citations.length) return errText("build_fact_tree needs { citations } — the source glyph ids the derivation reasons from");
          const { glyph, stored, primes } = encodeFact({ name, facts: args.facts });
          const confidence = clampConfidence(args.confidence);
          const sources = await s.byIds({ orgId: p.orgId, userId: p.userId }, citations);
          if (sources.length !== citations.length) {
            const found = new Set(sources.map((r) => r.id));
            return errText(`unknown citation ids: ${citations.filter((c) => !found.has(c)).join(", ")}`);
          }
          await s.insert({ orgId: p.orgId, userId: p.userId }, {
            id: glyph.identifier, name, scope: "org",
            concept: { name, facts: stored },
            confidence, primes, citations, derived: true,
            cortexB64: Buffer.from(glyph.globalCortex.data.buffer, glyph.globalCortex.data.byteOffset, glyph.globalCortex.data.byteLength).toString("base64"),
          });
          indexNewFact(p.orgId, glyph.identifier, glyph.globalCortex.data, name, stored, primes);
          void s.bumpLexicon({ orgId: p.orgId, userId: p.userId }, conceptWords(name, stored));

          const tree = new FactTree(name || "Derived fact");
          const { items } = await orgIndex(s, p);
          for (const src of sources) {
            const vec = items.get(src.id)?.cortex;
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
/**
 * The HTTP-facing memory surface (his order, 2026-08-25). The desktop's
 * ada_recall/ada_browse rode `/stator/*`, which no runtime ever served — a
 * 404 every time. ONE memory plane: those calls now land on the FACT
 * SUBSTRATE, the same ledger the per-turn fact block is drawn from. No second
 * vector store, no competing recall.
 */
export async function recallFacts(
  principal: Principal,
  query: string,
  topK = 5,
): Promise<{ block: string; facts: unknown[] }> {
  const s = await store();
  if (!s) return { block: "", facts: [] };
  const p = { orgId: principal.orgId, userId: principal.userId };
  const { glyph } = encodeFact({ name: query.slice(0, 120) || "probe", facts: { query: { text: query } } });
  const near = await neighbors(s, p, glyph.globalCortex.data, DEFAULT_THRESHOLD, Math.max(1, Math.min(20, topK)));
  const facts = near.map((n) => asFactJson(n.row, n.cos));
  const block = near.length
    ? near.map((n) => `- ${n.row.name}: ${JSON.stringify(n.row.concept)} (${n.cos.toFixed(3)})`).join("\n")
    : "";
  return { block, facts };
}

/** Every live fact for the caller — the REVIEW surface (ada_browse). */
export async function browseFacts(principal: Principal, limit = 200): Promise<{ block: string; facts: unknown[] }> {
  const s = await store();
  if (!s) return { block: "", facts: [] };
  const rows = await s.live({ orgId: principal.orgId, userId: principal.userId }, limit);
  return {
    block: rows.map((r) => `- ${r.id} · ${r.name}: ${JSON.stringify(r.concept)}`).join("\n"),
    facts: rows.map((r) => asFactJson(r, 1)),
  };
}

/** Append a NEW fact glyphh to the ledger — the one write path, shared by the
 *  create_fact tool and the HTTP create endpoint. */
async function persistFact(
  s: GlyphStore,
  p: Principal,
  input: { name: string; facts: unknown; confidence?: number; scope?: "org" | "user" },
): Promise<{ id: string; confidence: number; scope: "org" | "user"; preservedSlots?: string[] }> {
  const { glyph, stored, extra, primes } = encodeFact({ name: input.name, facts: input.facts });
  const scope = input.scope === "user" ? "user" as const : "org" as const;
  const confidence = clampConfidence(input.confidence);
  await s.insert({ orgId: p.orgId, userId: p.userId }, {
    id: glyph.identifier, name: input.name, scope,
    concept: { name: input.name, facts: stored },
    confidence, primes, citations: [], derived: false,
    cortexB64: Buffer.from(glyph.globalCortex.data.buffer, glyph.globalCortex.data.byteOffset, glyph.globalCortex.data.byteLength).toString("base64"),
  });
  indexNewFact(p.orgId, glyph.identifier, glyph.globalCortex.data, input.name, stored, primes);
  void s.bumpLexicon({ orgId: p.orgId, userId: p.userId }, conceptWords(input.name, stored));
  const preserved = extraSlotNames(extra);
  return { id: glyph.identifier, confidence, scope, ...(preserved.length ? { preservedSlots: preserved } : {}) };
}

/** Add a fact over HTTP (the Memory panel's "+ fact" and the e2e circle). */
export async function createFact(
  principal: Principal,
  input: { name?: string; facts?: unknown; confidence?: number; scope?: string },
): Promise<{ id: string; confidence: number; scope: "org" | "user"; preservedSlots?: string[] }> {
  const s = await store();
  if (!s) throw new Error("the fact ledger is not configured on this runtime (ROTOR_STATOR_URL unset)");
  return persistFact(s, principal, {
    name: String(input.name ?? "").trim(),
    facts: input.facts,
    ...(typeof input.confidence === "number" ? { confidence: input.confidence } : {}),
    ...(input.scope === "user" ? { scope: "user" as const } : {}),
  });
}

/** Tombstone a fact by id (ada_forget). */
export async function forgetFact(principal: Principal, id: string): Promise<{ forgotten: boolean }> {
  const s = await store();
  if (!s) return { forgotten: false };
  const p = { orgId: principal.orgId, userId: principal.userId };
  const existing = (await s.byIds(p, [id]))[0];
  if (!existing) return { forgotten: false };
  await s.tombstone(p, id);
  indexes.delete(principal.orgId);
  return { forgotten: true };
}

/** Correct a fact over HTTP (the Memory panel's edit): appends a new version
 *  and supersedes the old id — the ledger never mutates. The exact semantics
 *  of the update_fact tool, without an MCP round-trip. */
export async function amendFact(
  principal: Principal,
  input: { id: string; name?: string; facts?: unknown; confidence?: number },
): Promise<{ id: string; supersedes: string }> {
  const s = await store();
  if (!s) throw new Error("the fact ledger is not configured on this runtime (ROTOR_STATOR_URL unset)");
  const p = { orgId: principal.orgId, userId: principal.userId };
  const old = (await s.byIds(p, [input.id]))[0];
  if (!old) throw new Error(`no such fact: ${input.id}`);
  const name = (input.name ?? "").trim() || old.name;
  const facts = input.facts ?? (old.concept as { facts?: unknown }).facts;
  const { glyph, stored, primes } = encodeFact({ name, facts });
  const confidence = clampConfidence(input.confidence ?? old.confidence);
  await s.insert(p, {
    id: glyph.identifier, name, scope: old.scope,
    concept: { name, facts: stored },
    confidence, primes, citations: [input.id], derived: old.derived,
    cortexB64: Buffer.from(glyph.globalCortex.data.buffer, glyph.globalCortex.data.byteOffset, glyph.globalCortex.data.byteLength).toString("base64"),
  });
  await s.supersede(p, input.id, glyph.identifier);
  indexes.get(principal.orgId)?.items.delete(input.id);
  indexNewFact(principal.orgId, glyph.identifier, glyph.globalCortex.data, name, stored, primes);
  void s.bumpLexicon(p, conceptWords(name, stored));
  return { id: glyph.identifier, supersedes: input.id };
}

// ── The semantic space as data (the Memory panel's 3D view) ──────────────────

/** Cap the graph at a size whose pairwise cosine pass stays interactive —
 *  n²/2 dot products over the 10k-dim cortex. */
const GRAPH_MAX = 400;

/** Deterministic pseudo-random start vector (mulberry32) — same ledger, same
 *  projection, replayable renders. */
function seededVector(dim: number, seed: number): Float64Array {
  let a = seed >>> 0;
  const rand = (): number => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const v = new Float64Array(dim);
  for (let i = 0; i < dim; i++) v[i] = rand() - 0.5;
  return v;
}

/** Top-3 principal directions of the centered cortex cloud, by power
 *  iteration with deflation — exact PCA is overkill for a starfield. */
function pca3(vectors: Bipolar[], dim: number): Array<[number, number, number]> {
  const n = vectors.length;
  if (!n) return [];
  const mean = new Float64Array(dim);
  for (const v of vectors) for (let d = 0; d < dim; d++) mean[d]! += v[d]!;
  for (let d = 0; d < dim; d++) mean[d]! /= n;

  const comps: Float64Array[] = [];
  for (let c = 0; c < 3; c++) {
    let dir = seededVector(dim, SEED + c);
    for (let iter = 0; iter < 8; iter++) {
      const next = new Float64Array(dim);
      for (const v of vectors) {
        let dot = 0;
        for (let d = 0; d < dim; d++) dot += (v[d]! - mean[d]!) * dir[d]!;
        for (let d = 0; d < dim; d++) next[d]! += dot * (v[d]! - mean[d]!);
      }
      // Deflate against found components, then normalize.
      for (const prev of comps) {
        let proj = 0;
        for (let d = 0; d < dim; d++) proj += next[d]! * prev[d]!;
        for (let d = 0; d < dim; d++) next[d]! -= proj * prev[d]!;
      }
      let norm = 0;
      for (let d = 0; d < dim; d++) norm += next[d]! * next[d]!;
      norm = Math.sqrt(norm) || 1;
      for (let d = 0; d < dim; d++) next[d]! /= norm;
      dir = next;
    }
    comps.push(dir);
  }
  return vectors.map((v) => {
    const out: [number, number, number] = [0, 0, 0];
    for (let c = 0; c < 3; c++) {
      let dot = 0;
      for (let d = 0; d < dim; d++) dot += (v[d]! - mean[d]!) * comps[c]![d]!;
      out[c] = dot;
    }
    return out;
  });
}

export interface FactGraphNode {
  id: number;
  factId: string;
  name: string;
  /** "layer.role=value" lines — what the info card renders. */
  slots: string[];
  scope: string;
  derived: boolean;
  confidence: number;
  createdAt: string;
}

/** The org's live fact space as render-ready data: nodes, 3D PCA coords,
 *  cosine-banded similarity edges. Bands default to the glyph space's real
 *  spread (unrelated facts share the universal skeleton and sit well above
 *  zero, so the interesting bands start higher than the old stator's). */
export async function graphFacts(
  principal: Principal,
  opts: { max?: number; semMin?: number; neuralMin?: number; neuralMax?: number } = {},
): Promise<{ nodes: FactGraphNode[]; coords: Array<[number, number, number]>; edges: Array<{ a: number; b: number; sim: number; kind: "semantic" | "neural" }> }> {
  const s = await store();
  if (!s) return { nodes: [], coords: [], edges: [] };
  const max = Math.max(1, Math.min(GRAPH_MAX, Number(opts.max) || GRAPH_MAX));
  const semMin = typeof opts.semMin === "number" ? opts.semMin : 0.75;
  const neuralMin = typeof opts.neuralMin === "number" ? opts.neuralMin : 0.55;
  const neuralMax = typeof opts.neuralMax === "number" ? opts.neuralMax : semMin;

  const gp = { orgId: principal.orgId, userId: principal.userId };
  const { items } = await orgIndex(s, principal);
  const rows = (await s.live(gp, max)).filter((r) => items.has(r.id)).slice(0, max);
  const vectors = rows.map((r) => items.get(r.id)!.cortex);

  const nodes: FactGraphNode[] = rows.map((row, i) => {
    const facts = factsOf(row);
    const slots: string[] = [];
    for (const [layer, roles] of Object.entries(facts)) {
      for (const [role, value] of Object.entries(roles)) slots.push(`${layer}.${role}=${value}`);
    }
    return {
      id: i,
      factId: row.id,
      name: String((row.concept as { name?: string })?.name ?? row.name),
      slots,
      scope: row.scope,
      derived: row.derived,
      confidence: row.confidence,
      createdAt: row.createdAt,
    };
  });

  const coords = pca3(vectors, DIM);
  const edges: Array<{ a: number; b: number; sim: number; kind: "semantic" | "neural" }> = [];
  for (let a = 0; a < vectors.length; a++) {
    for (let b = a + 1; b < vectors.length; b++) {
      const sim = cosineSimilarity(vectors[a]!, vectors[b]!);
      if (sim >= semMin) edges.push({ a, b, sim: Number(sim.toFixed(4)), kind: "semantic" });
      else if (sim >= neuralMin && sim < neuralMax) edges.push({ a, b, sim: Number(sim.toFixed(4)), kind: "neural" });
    }
  }
  return { nodes, coords, edges };
}

/** Reserved directive slots inside {@link FACT_BLOCK_MAX} — a rule that fires
 *  needlessly costs one slot; one that fails to fire costs correctness, so
 *  both gates are tuned permissive. A rule's label match is a DIRECT cosine
 *  against its own gloss (no top-k competition — "is this exchange near MY
 *  activity?"), words-only on both sides. */
const DIRECTIVE_SLOTS = 4;
const DIRECTIVE_TRIGGER_GATE = 0.08;
const LABEL_GATE = 0.06;

export async function renderFactBlock(principal: Principal, exchangeText: string): Promise<string | null> {
  const s = await store();
  if (!s) return null;
  try {
    const gp = { orgId: principal.orgId, userId: principal.userId };
    const epoch = await orgIndex(s, principal);
    const rows = await s.live(gp);
    if (!rows.length) return null;
    // Exchange words are inbound words — the lexicon counts them (best-effort,
    // off the selection path; they weigh in at the NEXT hydration epoch).
    void s.bumpLexicon(gp, tokenize(exchangeText));

    const vEx = exchangeVector(exchangeText, epoch.lexicon);
    const vExWords = wordsVector(exchangeText, epoch.lexicon);
    const now = Date.now();

    const cands = rows.map((row) => {
      const facts = factsOf(row);
      const name = String((row.concept as { name?: string })?.name ?? row.name);
      const dict = epoch.items.get(row.id)?.dict ?? factDictVector(name, facts, row.primes, epoch.lexicon);
      const cos = dictCosine(vEx, dict);
      const directive = directiveOf(facts);
      // Force-include a directive when the exchange is ABOUT it: its own
      // gloss cosine clears the gate (the synonymy bridge), the permissive
      // trigger cosine clears (the atom lane), or it has no applies_to at
      // all (a standing rule rides every turn).
      const forced = !!directive && (
        directive.appliesTo.length === 0 ||
        directive.appliesTo.some((l) => {
          const gloss = epoch.glosses.get(l);
          return gloss !== undefined && dictCosine(vExWords, gloss) >= LABEL_GATE;
        }) ||
        cos >= DIRECTIVE_TRIGGER_GATE
      );
      const ageDays = Math.max(0, (now - Date.parse(row.createdAt)) / 86_400_000);
      const score = cos + row.confidence * 0.02 - Math.min(ageDays / 365, 0.05);
      return { row, cos, score, directive, forced };
    });

    // Directives own their reserved slots (confidence-ranked); untriggered
    // directives stay OUT entirely — they never crowd the fact slots.
    const directives = cands
      .filter((c) => c.directive && c.forced)
      .sort((a, b) => b.row.confidence - a.row.confidence || b.cos - a.cos)
      .slice(0, DIRECTIVE_SLOTS);
    const facts = cands
      .filter((c) => !c.directive)
      .sort((a, b) => b.score - a.score);

    const lines: string[] = [FACT_BLOCK_HEADER];
    let chars = FACT_BLOCK_HEADER.length;
    let taken = 0;
    for (const { row } of [...directives, ...facts]) {
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

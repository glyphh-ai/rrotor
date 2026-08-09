/**
 * harness/stator-api.ts — the STATOR API: recall/write the regional stator over
 * HTTP so a LOCAL runtime (which cannot reach the private pgvector) shares the
 * SAME memory plane as a cloud runtime. See server/docs/runtime-stator-split.md.
 *
 * SECURITY — multi-tenant. `PgVectorStore` is single-tenant by design
 * (docs/memory.md: "one runtime = one user"), so this shared API adds the tenant
 * boundary itself: every call is OWNER-SCOPED to the introspected principal by
 * pinning `SET search_path TO "<schemaForOrg(orgId)>", public` on a dedicated
 * single connection BEFORE any DDL or query (mirroring ThreadStore.scoped). One
 * org's memory can never touch another's; within the schema, facts are keyed by
 * `entity` (the user). An unattributed caller (no principal) is refused upstream.
 *
 * SPACE — every runtime MUST bind the SAME HDC space or recall silently misses,
 * so the API fixes it (the base-memory.rotor.yaml defaults) for all callers.
 */

import type { Principal } from "../auth/introspect.js";
import { connectPg, PgVectorStore } from "../exec/pgvector-store.js";
import { schemaForOrg } from "./threads.js";
import { BasicMemory } from "../plugins/memory.js";
import { BasicGrounding } from "../plugins/grounding.js";
import { recallContext } from "../exec/recall.js";
import { absorbText } from "../handlers/memory.js";
import { enrichFacts } from "./enricher.js";
import type { MemoryTier } from "../exec/facts.js";

// The ONE space every runtime shares (base-memory.rotor.yaml). Drift = recall misses.
const SPACE_DIM = 10000;
const SPACE_SEED = 42;
const SPACE_ROLES = "universal-7x33";
const DEFAULT_ENTITY = "user";

/**
 * Run `fn` against a memory plugin scoped to the principal's org schema. A fresh
 * single connection per call; `search_path` pinned BEFORE the store's DDL and
 * every read/write, so all of it lands in the org's schema — never public, never
 * another org. `max: 1` keeps the whole op on the one connection the `SET` bound.
 */
async function withScopedMemory<T>(
  principal: Principal,
  fn: (memory: BasicMemory, spaceId: string) => Promise<T>,
): Promise<T> {
  const url = process.env.ROTOR_STATOR_URL;
  if (!url) throw new Error("stator not configured (ROTOR_STATOR_URL)");
  const client = await connectPg(url, { max: 1 });
  try {
    const schema = schemaForOrg(principal.orgId);
    // CREATE the schema before pinning — Postgres SILENTLY drops a nonexistent
    // schema from search_path, so without this every op falls through to
    // public: a SHARED bucket across orgs. (ThreadStore.scoped does the same;
    // omitting it here was a real cross-tenant bug, caught in test.)
    await client.query(`CREATE SCHEMA IF NOT EXISTS "${schema}"`);
    await client.query(`SET search_path TO "${schema}", public`);
    const store = await PgVectorStore.create({ client });
    const memory = new BasicMemory(store);
    const grounding = new BasicGrounding(store);
    const spaceId = grounding.computeSpaceId(SPACE_DIM, SPACE_SEED, SPACE_ROLES);
    return await fn(memory, spaceId);
  } finally {
    await (client as { end?: () => Promise<void> }).end?.().catch(() => {});
  }
}

export interface RecallRequest { threadId?: string; query?: string; entity?: string; topK?: number; threshold?: number }
export interface WriteRequest { threadId?: string; entity?: string; mode?: "turn" | "absorb"; text?: string; speaker?: string; tier?: MemoryTier; date?: string }

/** POST /stator/recall — the recall block + the entity's fact node for a turn.
 *  Fact selection is relevance-ranked and latest-first (see recallContext), so an
 *  early-stated fact isn't lost to a recency cap and the current value leads. */
export async function statorRecall(principal: Principal, body: RecallRequest): Promise<unknown> {
  const entity = (body.entity ?? "").trim() || DEFAULT_ENTITY;
  const session = (body.threadId ?? "").trim() || undefined;
  const query = String(body.query ?? "");
  return withScopedMemory(principal, async (memory, spaceId) => {
    return recallContext(memory, query, {
      entity,
      spaceId,
      ...(session ? { session } : {}),
      ...(typeof body.topK === "number" ? { topK: body.topK } : {}),
      ...(typeof body.threshold === "number" ? { threshold: body.threshold } : {}),
    });
  });
}

/** POST /stator/write — persist a `turn` (recency + similarity corpus) or `absorb` facts. */
export async function statorWrite(principal: Principal, body: WriteRequest): Promise<unknown> {
  const entity = (body.entity ?? "").trim() || DEFAULT_ENTITY;
  const session = (body.threadId ?? "").trim() || undefined;
  const text = String(body.text ?? "");
  const speaker = (body.speaker ?? "").trim() || "user";
  const mode: "turn" | "absorb" = body.mode === "absorb" ? "absorb" : "turn";
  return withScopedMemory(principal, async (memory, spaceId) => {
    if (mode === "turn") {
      // Stamp the turn with its occurrence date when provided, so temporal
      // questions ("how many days between…") can be answered from recalled
      // context — facts carry no absolute date, the corpus does.
      const stamp = (body.date ?? "").trim();
      await memory.appendConversation(session ?? "default", speaker, text);
      await memory.recordTurn(`${stamp ? `[${stamp}] ` : ""}${speaker}: ${text}`);
      return { written: text.trim() ? 1 : 0, mode };
    }
    // Schema-on-write: the LLM enricher (qwen3 via the local model host) when
    // configured, the deterministic absorbText floor otherwise or on any failure.
    const facts = (await enrichFacts(text, entity)) ?? absorbText(text, entity);
    const written = await memory.write(facts as Array<Record<string, unknown>>, {
      key: entity,
      mode: "absorb",
      speaker,
      spaceId,
      ...(session ? { session } : {}),
      ...(body.tier ? { tier: body.tier } : {}),
    });
    return { written, mode };
  });
}

/** The recall block for a turn — plain text to fold into the system prompt.
 *  Best-effort: any failure yields "" so a turn NEVER fails on memory. */
export async function recallForTurn(
  principal: Principal, threadId: string | undefined, prompt: string,
  opts: { topK?: number; threshold?: number; entity?: string } = {},
): Promise<string> {
  try {
    const r = (await statorRecall(principal, {
      threadId, query: prompt,
      ...(opts.topK !== undefined ? { topK: opts.topK } : {}),
      ...(opts.threshold !== undefined ? { threshold: opts.threshold } : {}),
      ...(opts.entity ? { entity: opts.entity } : {}),
    })) as { block?: string };
    return r.block ?? "";
  } catch {
    return "";
  }
}

/** Persist a completed exchange: log both turns (recency + similarity corpus) and
 *  absorb the USER turn into facts. Best-effort — a memory write never fails a turn. */
export async function persistTurn(
  principal: Principal, threadId: string | undefined, userText: string, assistantText: string, entity = "user",
): Promise<void> {
  try {
    await statorWrite(principal, { threadId, mode: "turn", speaker: "user", text: userText });
    await statorWrite(principal, { threadId, mode: "absorb", entity, text: userText });
    if (assistantText.trim()) {
      await statorWrite(principal, { threadId, mode: "turn", speaker: "assistant", text: assistantText });
    }
  } catch {
    /* memory is best-effort */
  }
}

// ── the API path: a LOCAL (auth-off) pod recalls/writes the REGIONAL stator over
//    the control plane (/api/stator/*), bearer = the run's runtimeToken (the
//    user's access token on a local pod). Zero new config: the control base is
//    derived from the gatewayUrl every run already carries. ──────────────────────

/** `<control>/api/gateway` → `<control>`, or null when the shape is unknown. */
export function controlBaseFromGateway(gatewayUrl: string | undefined): string | null {
  if (!gatewayUrl) return null;
  const m = /^(.*)\/api\/gateway\/?$/.exec(gatewayUrl.trim());
  return m ? m[1] : null;
}

async function postStator(base: string, bearer: string, path: string, body: unknown): Promise<unknown> {
  const res = await fetch(`${base}/api/stator/${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${bearer}` },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`stator api ${path}: HTTP ${res.status}`);
  return res.json();
}

/** recallForTurn over the control-plane stator API. Best-effort → "" on failure. */
export async function recallForTurnViaApi(
  base: string, bearer: string, threadId: string | undefined, prompt: string,
  opts: { topK?: number; threshold?: number; entity?: string } = {},
): Promise<string> {
  try {
    const r = (await postStator(base, bearer, "recall", { threadId, query: prompt, ...opts })) as { block?: string };
    return r.block ?? "";
  } catch {
    return "";
  }
}

/** persistTurn over the control-plane stator API. Best-effort. */
export async function persistTurnViaApi(
  base: string, bearer: string, threadId: string | undefined, userText: string, assistantText: string, entity = "user",
): Promise<void> {
  try {
    await postStator(base, bearer, "write", { threadId, mode: "turn", speaker: "user", text: userText });
    await postStator(base, bearer, "write", { threadId, mode: "absorb", entity, text: userText });
    if (assistantText.trim()) {
      await postStator(base, bearer, "write", { threadId, mode: "turn", speaker: "assistant", text: assistantText });
    }
  } catch {
    /* memory is best-effort */
  }
}

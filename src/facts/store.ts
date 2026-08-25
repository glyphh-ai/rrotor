/**
 * facts/store.ts — the GLYPH LEDGER, org-schema resident.
 *
 * The append-only record of an org's facts as canonical glyphhs: every row is
 * one id'd, timestamped glyphh (identifier `name@ts#vN`) carrying its NSM
 * concept JSON, a confidence, its citations (a derived glyphh names the
 * source glyphhs it was reasoned from), and the encoded cortex bytes. An
 * UPDATE never mutates — it writes a new glyphh and stamps `superseded_by`
 * on the old one; a DELETE is a tombstone. The current fact set = rows with
 * neither. The in-memory HDC index (facts/server.ts) is DERIVED from this
 * ledger — concepts are re-encoded on hydration; the concept JSON is the one
 * authority and vectors never cross engines.
 *
 * Same store mechanics as threads.ts, deliberately: one connection, an op
 * mutex, `SET search_path` pinned per operation, DDL ensured on first touch
 * of an org, owner columns from the introspected principal.
 */

import { connectPg } from "../exec/pgvector-store.js";
import type { PgLike } from "../exec/pgvector-store.js";
import { schemaForOrg } from "../harness/threads.js";
import { log } from "../obs/logger.js";

export interface GlyphRow {
  id: string;
  name: string;
  scope: "org" | "user";
  userId: string | null;
  concept: unknown;
  confidence: number;
  citations: string[];
  derived: boolean;
  cortexB64: string;
  supersededBy: string | null;
  createdAt: string;
}

export interface GlyphPrincipal { orgId: string; userId: string }

const DDL = `
CREATE TABLE IF NOT EXISTS glyphs (
  id            TEXT PRIMARY KEY,
  org_id        TEXT NOT NULL,
  user_id       TEXT,
  name          TEXT NOT NULL,
  scope         TEXT NOT NULL DEFAULT 'org',
  concept       JSONB NOT NULL,
  confidence    REAL NOT NULL DEFAULT 0.7,
  citations     JSONB NOT NULL DEFAULT '[]',
  derived       BOOLEAN NOT NULL DEFAULT FALSE,
  cortex        BYTEA NOT NULL,
  superseded_by TEXT,
  deleted_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS glyphs_live_ix ON glyphs (org_id, created_at DESC);
`;

/** node-postgres reports rowCount; PGlite (tests) reports affectedRows. */
const affected = (r: unknown): number => {
  const x = r as { rowCount?: number | null; affectedRows?: number | null };
  return x.rowCount ?? x.affectedRows ?? 0;
};

interface Row {
  id: string; name: string; scope: string; user_id: string | null; concept: unknown;
  confidence: number; citations: unknown; derived: boolean; cortex: Buffer;
  superseded_by: string | null; created_at: string;
}

const toRow = (r: Row): GlyphRow => ({
  id: r.id,
  name: r.name,
  scope: r.scope === "user" ? "user" : "org",
  userId: r.user_id,
  concept: r.concept,
  confidence: Number(r.confidence),
  citations: Array.isArray(r.citations) ? (r.citations as string[]).map(String) : [],
  derived: !!r.derived,
  cortexB64: Buffer.from(r.cortex).toString("base64"),
  supersededBy: r.superseded_by,
  createdAt: String(r.created_at),
});

export class GlyphStore {
  private readonly ensured = new Set<string>();
  private chain: Promise<unknown> = Promise.resolve();

  private constructor(private readonly db: PgLike, private readonly owned: boolean) {}

  static async create(opts: { url?: string | undefined; client?: PgLike } = {}): Promise<GlyphStore> {
    const db = opts.client ?? (await connectPg(opts.url, { max: 1 }));
    return new GlyphStore(db, !opts.client);
  }

  private scoped<T>(orgId: string, fn: () => Promise<T>): Promise<T> {
    const run = this.chain.then(async () => {
      const schema = schemaForOrg(orgId);
      if (!this.ensured.has(schema)) {
        await this.db.query(`CREATE SCHEMA IF NOT EXISTS "${schema}"`);
        await this.db.query(`SET search_path TO "${schema}", public`);
        if (this.db.exec) await this.db.exec(DDL);
        else await this.db.query(DDL);
        this.ensured.add(schema);
        log.info("glyph schema ensured", { schema });
      } else {
        await this.db.query(`SET search_path TO "${schema}", public`);
      }
      return fn();
    });
    this.chain = run.catch(() => undefined);
    return run;
  }

  /** Append one glyphh. The id comes from the ENCODER (name@ts#vN) — the
   *  bridge stamps it; the ledger never invents identity. */
  insert(p: GlyphPrincipal, g: {
    id: string; name: string; scope: "org" | "user"; concept: unknown;
    confidence: number; citations: string[]; derived: boolean; cortexB64: string;
  }): Promise<void> {
    return this.scoped(p.orgId, async () => {
      await this.db.query(
        `INSERT INTO glyphs (id, org_id, user_id, name, scope, concept, confidence, citations, derived, cortex)
         VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8::jsonb,$9,$10)`,
        [g.id, p.orgId, g.scope === "user" ? p.userId : null, g.name, g.scope,
         JSON.stringify(g.concept ?? {}), g.confidence, JSON.stringify(g.citations ?? []),
         g.derived, Buffer.from(g.cortexB64, "base64")],
      );
    });
  }

  /** Chain a new version over an old id (the UPDATE motion). */
  supersede(p: GlyphPrincipal, oldId: string, newId: string): Promise<boolean> {
    return this.scoped(p.orgId, async () => {
      const r = await this.db.query(
        `UPDATE glyphs SET superseded_by = $3 WHERE id = $1 AND org_id = $2 AND superseded_by IS NULL AND deleted_at IS NULL`,
        [oldId, p.orgId, newId],
      );
      return affected(r) > 0;
    });
  }

  /** Tombstone (the DELETE motion) — the history stays, the fact leaves. */
  tombstone(p: GlyphPrincipal, id: string): Promise<boolean> {
    return this.scoped(p.orgId, async () => {
      const r = await this.db.query(
        `UPDATE glyphs SET deleted_at = now() WHERE id = $1 AND org_id = $2 AND deleted_at IS NULL`,
        [id, p.orgId],
      );
      return affected(r) > 0;
    });
  }

  /** Every LIVE glyphh (neither superseded nor tombstoned) — the fact set the
   *  item store hydrates from. */
  live(p: GlyphPrincipal, limit = 2000): Promise<GlyphRow[]> {
    return this.scoped(p.orgId, async () => {
      const r = await this.db.query(
        `SELECT id, name, scope, user_id, concept, confidence, citations, derived, cortex, superseded_by, created_at
           FROM glyphs WHERE org_id = $1 AND superseded_by IS NULL AND deleted_at IS NULL
          ORDER BY created_at DESC LIMIT $2`,
        [p.orgId, limit],
      );
      return (r.rows as unknown as Row[]).map(toRow);
    });
  }

  /** Specific glyphhs by id (live or not — history is readable, that is the point). */
  byIds(p: GlyphPrincipal, ids: string[]): Promise<GlyphRow[]> {
    if (!ids.length) return Promise.resolve([]);
    return this.scoped(p.orgId, async () => {
      const r = await this.db.query(
        `SELECT id, name, scope, user_id, concept, confidence, citations, derived, cortex, superseded_by, created_at
           FROM glyphs WHERE org_id = $1 AND id = ANY($2)`,
        [p.orgId, ids],
      );
      return (r.rows as unknown as Row[]).map(toRow);
    });
  }

  async close(): Promise<void> {
    if (this.owned && this.db.end) await this.db.end();
  }
}

/** Build from the same stator env threads use — absent config means the fact
 *  tools report themselves unavailable rather than half-working. */
export function glyphStoreFromEnv(env: NodeJS.ProcessEnv = process.env): Promise<GlyphStore | null> {
  if (!env.ROTOR_STATOR_URL) return Promise.resolve(null);
  return GlyphStore.create({ url: env.ROTOR_STATOR_URL })
    .then((s) => { log.info("glyph ledger enabled (stator)", {}); return s; })
    .catch((err: unknown) => {
      log.error("glyph store connect failed", { detail: (err as Error).message });
      return null;
    });
}

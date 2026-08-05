/**
 * harness/threads.ts — SESSION THREAD PERSISTENCE on the stator.
 *
 * The harness pod writes every session's transcript to the stator Postgres as
 * the run streams, so every surface (desktop, web, server) reads ONE store
 * instead of each keeping a private cache. Two pieces live here:
 *
 *   • {@link ThreadStore} — `threads` + `thread_messages` over the SAME
 *     connection mechanism as the pgvector stator (exec/pgvector-store.ts:
 *     a {@link PgLike} client, `connectPg`, idempotent DDL at create). The
 *     SCHEMA IS THE SERVER'S: the server-side migration is the DDL authority
 *     and the ensure-DDL below byte-matches it — the pod only guarantees the
 *     tables exist on first touch of an org. A SHARED pod serves many orgs
 *     through ONE static DSN, so every operation is scoped to the caller's
 *     org with the server's deterministic convention ({@link schemaForOrg},
 *     mirroring server/src/db/tenant.ts): `SET search_path TO <org schema>,
 *     public` pinned per operation on the store's single serialized
 *     connection (pool max 1 + an op mutex — no interleaving between the SET
 *     and the queries it scopes) — the server reads the same per-org schemas
 *     via its tenantDb routing. WHO owns a thread within the org is explicit:
 *     every row carries `org_id`/`user_id` from the introspected runtime-token
 *     {@link Principal}, and every read/write here is owner-scoped so a pod
 *     shared by an org cannot leak threads across users.
 *   • {@link ThreadRecorder} — subscribes to a {@link HarnessSession}'s frame
 *     stream and translates frames into messages with the DESKTOP'S transcript
 *     grammar (app/src/renderer/help/code.ts CodeMsg): the user turn lands at
 *     run start (a crashed run still has it), streamed text upserts a live
 *     assistant row (throttled), tool start/done rows mirror the renderer's
 *     `⚙` / `✓ ✕` breadcrumbs, and the terminal frame commits the tail.
 *
 * Messages are stored AS-IS in jsonb (role/text/at + optional kind/attachments/
 * decline/connect) — the CodeMsg shape is the contract; no normalization.
 * Message order is an explicit `ord` (max+1 per thread, assigned at append —
 * writes are serialized). Sync is last-write-wins on `updated_at` (epoch ms,
 * the same clock as CodeMsg.at): a client PUT applies only when its stamp is
 * newer, and a tombstone (`deleted_at`) is terminal.
 */

import { connectPg, asJson } from "../exec/pgvector-store.js";
import type { PgLike } from "../exec/pgvector-store.js";
import type { Principal } from "../auth/introspect.js";
import type { HarnessRunConfig, SessionMode } from "./config.js";
import type { WireFrame } from "./frames.js";
import type { HarnessSession } from "./session.js";
import { log } from "../obs/logger.js";
import type { Logger } from "../obs/logger.js";

/** One transcript message — the desktop's CodeMsg, stored verbatim in jsonb.
 *  Extra fields (attachments/decline/connect) ride along untyped. */
export interface ThreadMsg {
  role: "user" | "assistant";
  text: string;
  at: number;
  kind?: "tool" | "decline" | "connect";
  [k: string]: unknown;
}

/** List-view metadata — everything but the messages. */
export interface ThreadMeta {
  id: string;
  title: string;
  mode: SessionMode;
  createdAt: number;
  updatedAt: number;
  messageCount: number;
}

/** A full thread: metadata + (capped) messages + the workspace source. */
export interface ThreadFull extends ThreadMeta {
  source?: unknown;
  messages: ThreadMsg[];
}

/** The client-push (PUT) payload — LWW: applies only when `updatedAt` beats
 *  the stored stamp. `messages` replaces the whole transcript when present. */
export interface ThreadPut {
  title?: string;
  mode?: SessionMode;
  source?: unknown;
  messages?: ThreadMsg[];
  updatedAt: number;
}

/** A PUT's outcome: `owned` false = the id belongs to another principal (the
 *  route answers 404 — existence is not revealed); `applied` false with
 *  `owned` true = the incoming stamp lost LWW (409, `updatedAt` wins). */
export interface PutResult {
  applied: boolean;
  owned: boolean;
  updatedAt: number;
}

const SESSION_MODES: SessionMode[] = ["chat", "cowork", "code"];

/** How many of the LATEST messages a full read returns. */
export const THREAD_MSG_CAP = 500;

/** The server's deterministic per-org schema convention (server/src/db/
 *  tenant.ts `schemaForOrg`): `org_` + the orgId with dashes stripped,
 *  lowercased. Guarded exactly like the server's quoteSchema idiom — anything
 *  outside ^[a-z0-9_]+$ is rejected, so the name is safe to interpolate. */
export function schemaForOrg(orgId: string): string {
  const schema = `org_${orgId.replace(/-/g, "")}`.toLowerCase();
  if (schema === "org_" || !/^[a-z0-9_]+$/.test(schema)) throw new Error(`unsafe schema name: ${schema}`);
  return schema;
}

// The server's migration is the DDL AUTHORITY — the table bodies below
// byte-match it; the pod's ensure-DDL only creates them on a fresh schema.
// Epoch-ms BIGINT stamps match CodeMsg.at, so LWW compares are plain numbers.
const DDL = `
CREATE TABLE IF NOT EXISTS threads (
  id TEXT PRIMARY KEY,
  org_id UUID NOT NULL,
  user_id UUID NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL DEFAULT 'code',
  source JSONB,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  deleted_at BIGINT
);
CREATE TABLE IF NOT EXISTS thread_messages (
  thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  ord BIGINT NOT NULL,
  msg JSONB NOT NULL,
  PRIMARY KEY (thread_id, ord)
);
CREATE INDEX IF NOT EXISTS ix_threads_user_updated ON threads(user_id, updated_at DESC);
`;

export interface ThreadStoreOptions {
  /** Inject a ready client (tests pass a PGlite instance). */
  client?: PgLike;
  /** Postgres connection string (the provisioned stator DSN). */
  url?: string;
}

export class ThreadStore {
  /** Orgs whose schema + tables this store has already ensured. */
  private readonly ensured = new Set<string>();
  /** The op mutex: every operation (ensure + SET search_path + its queries)
   *  runs alone on the single connection, so the pinned path never shifts
   *  under an in-flight operation. */
  private chain: Promise<unknown> = Promise.resolve();

  private constructor(
    private readonly db: PgLike,
    /** True when this store opened the connection (and must close it). */
    private readonly owned: boolean,
  ) {}

  /** Connect (or adopt an injected client). The pg pool is capped at ONE
   *  connection — `SET search_path` is session-scoped, so every query must
   *  ride the same session. Schemas/tables are ensured lazily per org
   *  ({@link scoped}), not here: a shared pod does not know its orgs up
   *  front, and a pod can see an org before the server's boot backfill. */
  static async create(opts: ThreadStoreOptions = {}): Promise<ThreadStore> {
    const db = opts.client ?? (await connectPg(opts.url, { max: 1 }));
    return new ThreadStore(db, !opts.client);
  }

  /** Run one operation scoped to the principal's org schema: take the mutex,
   *  ensure the schema + tables exist (first touch of the org), pin
   *  `search_path` to it, then run the queries. The schema name is derived —
   *  and guarded — by {@link schemaForOrg}. */
  private scoped<T>(p: Principal, fn: () => Promise<T>): Promise<T> {
    const run = this.chain.then(async () => {
      const schema = schemaForOrg(p.orgId);
      if (!this.ensured.has(schema)) {
        await this.db.query(`CREATE SCHEMA IF NOT EXISTS "${schema}"`);
        await this.db.query(`SET search_path TO "${schema}", public`);
        if (this.db.exec) await this.db.exec(DDL);
        else await this.db.query(DDL); // node-postgres runs multi-statement simple queries
        this.ensured.add(schema);
        log.info("thread schema ensured", { schema });
      } else {
        await this.db.query(`SET search_path TO "${schema}", public`);
      }
      return fn();
    });
    // The mutex survives a failed op; the failure still rejects `run`.
    this.chain = run.catch(() => undefined);
    return run;
  }

  /** Ensure the thread row exists for `p` and its stamp covers `at`. Never
   *  overwrites client-owned fields (title/mode/source) on an existing thread —
   *  the run path only inserts defaults; the client's PUT is authoritative for
   *  those — and never touches a row another principal owns. */
  async touch(p: Principal, id: string, at: number, fields: { mode?: SessionMode; source?: unknown } = {}): Promise<void> {
    await this.scoped(p, () =>
      this.db.query(
        "INSERT INTO threads (id, org_id, user_id, title, mode, source, created_at, updated_at) VALUES ($1,$2,$3,'',$4,$5,$6,$6) " +
          "ON CONFLICT (id) DO UPDATE SET updated_at = GREATEST(threads.updated_at, excluded.updated_at) " +
          "WHERE threads.org_id = excluded.org_id AND threads.user_id = excluded.user_id",
        [id, p.orgId, p.userId, fields.mode ?? "code", fields.source != null ? JSON.stringify(fields.source) : null, at],
      ),
    );
  }

  /** Append one message at the thread's next `ord`; bumps the thread stamp.
   *  Returns the ord so a streaming writer can {@link amend} the row in place,
   *  or null when `p` does not own thread `id` (nothing is written). */
  async append(p: Principal, id: string, msg: ThreadMsg): Promise<number | null> {
    return this.scoped(p, async () => {
      const rows = (
        await this.db.query(
          "INSERT INTO thread_messages (thread_id, ord, msg) " +
            "SELECT t.id, COALESCE((SELECT MAX(m.ord) + 1 FROM thread_messages m WHERE m.thread_id = t.id), 0), $4 " +
            "FROM threads t WHERE t.id = $1 AND t.org_id = $2 AND t.user_id = $3 RETURNING ord",
          [id, p.orgId, p.userId, JSON.stringify(msg)],
        )
      ).rows;
      if (!rows.length) return null;
      await this.stamp(p, id, msg.at);
      return Number(rows[0].ord);
    });
  }

  /** Replace a message in place (the live streamed row / tool-done rewrite). */
  async amend(p: Principal, id: string, ord: number, msg: ThreadMsg): Promise<void> {
    await this.scoped(p, async () => {
      await this.db.query(
        "UPDATE thread_messages SET msg = $4 WHERE thread_id = $1 AND ord = $5 " +
          "AND EXISTS (SELECT 1 FROM threads t WHERE t.id = $1 AND t.org_id = $2 AND t.user_id = $3)",
        [id, p.orgId, p.userId, JSON.stringify(msg), ord],
      );
      await this.stamp(p, id, msg.at);
    });
  }

  private async stamp(p: Principal, id: string, at: number): Promise<void> {
    await this.db.query(
      "UPDATE threads SET updated_at = GREATEST(updated_at, $4) WHERE id = $1 AND org_id = $2 AND user_id = $3",
      [id, p.orgId, p.userId, at],
    );
  }

  /** The principal's live threads, newest first — metadata only, tombstones
   *  hidden. Owner-scoped: another user's threads never appear. */
  async list(p: Principal): Promise<ThreadMeta[]> {
    const rows = await this.scoped(p, async () =>
      (
        await this.db.query(
          "SELECT t.id, t.title, t.mode, t.created_at, t.updated_at, " +
            "(SELECT COUNT(*) FROM thread_messages m WHERE m.thread_id = t.id) AS n " +
            "FROM threads t WHERE t.org_id = $1 AND t.user_id = $2 AND t.deleted_at IS NULL ORDER BY t.updated_at DESC",
          [p.orgId, p.userId],
        )
      ).rows,
    );
    return rows.map((r) => ({
      id: String(r.id),
      title: String(r.title),
      mode: String(r.mode) as SessionMode,
      createdAt: Number(r.created_at),
      updatedAt: Number(r.updated_at),
      messageCount: Number(r.n),
    }));
  }

  /** One full thread of the principal's (latest {@link THREAD_MSG_CAP}
   *  messages, in order), or undefined when absent, tombstoned, or not owned. */
  async get(p: Principal, id: string, cap = THREAD_MSG_CAP): Promise<ThreadFull | undefined> {
    return this.scoped(p, () => this.getScoped(p, id, cap));
  }

  private async getScoped(p: Principal, id: string, cap: number): Promise<ThreadFull | undefined> {
    const rows = (
      await this.db.query(
        "SELECT id, title, mode, source, created_at, updated_at, deleted_at FROM threads WHERE id = $1 AND org_id = $2 AND user_id = $3",
        [id, p.orgId, p.userId],
      )
    ).rows;
    if (!rows.length || rows[0].deleted_at != null) return undefined;
    const t = rows[0];
    const msgs = (
      await this.db.query(
        "SELECT msg FROM (SELECT ord, msg FROM thread_messages WHERE thread_id = $1 ORDER BY ord DESC LIMIT $2) sub ORDER BY ord ASC",
        [id, cap],
      )
    ).rows;
    const count = (await this.db.query("SELECT COUNT(*) AS n FROM thread_messages WHERE thread_id = $1", [id])).rows;
    const source = t.source == null ? undefined : asJson(t.source);
    return {
      id: String(t.id),
      title: String(t.title),
      mode: String(t.mode) as SessionMode,
      ...(source !== undefined ? { source } : {}),
      createdAt: Number(t.created_at),
      updatedAt: Number(t.updated_at),
      messageCount: Number(count[0].n),
      messages: msgs.map((r) => asJson(r.msg) as ThreadMsg),
    };
  }

  /** The client-push seam (PUT): apply only when the incoming stamp is NEWER
   *  than the stored one; a tombstone always wins; another principal's thread
   *  is untouchable (`owned` false — the id is theirs). Creates the thread
   *  under `p` when it does not exist yet (client-generated ids like `c<ts36>`
   *  are the norm). Returns the winning stamp so a losing client can pull. */
  async put(p: Principal, id: string, patch: ThreadPut): Promise<PutResult> {
    return this.scoped(p, () => this.putScoped(p, id, patch));
  }

  private async putScoped(p: Principal, id: string, patch: ThreadPut): Promise<PutResult> {
    const rows = (
      await this.db.query("SELECT org_id, user_id, title, mode, source, updated_at, deleted_at FROM threads WHERE id = $1", [id])
    ).rows;
    const cur = rows[0];
    if (cur && (String(cur.org_id) !== p.orgId || String(cur.user_id) !== p.userId)) {
      return { applied: false, owned: false, updatedAt: 0 };
    }
    if (cur && (cur.deleted_at != null || Number(cur.updated_at) >= patch.updatedAt)) {
      return { applied: false, owned: true, updatedAt: Math.max(Number(cur.updated_at), Number(cur.deleted_at ?? 0)) };
    }
    const title = patch.title ?? (cur ? String(cur.title) : "");
    const mode = patch.mode ?? (cur ? (String(cur.mode) as SessionMode) : "code");
    const source = patch.source !== undefined ? patch.source : cur ? asJson(cur.source) : null;
    await this.db.query(
      "INSERT INTO threads (id, org_id, user_id, title, mode, source, created_at, updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$7) " +
        "ON CONFLICT (id) DO UPDATE SET title = excluded.title, mode = excluded.mode, source = excluded.source, updated_at = excluded.updated_at",
      [id, p.orgId, p.userId, title, mode, source != null ? JSON.stringify(source) : null, patch.updatedAt],
    );
    if (patch.messages) {
      await this.db.query("DELETE FROM thread_messages WHERE thread_id = $1", [id]);
      for (let i = 0; i < patch.messages.length; i++) {
        await this.db.query("INSERT INTO thread_messages (thread_id, ord, msg) VALUES ($1,$2,$3)", [id, i, JSON.stringify(patch.messages[i])]);
      }
    }
    return { applied: true, owned: true, updatedAt: patch.updatedAt };
  }

  /** Tombstone a thread of the principal's (hides it from list; reads 404;
   *  later PUTs lose). Returns false when `p` owns no such thread. */
  async tombstone(p: Principal, id: string, at: number): Promise<boolean> {
    return this.scoped(p, async () => {
      const rows = (
        await this.db.query(
          "UPDATE threads SET deleted_at = $4, updated_at = GREATEST(updated_at, $4) WHERE id = $1 AND org_id = $2 AND user_id = $3 RETURNING id",
          [id, p.orgId, p.userId, at],
        )
      ).rows;
      return rows.length > 0;
    });
  }

  /** Release the connection — only when this store opened it. */
  async close(): Promise<void> {
    if (!this.owned) return;
    if (this.db.close) await this.db.close();
    else if (this.db.end) await this.db.end();
  }
}

/** Whether the stator env is set for thread persistence — the same switch as
 *  the memory side (exec/stator.ts): pgvector backend + DSN. */
export function statorConfigured(env: NodeJS.ProcessEnv = process.env): boolean {
  return env.ROTOR_STATOR_BACKEND === "pgvector" && Boolean(env.ROTOR_STATOR_URL);
}

/** Warn LOUDLY when the stator is configured but introspection auth is not:
 *  without a principal every run records nothing, silently by design — this
 *  line makes the misconfiguration visible at pod boot. Returns whether it
 *  warned (the seam the test asserts). */
export function warnIfUnrecordable(env: NodeJS.ProcessEnv, authEnabled: boolean, logger: Logger = log): boolean {
  if (!statorConfigured(env) || authEnabled) return false;
  logger.warn(
    "stator configured but introspection auth is OFF — threads will NOT persist " +
      "(runs have no principal; set ROTOR_AUTH_INTROSPECT_URL + ROTOR_AUTH_SERVICE_TOKEN)",
  );
  return true;
}

/** Build the pod's thread store from the stator env ({@link statorConfigured});
 *  off → persistence off and the thread routes answer 503. Connect failures
 *  degrade (the pod still serves runs), never raise. */
export function threadStoreFromEnv(env: NodeJS.ProcessEnv = process.env): Promise<ThreadStore | null> {
  if (!statorConfigured(env)) return Promise.resolve(null);
  return ThreadStore.create({ url: env.ROTOR_STATOR_URL })
    .then((store) => {
      log.info("thread persistence enabled (stator)", {});
      return store;
    })
    .catch((err: unknown) => {
      log.error("thread store connect failed", { detail: (err as Error).message });
      return null;
    });
}

/** Parse untrusted PUT-body messages: keep only well-formed CodeMsg rows, but
 *  carry their extra fields verbatim (same tolerance as config.parseHistory). */
export function parseThreadMsgs(raw: unknown): ThreadMsg[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const msgs: ThreadMsg[] = [];
  for (const m of raw) {
    const role = (m as { role?: unknown })?.role;
    const text = (m as { text?: unknown })?.text;
    const at = (m as { at?: unknown })?.at;
    if ((role === "user" || role === "assistant") && typeof text === "string" && typeof at === "number") {
      msgs.push(m as ThreadMsg);
    }
  }
  return msgs;
}

/** Validate a mode string from a PUT body. */
export function parseMode(raw: unknown): SessionMode | undefined {
  return SESSION_MODES.includes(raw as SessionMode) ? (raw as SessionMode) : undefined;
}

// ── the run→thread recorder ─────────────────────────────────────────────────

/** How often the in-flight assistant text flushes mid-stream. Correctness over
 *  chattiness: a crash loses at most this window of streamed text. */
const FLUSH_MS = 1500;

/**
 * Records ONE run into its thread, under the run's introspected principal.
 * Subscribes to the session's frame stream and serializes all writes on an
 * internal promise chain (frame order = row order); a failing store never
 * takes the run down, and a run with NO principal (auth disabled, or the
 * control plane sent none) records nothing. The transcript grammar is the
 * desktop renderer's, verbatim (help/code.ts onGlyphhAgentTool/Done/Error):
 * text commits before each tool row, tool rows rewrite `⚙` → `✓/✕` in place,
 * errors append as `⚠ <error>`.
 */
export class ThreadRecorder {
  private readonly threadId: string;
  private store: ThreadStore | null = null;
  private q: Promise<unknown>;
  private text = "";
  private liveOrd: number | null = null;
  private lastFlush = 0;
  private lastTool: { ord: number; name: string } | null = null;
  private readonly logger;

  constructor(
    store: Promise<ThreadStore | null>,
    private readonly cfg: Pick<HarnessRunConfig, "runId" | "sessionId" | "threadId" | "prompt" | "mode">,
    private readonly principal: Principal | undefined,
  ) {
    // The CLIENT's thread id wins (clients mint `c<ts36>` chat ids while auth
    // binds the token to the provisioned `sess_*` sessionId); without one the
    // session id is the thread id, and a blank session keys by the run.
    this.threadId = cfg.threadId || cfg.sessionId || cfg.runId;
    this.logger = log.child({ run_id: cfg.runId, thread: this.threadId });
    this.q = store.then((s) => {
      this.store = s;
    });
  }

  /** Persist the user turn and start translating frames. Call after the
   *  session is admitted, before the engine starts emitting. */
  attach(session: HarnessSession): void {
    if (!this.principal) {
      this.logger.debug("no principal on the run — thread persistence off");
      return;
    }
    this.enqueue(async (s, p) => {
      const at = Date.now();
      await s.touch(p, this.threadId, at, { mode: this.cfg.mode });
      await this.appendOwned(s, p, { role: "user", text: this.cfg.prompt, at });
    });
    const unsubscribe = session.subscribe((f) => this.onFrame(f, unsubscribe));
  }

  private onFrame(f: WireFrame, unsubscribe: () => void): void {
    if (f.type === "delta") {
      this.text += f.delta;
      if (f.at - this.lastFlush >= FLUSH_MS) {
        this.lastFlush = f.at;
        this.enqueue((s, p) => this.flushLive(s, p, f.at));
      }
    } else if (f.type === "tool" && f.phase === "start") {
      const row = `⚙ ${f.name}${f.thought ? ` — ${f.thought}` : ""}`;
      const name = f.name;
      this.enqueue(async (s, p) => {
        await this.commitText(s, p, f.at);
        const ord = await this.appendOwned(s, p, { role: "assistant", kind: "tool", text: row, at: f.at });
        this.lastTool = ord == null ? null : { ord, name };
      });
    } else if (f.type === "tool") {
      // phase:done — the desktop's breadcrumb rewrite, byte for byte.
      const bad = f.denied || f.failed;
      const reason = f.denied ? " — denied" : f.failed ? ` — ${String(f.preview || "").split("\n")[0].slice(0, 90)}` : "";
      const diff = !bad ? (/\[\+\d+ -\d+\]/.exec(String(f.preview || ""))?.[0] ?? "") : "";
      const text = `${bad ? "✕" : "✓"} ${f.title || f.name}${reason}${diff ? ` ${diff}` : ""}`;
      const name = f.name;
      this.enqueue(async (s, p) => {
        const open = this.lastTool;
        this.lastTool = null;
        if (open && open.name === name) await s.amend(p, this.threadId, open.ord, { role: "assistant", kind: "tool", text, at: f.at });
        else await this.appendOwned(s, p, { role: "assistant", kind: "tool", text, at: f.at });
      });
    } else if (f.type === "done") {
      this.enqueue((s, p) => this.commitText(s, p, f.at));
      unsubscribe();
    } else if (f.type === "error") {
      const err = `⚠ ${f.error}`;
      this.text = this.text ? `${this.text}\n\n${err}` : err;
      this.enqueue((s, p) => this.commitText(s, p, f.at));
      unsubscribe();
    }
  }

  /** Append under the principal; an unowned thread (a foreign id collision)
   *  turns persistence off for the rest of the run — never write into it. */
  private async appendOwned(s: ThreadStore, p: Principal, msg: ThreadMsg): Promise<number | null> {
    const ord = await s.append(p, this.threadId, msg);
    if (ord == null) {
      this.logger.warn("thread not owned by the run's principal — persistence off");
      this.store = null;
    }
    return ord;
  }

  /** Upsert the in-progress assistant row with the text streamed so far. */
  private async flushLive(s: ThreadStore, p: Principal, at: number): Promise<void> {
    if (!this.text) return;
    const msg: ThreadMsg = { role: "assistant", text: this.text, at };
    if (this.liveOrd == null) this.liveOrd = await this.appendOwned(s, p, msg);
    else await s.amend(p, this.threadId, this.liveOrd, msg);
  }

  /** Commit the streamed text as a finished message and open a fresh block. */
  private async commitText(s: ThreadStore, p: Principal, at: number): Promise<void> {
    await this.flushLive(s, p, at);
    this.liveOrd = null;
    this.text = "";
  }

  private enqueue(fn: (s: ThreadStore, p: Principal) => Promise<unknown>): void {
    this.q = this.q
      .then(() => (this.store && this.principal ? fn(this.store, this.principal) : undefined))
      .catch((err: unknown) => {
        this.logger.warn("thread persist failed", { detail: (err as Error).message });
      });
  }
}

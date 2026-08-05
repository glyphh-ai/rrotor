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
 *     rotor stays TENANT-UNAWARE: tables land in whatever database/schema the
 *     provisioned `ROTOR_STATOR_URL` DSN is scoped to — tenancy is the control
 *     plane's concern, exactly like the memory tables.
 *   • {@link ThreadRecorder} — subscribes to a {@link HarnessSession}'s frame
 *     stream and translates frames into messages with the DESKTOP'S transcript
 *     grammar (app/src/renderer/help/code.ts CodeMsg): the user turn lands at
 *     run start (a crashed run still has it), streamed text upserts a live
 *     assistant row (throttled), tool start/done rows mirror the renderer's
 *     `⚙` / `✓ ✕` breadcrumbs, and the terminal frame commits the tail.
 *
 * Messages are stored AS-IS in jsonb (role/text/at + optional kind/attachments/
 * decline/connect) — the CodeMsg shape is the contract; no normalization.
 * Sync is last-write-wins on `updated_at` (epoch ms, the same clock as
 * CodeMsg.at): a client PUT applies only when its stamp is newer, and a
 * tombstone (`deleted_at`) is terminal.
 */

import { connectPg, asJson } from "../exec/pgvector-store.js";
import type { PgLike } from "../exec/pgvector-store.js";
import type { HarnessRunConfig, SessionMode } from "./config.js";
import type { WireFrame } from "./frames.js";
import type { HarnessSession } from "./session.js";
import { log } from "../obs/logger.js";

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

const SESSION_MODES: SessionMode[] = ["chat", "cowork", "code"];

/** How many of the LATEST messages a full read returns. */
export const THREAD_MSG_CAP = 500;

// Same migration mechanism as the pgvector stator: idempotent DDL at create.
// Epoch-ms BIGINT stamps match CodeMsg.at, so LWW compares are plain numbers.
const DDL = `
CREATE TABLE IF NOT EXISTS threads (
  id         TEXT PRIMARY KEY,
  title      TEXT NOT NULL DEFAULT '',
  mode       TEXT NOT NULL DEFAULT 'code',
  source     JSONB,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  deleted_at BIGINT
);
CREATE INDEX IF NOT EXISTS ix_threads_updated ON threads(updated_at DESC);
CREATE TABLE IF NOT EXISTS thread_messages (
  seq       BIGSERIAL PRIMARY KEY,
  thread_id TEXT NOT NULL,
  msg       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_thread_messages_thread ON thread_messages(thread_id, seq);
`;

export interface ThreadStoreOptions {
  /** Inject a ready client (tests pass a PGlite instance). */
  client?: PgLike;
  /** Postgres connection string (the provisioned stator DSN). */
  url?: string;
}

export class ThreadStore {
  private constructor(
    private readonly db: PgLike,
    /** True when this store opened the connection (and must close it). */
    private readonly owned: boolean,
  ) {}

  /** Connect (or adopt an injected client) and apply the schema. */
  static async create(opts: ThreadStoreOptions = {}): Promise<ThreadStore> {
    const db = opts.client ?? (await connectPg(opts.url));
    const store = new ThreadStore(db, !opts.client);
    if (db.exec) await db.exec(DDL);
    else await db.query(DDL); // node-postgres runs multi-statement simple queries
    return store;
  }

  /** Ensure the thread row exists and its stamp covers `at`. Never overwrites
   *  client-owned fields (title/mode/source) on an existing thread — the run
   *  path only inserts defaults; the client's PUT is authoritative for those. */
  async touch(id: string, at: number, fields: { mode?: SessionMode; source?: unknown } = {}): Promise<void> {
    await this.db.query(
      "INSERT INTO threads (id, title, mode, source, created_at, updated_at) VALUES ($1,'',$2,$3,$4,$4) " +
        "ON CONFLICT (id) DO UPDATE SET updated_at = GREATEST(threads.updated_at, excluded.updated_at)",
      [id, fields.mode ?? "code", fields.source != null ? JSON.stringify(fields.source) : null, at],
    );
  }

  /** Append one message; bumps the thread stamp. Returns the row's seq so a
   *  streaming writer can {@link amend} it in place. */
  async append(id: string, msg: ThreadMsg): Promise<number> {
    const rows = (
      await this.db.query("INSERT INTO thread_messages (thread_id, msg) VALUES ($1,$2) RETURNING seq", [id, JSON.stringify(msg)])
    ).rows;
    await this.stamp(id, msg.at);
    return Number(rows[0].seq);
  }

  /** Replace a message in place (the live streamed row / tool-done rewrite). */
  async amend(id: string, seq: number, msg: ThreadMsg): Promise<void> {
    await this.db.query("UPDATE thread_messages SET msg = $2 WHERE seq = $1", [seq, JSON.stringify(msg)]);
    await this.stamp(id, msg.at);
  }

  private async stamp(id: string, at: number): Promise<void> {
    await this.db.query("UPDATE threads SET updated_at = GREATEST(updated_at, $2) WHERE id = $1", [id, at]);
  }

  /** All live threads, newest first — metadata only, tombstones hidden. */
  async list(): Promise<ThreadMeta[]> {
    const rows = (
      await this.db.query(
        "SELECT t.id, t.title, t.mode, t.created_at, t.updated_at, " +
          "(SELECT COUNT(*) FROM thread_messages m WHERE m.thread_id = t.id) AS n " +
          "FROM threads t WHERE t.deleted_at IS NULL ORDER BY t.updated_at DESC",
      )
    ).rows;
    return rows.map((r) => ({
      id: String(r.id),
      title: String(r.title),
      mode: String(r.mode) as SessionMode,
      createdAt: Number(r.created_at),
      updatedAt: Number(r.updated_at),
      messageCount: Number(r.n),
    }));
  }

  /** One full thread (latest {@link THREAD_MSG_CAP} messages, in order), or
   *  undefined when absent or tombstoned. */
  async get(id: string, cap = THREAD_MSG_CAP): Promise<ThreadFull | undefined> {
    const rows = (
      await this.db.query("SELECT id, title, mode, source, created_at, updated_at, deleted_at FROM threads WHERE id = $1", [id])
    ).rows;
    if (!rows.length || rows[0].deleted_at != null) return undefined;
    const t = rows[0];
    const msgs = (
      await this.db.query(
        "SELECT msg FROM (SELECT seq, msg FROM thread_messages WHERE thread_id = $1 ORDER BY seq DESC LIMIT $2) sub ORDER BY seq ASC",
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
   *  than the stored one; a tombstone always wins. Creates the thread when it
   *  does not exist yet (client-generated ids like `c<ts36>` are the norm).
   *  Returns the winning stamp either way, so a losing client can pull. */
  async put(id: string, patch: ThreadPut): Promise<{ applied: boolean; updatedAt: number }> {
    const rows = (
      await this.db.query("SELECT title, mode, source, updated_at, deleted_at FROM threads WHERE id = $1", [id])
    ).rows;
    const cur = rows[0];
    if (cur && (cur.deleted_at != null || Number(cur.updated_at) >= patch.updatedAt)) {
      return { applied: false, updatedAt: Math.max(Number(cur.updated_at), Number(cur.deleted_at ?? 0)) };
    }
    const title = patch.title ?? (cur ? String(cur.title) : "");
    const mode = patch.mode ?? (cur ? (String(cur.mode) as SessionMode) : "code");
    const source = patch.source !== undefined ? patch.source : cur ? asJson(cur.source) : null;
    await this.db.query(
      "INSERT INTO threads (id, title, mode, source, created_at, updated_at) VALUES ($1,$2,$3,$4,$5,$5) " +
        "ON CONFLICT (id) DO UPDATE SET title = excluded.title, mode = excluded.mode, source = excluded.source, updated_at = excluded.updated_at",
      [id, title, mode, source != null ? JSON.stringify(source) : null, patch.updatedAt],
    );
    if (patch.messages) {
      await this.db.query("DELETE FROM thread_messages WHERE thread_id = $1", [id]);
      for (const m of patch.messages) {
        await this.db.query("INSERT INTO thread_messages (thread_id, msg) VALUES ($1,$2)", [id, JSON.stringify(m)]);
      }
    }
    return { applied: true, updatedAt: patch.updatedAt };
  }

  /** Tombstone a thread (hides it from list; reads 404; later PUTs lose).
   *  Returns false when no such thread exists. */
  async tombstone(id: string, at: number): Promise<boolean> {
    const rows = (
      await this.db.query("UPDATE threads SET deleted_at = $2, updated_at = GREATEST(updated_at, $2) WHERE id = $1 RETURNING id", [id, at])
    ).rows;
    return rows.length > 0;
  }

  /** Release the connection — only when this store opened it. */
  async close(): Promise<void> {
    if (!this.owned) return;
    if (this.db.close) await this.db.close();
    else if (this.db.end) await this.db.end();
  }
}

/** Build the pod's thread store from the stator env — same switch as the
 *  memory side (exec/stator.ts): pgvector backend + DSN, else persistence is
 *  off and the thread routes answer 503. Connect failures degrade (the pod
 *  still serves runs), never raise. */
export function threadStoreFromEnv(env: NodeJS.ProcessEnv = process.env): Promise<ThreadStore | null> {
  if (env.ROTOR_STATOR_BACKEND !== "pgvector" || !env.ROTOR_STATOR_URL) return Promise.resolve(null);
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
 * Records ONE run into its thread. Subscribes to the session's frame stream
 * and serializes all writes on an internal promise chain (frame order = row
 * order); a failing store never takes the run down. The transcript grammar is
 * the desktop renderer's, verbatim (help/code.ts onGlyphhAgentTool/Done/Error):
 * text commits before each tool row, tool rows rewrite `⚙` → `✓/✕` in place,
 * errors append as `⚠ <error>`.
 */
export class ThreadRecorder {
  private readonly threadId: string;
  private store: ThreadStore | null = null;
  private q: Promise<unknown>;
  private text = "";
  private liveSeq: number | null = null;
  private lastFlush = 0;
  private lastTool: { seq: number; name: string } | null = null;
  private readonly logger;

  constructor(
    store: Promise<ThreadStore | null>,
    private readonly cfg: Pick<HarnessRunConfig, "runId" | "sessionId" | "prompt" | "mode">,
  ) {
    // Client-generated session ids (`c<ts36>`) ARE the thread ids; a blank
    // session still gets a thread keyed by the run.
    this.threadId = cfg.sessionId || cfg.runId;
    this.logger = log.child({ run_id: cfg.runId, thread: this.threadId });
    this.q = store.then((s) => {
      this.store = s;
    });
  }

  /** Persist the user turn and start translating frames. Call after the
   *  session is admitted, before the engine starts emitting. */
  attach(session: HarnessSession): void {
    this.enqueue(async (s) => {
      const at = Date.now();
      await s.touch(this.threadId, at, { mode: this.cfg.mode });
      await s.append(this.threadId, { role: "user", text: this.cfg.prompt, at });
    });
    const unsubscribe = session.subscribe((f) => this.onFrame(f, unsubscribe));
  }

  private onFrame(f: WireFrame, unsubscribe: () => void): void {
    if (f.type === "delta") {
      this.text += f.delta;
      if (f.at - this.lastFlush >= FLUSH_MS) {
        this.lastFlush = f.at;
        this.enqueue((s) => this.flushLive(s, f.at));
      }
    } else if (f.type === "tool" && f.phase === "start") {
      const row = `⚙ ${f.name}${f.thought ? ` — ${f.thought}` : ""}`;
      const name = f.name;
      this.enqueue(async (s) => {
        await this.commitText(s, f.at);
        this.lastTool = { seq: await s.append(this.threadId, { role: "assistant", kind: "tool", text: row, at: f.at }), name };
      });
    } else if (f.type === "tool") {
      // phase:done — the desktop's breadcrumb rewrite, byte for byte.
      const bad = f.denied || f.failed;
      const reason = f.denied ? " — denied" : f.failed ? ` — ${String(f.preview || "").split("\n")[0].slice(0, 90)}` : "";
      const diff = !bad ? (/\[\+\d+ -\d+\]/.exec(String(f.preview || ""))?.[0] ?? "") : "";
      const text = `${bad ? "✕" : "✓"} ${f.title || f.name}${reason}${diff ? ` ${diff}` : ""}`;
      const name = f.name;
      this.enqueue(async (s) => {
        const open = this.lastTool;
        this.lastTool = null;
        if (open && open.name === name) await s.amend(this.threadId, open.seq, { role: "assistant", kind: "tool", text, at: f.at });
        else await s.append(this.threadId, { role: "assistant", kind: "tool", text, at: f.at });
      });
    } else if (f.type === "done") {
      this.enqueue((s) => this.commitText(s, f.at));
      unsubscribe();
    } else if (f.type === "error") {
      const err = `⚠ ${f.error}`;
      this.text = this.text ? `${this.text}\n\n${err}` : err;
      this.enqueue((s) => this.commitText(s, f.at));
      unsubscribe();
    }
  }

  /** Upsert the in-progress assistant row with the text streamed so far. */
  private async flushLive(s: ThreadStore, at: number): Promise<void> {
    if (!this.text) return;
    const msg: ThreadMsg = { role: "assistant", text: this.text, at };
    if (this.liveSeq == null) this.liveSeq = await s.append(this.threadId, msg);
    else await s.amend(this.threadId, this.liveSeq, msg);
  }

  /** Commit the streamed text as a finished message and open a fresh block. */
  private async commitText(s: ThreadStore, at: number): Promise<void> {
    await this.flushLive(s, at);
    this.liveSeq = null;
    this.text = "";
  }

  private enqueue(fn: (s: ThreadStore) => Promise<void>): void {
    this.q = this.q
      .then(() => (this.store ? fn(this.store) : undefined))
      .catch((err: unknown) => {
        this.logger.warn("thread persist failed", { detail: (err as Error).message });
      });
  }
}

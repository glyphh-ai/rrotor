/**
 * Stator backend selection (BUILD_PLAN.md Phase 2). One place decides whether a
 * runtime runs against the zero-dependency in-process store (default; tests and
 * bare-box) or the durable SQLite store, driven by `ROTOR_STATOR_BACKEND` /
 * `ROTOR_STATOR_URL` (declared in deploy/k8s/configmap.yaml).
 *
 * The premium tier (Postgres + pgvector, shared cross-pod) registers behind the
 * same {@link Stator} interface — the factory is the only edit site.
 */

import { InProcessStore, type Stator } from "./store.js";
import { SqliteStore } from "./sqlite-store.js";
import { PgVectorStore } from "./pgvector-store.js";
import { embedderFromEnv, type Embedder } from "./embedder.js";

export type StatorBackend = "memory" | "sqlite" | "pgvector";

export interface StatorOptions {
  backend?: StatorBackend;
  /** For `sqlite`, a file path (or `:memory:`); for `pgvector`, a connection
   *  string. Ignored by `memory`. */
  url?: string;
  /** For `pgvector`, the turn embedder (its `dim` sizes the vector column, §7.7).
   *  Defaults to the deterministic hash embedder (256) when omitted. */
  embedder?: Embedder;
}

/** Construct a **synchronous** stator from explicit options. `pgvector` is async
 *  (it hydrates from Postgres), so it is NOT available here — use
 *  {@link initStator}. Throwing keeps the sync path honest rather than silently
 *  degrading a durable backend to in-process. */
export function createStator(opts: StatorOptions = {}): Stator {
  if (opts.backend === "pgvector") {
    throw new Error("pgvector is async; use initStator() (it hydrates from Postgres before the run)");
  }
  if (opts.backend === "sqlite") return new SqliteStore(opts.url ?? ":memory:");
  return new InProcessStore();
}

/** Construct a stator from explicit options, awaiting async backends. This is the
 *  general factory: sync backends resolve immediately, `pgvector` connects +
 *  hydrates its in-memory mirror before returning (so the sync run loop that
 *  follows is deterministic). */
export async function initStator(opts: StatorOptions = {}): Promise<Stator> {
  if (opts.backend === "pgvector") {
    return PgVectorStore.create({ url: opts.url, embedder: opts.embedder });
  }
  return createStator(opts);
}

function backendFromEnv(env: NodeJS.ProcessEnv): StatorBackend {
  if (env.ROTOR_STATOR_BACKEND === "pgvector") return "pgvector";
  if (env.ROTOR_STATOR_BACKEND === "sqlite") return "sqlite";
  return "memory";
}

/** Construct a stator from the environment, awaiting async backends. Defaults to
 *  the in-process backend so nothing durable is created unless asked for. */
export async function statorFromEnvAsync(env: NodeJS.ProcessEnv = process.env): Promise<Stator> {
  const backend = backendFromEnv(env);
  const url = env.ROTOR_STATOR_URL?.replace(/^~(?=$|\/)/, process.env.HOME ?? "~");
  // Only the pgvector path stores the turn vector, so only it needs the embedder;
  // built here (via `ROTOR_EMBED_*`) so the DB column width follows the model.
  const embedder = backend === "pgvector" ? embedderFromEnv(env) : undefined;
  return initStator({ backend, url, embedder });
}

/** Synchronous env construction (memory/sqlite only). Retained for callers on the
 *  sync path; `pgvector` requires {@link statorFromEnvAsync}. */
export function statorFromEnv(env: NodeJS.ProcessEnv = process.env): Stator {
  const backend = backendFromEnv(env);
  return createStator({ backend, url: env.ROTOR_STATOR_URL });
}

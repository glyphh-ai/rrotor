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

export type StatorBackend = "memory" | "sqlite";

export interface StatorOptions {
  backend?: StatorBackend;
  /** For `sqlite`, a file path (or `:memory:`). Ignored by `memory`. */
  url?: string;
}

/** Construct a stator from explicit options. */
export function createStator(opts: StatorOptions = {}): Stator {
  if (opts.backend === "sqlite") return new SqliteStore(opts.url ?? ":memory:");
  return new InProcessStore();
}

/** Construct a stator from the environment. Defaults to the in-process backend
 *  so nothing durable is created unless explicitly asked for. */
export function statorFromEnv(env: NodeJS.ProcessEnv = process.env): Stator {
  const backend: StatorBackend = env.ROTOR_STATOR_BACKEND === "sqlite" ? "sqlite" : "memory";
  return createStator({ backend, url: env.ROTOR_STATOR_URL });
}

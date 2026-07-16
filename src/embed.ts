/**
 * embed.ts — the IN-PROCESS face of the runtime, the twin of `serve`.
 *
 * `openrotor serve` streams a run's `open → step* → terminal → done` over SSE;
 * `runInProcess` streams the SAME events to a callback in the caller's own process.
 * Both sit on the transport-agnostic core (`executeToEvents`), so an embedded client
 * (the CLI / desktop) and a remote pod run identical code — they differ only in where
 * the events go and where the stator lives.
 *
 * The one local memory layer is a shared SQLite stator file: open it once with
 * {@link openSqliteStator} and hand it to every run, so memory carries across every
 * client on the machine (WAL + busy_timeout make concurrent processes safe).
 */

import { cwd } from "node:process";
import { executeToEvents, type StreamContext } from "./transport/run.js";
import type { WireEvent } from "./transport/events.js";
import { parseRotor } from "./parser/index.js";
import { drainFromEnv } from "./plugins/index.js";
import { initStator } from "./exec/stator.js";
import type { Stator } from "./exec/store.js";
import type { RotorDocument } from "./types.js";

export interface EmbedOptions {
  /** The durable memory. Defaults to an ephemeral in-process store — pass a shared
   *  SQLite stator (see {@link openSqliteStator}) for memory that persists + is shared. */
  store?: Stator;
  /** Tool sandbox root. Defaults to the current working directory. */
  workspace?: string;
  /** Optional memory-scoping session id. */
  session?: string;
}

/**
 * Run a rotor IN-PROCESS, calling `emit` for each wire event as it streams. Resolves
 * when the run is terminal. `rotor` is a document object or its YAML/JSON source.
 */
export async function runInProcess(
  rotor: string | RotorDocument,
  inputs: Record<string, unknown>,
  emit: (ev: WireEvent) => void,
  opts: EmbedOptions = {},
): Promise<void> {
  const doc = typeof rotor === "string" ? parseRotor(rotor) : rotor;
  const store = opts.store ?? (await initStator({ backend: "memory" }));
  const ctx: StreamContext = {
    store,
    drain: drainFromEnv(),
    workspace: opts.workspace ?? cwd(),
    ...(opts.session ? { session: opts.session } : {}),
  };
  await executeToEvents(doc, inputs, ctx, emit);
}

/** Open (or create) the shared SQLite stator at `path` — the one local memory layer
 *  every client on the machine points at. */
export function openSqliteStator(path: string): Promise<Stator> {
  return initStator({ backend: "sqlite", url: path });
}

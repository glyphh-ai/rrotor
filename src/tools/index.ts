/**
 * The glyphh tool standard library — batteries included. Assembles the built-in
 * packs (fs, exec, git, doc, cowork, chat) into one registry and installs them into
 * a connections plugin, **capability-gated by the run's permission mode** so a chat
 * session never gets a `mutating` tool and a code session gets the whole workbench.
 *
 * This is also the SDK surface: `defineTool` / `pack` let a developer add their own
 * tool behind the same contract (effect + grants + schema), so "build your own tool"
 * and "a built-in tool" are the identical mechanism — and a better third-party `read`
 * drops in behind the same name (docs/tools.md).
 */

import type { ConnectionsPlugin, MemoryPlugin } from "../plugins/interfaces.js";
import {
  ToolRegistry,
  MODES,
  type Capability,
  type InstallResult,
  type ModeName,
  type ToolPack,
  type ToolSpec,
} from "./spec.js";
import { fsPack } from "./fs.js";
import { execPack } from "./exec.js";
import { gitPack } from "./git.js";
import { docPack } from "./doc.js";
import { coworkPack, type KvLike } from "./cowork.js";
import { chatPack } from "./chat.js";
import { webPack } from "./web.js";
import { dataPack } from "./data.js";
import { textPack } from "./text.js";
import { cryptoPack } from "./crypto.js";
import { calcPack } from "./calc.js";
import { filesPack } from "./files.js";
import { sysPack } from "./sys.js";
import { gitxPack } from "./gitx.js";
import { dbPack } from "./db.js";
import { previewPack } from "./preview.js";

export * from "./spec.js";
export { fsPack } from "./fs.js";
export { execPack } from "./exec.js";
export { gitPack } from "./git.js";
export { docPack } from "./doc.js";
export { coworkPack, type KvLike } from "./cowork.js";
export { chatPack } from "./chat.js";
export { webPack } from "./web.js";
export { dataPack } from "./data.js";
export { textPack } from "./text.js";
export { cryptoPack } from "./crypto.js";
export { calcPack } from "./calc.js";
export { filesPack } from "./files.js";
export { sysPack } from "./sys.js";
export { gitxPack } from "./gitx.js";
export { dbPack } from "./db.js";
export { previewPack } from "./preview.js";

export interface StdlibOptions {
  /** The workspace sandbox root for fs/exec/git/doc tools. */
  root: string;
  /** The memory plugin `recall` reads over. */
  memory: MemoryPlugin;
  /** Durable kv for co-work todos/artifacts (adapt from a stator: {@link kvFromStore}). */
  kv: KvLike;
}

/** Build the full standard-library registry (all packs, nothing installed yet). */
export function buildStdlib(opts: StdlibOptions): ToolRegistry {
  return new ToolRegistry()
    .add(fsPack({ root: opts.root }))
    .add(execPack({ root: opts.root }))
    .add(gitPack({ root: opts.root }))
    .add(docPack({ root: opts.root }))
    .add(coworkPack(opts.kv))
    .add(chatPack({ memory: opts.memory }))
    .add(webPack({ root: opts.root }))
    .add(dataPack())
    .add(textPack())
    .add(cryptoPack())
    .add(calcPack())
    .add(filesPack({ root: opts.root }))
    .add(sysPack())
    .add(gitxPack({ root: opts.root }))
    .add(dbPack({ root: opts.root }))
    .add(previewPack({ root: opts.root }));
}

/**
 * Install the stdlib into a connections plugin, gated by the permission mode (or an
 * explicit capability set). Returns which tools were installed vs skipped (so the
 * client can show "this mode can't do X"). Omitting both installs everything
 * (bare-box open). `packs` adds the HOST's own tools (a desktop's window manager,
 * a CLI's connectors) behind the identical contract and gating as the built-ins.
 */
export function installStdlib(
  connections: ConnectionsPlugin,
  opts: StdlibOptions & { mode?: ModeName; granted?: ReadonlySet<Capability>; packs?: ToolPack[] },
): InstallResult {
  const registry = buildStdlib(opts);
  for (const pack of opts.packs ?? []) registry.add(pack);
  const granted = opts.granted ?? (opts.mode ? MODES[opts.mode] : undefined);
  return registry.install(connections, { granted });
}

/** Adapt a stator (kvGet/kvSet) to the {@link KvLike} the cowork pack needs. */
export function kvFromStore(store: { kvGet(k: string): Promise<unknown>; kvSet(k: string, v: unknown): Promise<void> }): KvLike {
  return { get: (k) => store.kvGet(k), set: (k, v) => store.kvSet(k, v) };
}

/** The permission mode a rotor declares via `metadata.labels.mode`. The rotor drives
 *  it; default `code` (the full local workbench). */
export function toolModeFromLabels(labels?: Record<string, string>): ModeName {
  const m = labels?.mode;
  return m === "chat" || m === "cowork" || m === "code" ? m : "code";
}

// ── SDK surface: define your own tools behind the same contract ─────────────────

/** Typed constructor for a user-defined tool (identity — the value IS the spec). */
export function defineTool(spec: ToolSpec): ToolSpec {
  return spec;
}

/** Bundle user tools into a pack. */
export function definePack(name: string, version: string, tools: ToolSpec[]): ToolPack {
  return { name, version, tools };
}

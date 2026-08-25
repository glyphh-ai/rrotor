/**
 * source-sync.ts — the pod's SOURCE-OF-RECORD tools: pull_source, push_source,
 * rebase_source.
 *
 * Every glyphh app's project source lives on R2 (control-plane snapshot chain,
 * CAS on publish — see server/src/domains/apps/source-snapshots.ts). These
 * tools are how a WORKSPACE synchronizes with it:
 *
 *   pull_source   — hydrate the working folder from the app's head snapshot
 *                   and stamp the base (.glyphh/source-base).
 *   push_source   — pack the working folder (node_modules/dist/.git excluded)
 *                   and CAS-publish it against the stamped base. A moved head
 *                   comes back as the human conflict message, never a clobber.
 *   rebase_source — pull the moved head UNDER local changes: files only
 *                   upstream changed are taken; files changed on both sides
 *                   keep the local copy with the upstream staged beside it at
 *                   .glyphh/upstream/<path> for the agent to merge by reason;
 *                   the base restamps to the new head. Returns the summary the
 *                   agent reports to the user — WHAT CHANGED FIRST, by name.
 *
 * Transport is the control plane's plain HTTP source routes, authorized with
 * the run's own token (the same credential the app tools ride).
 */

import AdmZip from "adm-zip";
import { createHash } from "node:crypto";
import { promises as fsp } from "node:fs";
import { join, resolve, sep, dirname } from "node:path";

/** Folders never packed or compared — build products and machinery. */
const EXCLUDE_DIRS = new Set(["node_modules", "dist", ".git", ".glyphh", "build", ".next", "coverage"]);
const BASE_FILE = ".glyphh/source-base";
const UPSTREAM_DIR = ".glyphh/upstream";
const MAX_FILES = 4000;

export interface SourceSyncCfg {
  workdir: string;
  controlUrl: string;   // control-plane origin (no trailing slash)
  token: string;
  fetchFn?: typeof fetch;
}

const api = (cfg: SourceSyncCfg, path: string) => `${cfg.controlUrl.replace(/\/$/, "")}${path}`;
const auth = (cfg: SourceSyncCfg) => ({ authorization: `Bearer ${cfg.token}` });

async function walk(root: string, rel = "", out: string[] = []): Promise<string[]> {
  const entries = await fsp.readdir(join(root, rel), { withFileTypes: true });
  for (const e of entries) {
    if (e.isDirectory()) {
      if (EXCLUDE_DIRS.has(e.name)) continue;
      await walk(root, rel ? `${rel}/${e.name}` : e.name, out);
    } else if (e.isFile()) {
      out.push(rel ? `${rel}/${e.name}` : e.name);
      if (out.length > MAX_FILES) throw new Error(`workspace has over ${MAX_FILES} files — is a build folder leaking past the excludes?`);
    }
  }
  return out;
}

export async function packWorkdir(workdir: string): Promise<Buffer> {
  const zip = new AdmZip();
  const files = await walk(workdir);
  files.sort(); // deterministic archive → deterministic hash for identical trees
  for (const rel of files) {
    zip.addFile(rel, await fsp.readFile(join(workdir, rel)));
  }
  return zip.toBuffer();
}

async function readBase(workdir: string): Promise<string | null> {
  try { return (await fsp.readFile(join(workdir, BASE_FILE), "utf8")).trim() || null; }
  catch { return null; }
}

async function writeBase(workdir: string, hash: string): Promise<void> {
  await fsp.mkdir(join(workdir, ".glyphh"), { recursive: true });
  await fsp.writeFile(join(workdir, BASE_FILE), hash, "utf8");
}

async function head(cfg: SourceSyncCfg, slug: string): Promise<{ mode: string; head: { hash: string; createdByEmail: string | null; createdAt: string } | null }> {
  const f = cfg.fetchFn ?? fetch;
  const res = await f(api(cfg, `/api/apps/${encodeURIComponent(slug)}/source/head`), { headers: auth(cfg) });
  if (!res.ok) throw new Error(`source head lookup failed (${res.status})`);
  return (await res.json() as { data: { mode: string; head: { hash: string; createdByEmail: string | null; createdAt: string } | null } }).data;
}

async function archive(cfg: SourceSyncCfg, slug: string, hash: string): Promise<Buffer> {
  const f = cfg.fetchFn ?? fetch;
  const res = await f(api(cfg, `/api/apps/${encodeURIComponent(slug)}/source/archive/${encodeURIComponent(hash)}`), { headers: auth(cfg) });
  if (!res.ok) throw new Error(`snapshot download failed (${res.status})`);
  return Buffer.from(await res.arrayBuffer());
}

/** Safe extraction root guard — a hostile archive path must never escape. */
function safeJoin(root: string, rel: string): string {
  const abs = resolve(root, rel);
  if (abs !== resolve(root) && !abs.startsWith(resolve(root) + sep)) throw new Error(`unsafe path in archive: ${rel}`);
  return abs;
}

async function extractInto(zipBytes: Buffer, root: string): Promise<string[]> {
  const zip = new AdmZip(zipBytes);
  const written: string[] = [];
  for (const entry of zip.getEntries()) {
    if (entry.isDirectory) continue;
    const abs = safeJoin(root, entry.entryName);
    await fsp.mkdir(dirname(abs), { recursive: true });
    await fsp.writeFile(abs, entry.getData());
    written.push(entry.entryName);
  }
  return written;
}

const fileHash = (b: Buffer) => createHash("sha256").update(b).digest("hex");

async function zipIndex(zipBytes: Buffer): Promise<Map<string, string>> {
  const zip = new AdmZip(zipBytes);
  const map = new Map<string, string>();
  for (const e of zip.getEntries()) if (!e.isDirectory) map.set(e.entryName, fileHash(e.getData()));
  return map;
}

export async function pullSource(cfg: SourceSyncCfg, slug: string): Promise<string> {
  const h = await head(cfg, slug);
  if (!h.head) return `'${slug}' has no source snapshot yet — build it here and push_source will create the first one.`;
  const bytes = await archive(cfg, slug, h.head.hash);
  const files = await extractInto(bytes, cfg.workdir);
  await writeBase(cfg.workdir, h.head.hash);
  return `pulled '${slug}' source head ${h.head.hash.slice(0, 12)} (${files.length} files) into the working folder. Run npm install before building.`;
}

export async function pushSource(cfg: SourceSyncCfg, slug: string, force = false): Promise<string> {
  const bytes = await packWorkdir(cfg.workdir);
  const base = await readBase(cfg.workdir);
  const f = cfg.fetchFn ?? fetch;
  const q = new URLSearchParams();
  if (base) q.set("baseHash", base);
  if (force) q.set("force", "1");
  const res = await f(api(cfg, `/api/apps/${encodeURIComponent(slug)}/source?${q.toString()}`), {
    method: "PUT",
    headers: { ...auth(cfg), "content-type": "application/zip" },
    body: new Uint8Array(bytes),
  });
  if (res.status === 409) {
    const body = await res.json().catch(() => null) as { detail?: string } | null;
    return `SOURCE CONFLICT: ${body?.detail ?? "the head moved since your base."}`;
  }
  if (!res.ok) throw new Error(`source push failed (${res.status})`);
  const out = (await res.json() as { data: { hash: string; noop?: boolean } }).data;
  await writeBase(cfg.workdir, out.hash);
  return out.noop
    ? `source unchanged — head is already ${out.hash.slice(0, 12)}.`
    : `pushed '${slug}' source — new head ${out.hash.slice(0, 12)}.`;
}

export async function rebaseSource(cfg: SourceSyncCfg, slug: string): Promise<string> {
  const h = await head(cfg, slug);
  if (!h.head) return `'${slug}' has no snapshot head — nothing to rebase onto.`;
  const base = await readBase(cfg.workdir);
  if (base === h.head.hash) return "already based on the current head — nothing to rebase.";

  const headBytes = await archive(cfg, slug, h.head.hash);
  const headIdx = await zipIndex(headBytes);
  const baseIdx = base ? await zipIndex(await archive(cfg, slug, base)).catch(() => new Map<string, string>()) : new Map<string, string>();

  const localFiles = await walk(cfg.workdir);
  const localIdx = new Map<string, string>();
  for (const rel of localFiles) localIdx.set(rel, fileHash(await fsp.readFile(join(cfg.workdir, rel))));

  const taken: string[] = [];
  const conflicts: string[] = [];
  const zip = new AdmZip(headBytes);
  for (const [rel, headHashV] of headIdx) {
    const baseV = baseIdx.get(rel);
    const localV = localIdx.get(rel);
    const upstreamChanged = headHashV !== baseV;
    const localChanged = localV !== undefined ? localV !== baseV : false;
    if (!upstreamChanged) continue;
    const data = zip.getEntry(rel)!.getData();
    if (!localChanged || localV === undefined) {
      // Upstream-only change (or a file we never had): take it.
      const abs = safeJoin(cfg.workdir, rel);
      await fsp.mkdir(dirname(abs), { recursive: true });
      await fsp.writeFile(abs, data);
      taken.push(rel);
    } else if (localV !== headHashV) {
      // Both sides changed: keep local, stage upstream beside it for the merge.
      const staged = safeJoin(cfg.workdir, `${UPSTREAM_DIR}/${rel}`);
      await fsp.mkdir(dirname(staged), { recursive: true });
      await fsp.writeFile(staged, data);
      conflicts.push(rel);
    }
  }
  await writeBase(cfg.workdir, h.head.hash);

  const who = h.head.createdByEmail || "a teammate";
  const lines = [
    `Rebased onto head ${h.head.hash.slice(0, 12)} (published by ${who}).`,
    taken.length ? `Took upstream changes: ${taken.slice(0, 20).join(", ")}${taken.length > 20 ? ` (+${taken.length - 20} more)` : ""}.` : "No upstream-only changes.",
    conflicts.length
      ? `CONFLICTS (${conflicts.length}) — your copy kept, ${who}'s staged at ${UPSTREAM_DIR}/<path>: ${conflicts.join(", ")}. Merge each by hand (read both, write the merged file, delete the staged copy), then push_source.`
      : "No conflicts — push_source when ready.",
    `TELL THE USER what changed first: name the files ${who} shipped and summarize their intent before you republish.`,
  ];
  return lines.join("\n");
}

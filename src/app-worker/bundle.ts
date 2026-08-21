/**
 * bundle.ts — fetch + materialize a workpanel app's `.glyphh` bundle on the pod.
 *
 * The consumption rail of docs/app-runtime-spec.md ("Distribution and
 * execution home"): bundle bytes move by presigned R2 URL — the pod asks the
 * control plane for `GET /api/apps/:slug/bundle/download-url` (Bearer runtime
 * token), streams the presigned object to disk, and verifies the sha256 the
 * control plane reported. In local/dev the store cannot presign and the route
 * returns `url: null`; the pod then falls back to the buffered
 * `GET /api/apps/:slug/source` (same object, same bytes).
 *
 * Materialized bundles are cached BY CONTENT HASH: `<cacheDir>/<sha256>/…` —
 * so a re-fetch of an unchanged release is a no-op, and two apps that pin the
 * same bytes share one extraction. The manifest validation mirrors the
 * desktop's (app/src/main/glyphh-app-host.ts `readGlyphhAppManifest`): the
 * bundle IS the app, and both hosts must refuse the same malformed manifests.
 *
 * Bundles are code, never data — the 250MB publish cap is re-enforced here at
 * download so a hostile control plane response cannot fill the pod's disk.
 */

import { createHash } from "node:crypto";
import { createWriteStream, existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { once } from "node:events";
import { tmpdir } from "node:os";
import { dirname, join, resolve, sep } from "node:path";
import { Readable } from "node:stream";
import type { ReadableStream as WebReadableStream } from "node:stream/web";

import { readZipEntries } from "./zip.js";

/** Publish-side hard cap (docs/app-runtime-spec.md) — re-enforced at download. */
export const MAX_BUNDLE_BYTES = 250 * 1024 * 1024;

/** An MCP tool the app exposes — backed by exactly one of a host `capability`
 *  (1:1 passthrough) or a worker `handler` (app logic runs headless). */
export interface AppBundleTool {
  name: string;
  description?: string;
  capability?: string;
  handler?: string;
  inputSchema?: Record<string, unknown>;
}

/** manifest.json of a `.glyphh` bundle — the desktop's GlyphhAppManifest shape. */
export interface AppBundleManifest {
  manifestVersion: number;
  kind: "glyphh-app";
  name: string;
  slug: string;
  /** Panel entry html, relative to the bundle root. */
  entry: string;
  /** Headless script — registers handlers via glyphh.handle(). Relative path. */
  worker?: string;
  capabilities: string[];
  /** Connector app slugs the app integrates; connector.* capabilities scope to it. */
  connectors?: string[];
  tools?: AppBundleTool[];
  window?: { width?: number; height?: number };
  release?: { version: string; notes?: string | null; channel?: "dev" | "live"; releasedAt?: string | null };
  description?: string;
}

/** A slug's bundle, fetched, verified, and extracted — ready to execute. */
export interface ResolvedApp {
  /** Directory the bundle was extracted to (content-hash keyed). */
  dir: string;
  manifest: AppBundleManifest;
  sha256: string;
}

/**
 * Parse + validate a glyphh-app manifest. Mirrors the desktop's
 * `readGlyphhAppManifest` rule for rule: same inputs must pass or fail on both
 * hosts, or an app installs on one and not the other. Returns null on refusal.
 */
export function readAppBundleManifest(json: string): AppBundleManifest | null {
  let m: AppBundleManifest;
  try {
    m = JSON.parse(json) as AppBundleManifest;
  } catch {
    return null;
  }
  if (m?.kind !== "glyphh-app") return null;
  if (!m.name || !m.slug || !m.entry) return null;
  m.slug = String(m.slug).toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  // Leading-underscore slugs are reserved for host-owned dirs (desktop: _libs).
  if (m.slug.startsWith("_")) return null;
  m.entry = String(m.entry);
  m.capabilities = Array.isArray(m.capabilities) ? m.capabilities.map(String) : [];
  m.connectors = Array.isArray(m.connectors)
    ? [...new Set(m.connectors.map((c) => String(c).trim().toLowerCase()).filter((c) => /^[a-z0-9_-]+$/.test(c)))]
    : [];
  m.worker = typeof m.worker === "string" && m.worker.trim() ? m.worker.trim() : undefined;
  m.tools = Array.isArray(m.tools)
    ? m.tools.filter(
        (t) =>
          t &&
          typeof t.name === "string" &&
          // exactly one backing: a host capability OR a worker handler
          (typeof t.capability === "string") !== (typeof t.handler === "string")
      )
    : [];
  // A handler-backed tool is meaningless without a worker to run it in.
  if ((m.tools ?? []).some((t) => t.handler) && !m.worker) return null;
  return m;
}

/** What the control plane's download-url route reports for a release. */
export interface BundleDownload {
  /** Presigned GET, or null when the store cannot presign (local/dev). */
  url: string | null;
  releaseId: string;
  version: string;
  sha256: string | null;
  sizeBytes: number | null;
}

export interface FetchBundleOptions {
  /** Directory for the downloaded archive (default: a fresh os tmpdir). */
  tmpDir?: string;
  /** Injection seam for tests; defaults to global fetch. */
  fetchImpl?: typeof fetch;
  /** Download cap override for tests; defaults to MAX_BUNDLE_BYTES. */
  maxBytes?: number;
}

export interface FetchedBundle {
  zipPath: string;
  sha256: string;
  sizeBytes: number;
  version: string;
  releaseId: string;
}

function trimBase(url: string): string {
  return url.replace(/\/+$/, "");
}

async function readError(res: Response): Promise<string> {
  const text = await res.text().catch(() => "");
  return text.slice(0, 300);
}

/** Ask the control plane where a slug's bundle lives (and what its digest is). */
export async function mintBundleDownload(
  controlPlaneUrl: string,
  token: string,
  slug: string,
  fetchImpl: typeof fetch = fetch
): Promise<BundleDownload> {
  const base = trimBase(controlPlaneUrl);
  const res = await fetchImpl(`${base}/api/apps/${encodeURIComponent(slug)}/bundle/download-url`, {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    throw new Error(`bundle download-url for "${slug}" failed: HTTP ${res.status} ${await readError(res)}`);
  }
  const body = (await res.json()) as { data?: Partial<BundleDownload> };
  const d = body?.data;
  if (!d || typeof d.releaseId !== "string" || typeof d.version !== "string") {
    throw new Error(`bundle download-url for "${slug}" returned an unexpected shape`);
  }
  return {
    url: typeof d.url === "string" && d.url ? d.url : null,
    releaseId: d.releaseId,
    version: d.version,
    sha256: typeof d.sha256 === "string" && d.sha256 ? d.sha256.toLowerCase() : null,
    sizeBytes: typeof d.sizeBytes === "number" ? d.sizeBytes : null,
  };
}

/** Stream an HTTP response body to `dest`, hashing and size-capping as it goes. */
async function streamToFile(res: Response, dest: string, maxBytes: number): Promise<{ sha256: string; sizeBytes: number }> {
  if (!res.body) throw new Error("bundle download returned an empty body");
  const hash = createHash("sha256");
  const out = createWriteStream(dest);
  let size = 0;
  try {
    for await (const chunk of Readable.fromWeb(res.body as WebReadableStream<Uint8Array>)) {
      const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      size += buf.length;
      if (size > maxBytes) throw new Error(`bundle exceeds the ${Math.floor(maxBytes / (1024 * 1024))}MB cap`);
      hash.update(buf);
      if (!out.write(buf)) await once(out, "drain");
    }
    out.end();
    await once(out, "close");
  } catch (err) {
    out.destroy();
    await rm(dest, { force: true });
    throw err;
  }
  return { sha256: hash.digest("hex"), sizeBytes: size };
}

/**
 * Download an already-minted bundle to a local temp file and verify its digest.
 *
 * Presigned lane: GET the minted URL directly (no auth — the signature IS the
 * auth). Fallback lane (`url: null`, local/dev): buffered
 * GET /api/apps/:slug/source with the same Bearer. Either way the bytes are
 * hashed as they stream and checked against the control plane's reported
 * sha256 — a mismatch deletes the file and throws.
 */
async function downloadMinted(
  controlPlaneUrl: string,
  token: string,
  slug: string,
  minted: BundleDownload,
  opts: FetchBundleOptions = {}
): Promise<FetchedBundle> {
  const f = opts.fetchImpl ?? fetch;
  const maxBytes = opts.maxBytes ?? MAX_BUNDLE_BYTES;
  if (minted.sizeBytes !== null && minted.sizeBytes > maxBytes) {
    throw new Error(`bundle for "${slug}" is ${minted.sizeBytes} bytes — over the ${maxBytes}-byte cap`);
  }

  const tmpDir = opts.tmpDir ?? (await mkdtemp(join(tmpdir(), "glyphh-bundle-")));
  await mkdir(tmpDir, { recursive: true });
  const zipPath = join(tmpDir, `${slug}-${minted.releaseId}.glyphh`);

  const res = minted.url
    ? await f(minted.url)
    : await f(`${trimBase(controlPlaneUrl)}/api/apps/${encodeURIComponent(slug)}/source`, {
        headers: { authorization: `Bearer ${token}` },
      });
  if (!res.ok) {
    throw new Error(`bundle download for "${slug}" failed: HTTP ${res.status} ${await readError(res)}`);
  }
  const { sha256, sizeBytes } = await streamToFile(res, zipPath, maxBytes);
  if (minted.sha256 && sha256 !== minted.sha256) {
    await rm(zipPath, { force: true });
    throw new Error(`bundle for "${slug}" failed sha256 verification (expected ${minted.sha256}, got ${sha256})`);
  }
  return { zipPath, sha256, sizeBytes, version: minted.version, releaseId: minted.releaseId };
}

/** Mint + download + verify a slug's bundle in one call. */
export async function fetchBundle(
  controlPlaneUrl: string,
  token: string,
  slug: string,
  opts: FetchBundleOptions = {}
): Promise<FetchedBundle> {
  const minted = await mintBundleDownload(controlPlaneUrl, token, slug, opts.fetchImpl ?? fetch);
  return downloadMinted(controlPlaneUrl, token, slug, minted, opts);
}

/** Refuse entry names that could write outside destDir: absolute paths, drive
 *  letters, backslashes, `..` (or empty) segments. Returns the safe absolute
 *  target path. */
function safeEntryPath(destDir: string, name: string): string {
  const bad = () => new Error(`bundle entry has an unsafe path: "${name}"`);
  if (!name || name.includes("\\") || name.startsWith("/") || /^[a-zA-Z]:/.test(name)) throw bad();
  const segments = name.split("/").filter((s, i, all) => !(s === "" && i === all.length - 1));
  if (segments.length === 0 || segments.some((s) => s === "" || s === "." || s === "..")) throw bad();
  const full = resolve(destDir, segments.join("/"));
  if (full !== destDir && !full.startsWith(destDir + sep)) throw bad();
  return full;
}

/**
 * Extract a fetched `.glyphh` archive into `destDir` and validate it as an app
 * bundle. Every entry NAME is vetted before any byte is written, so a
 * traversal entry poisons nothing. Throws (and leaves destDir best-effort
 * cleaned) on a missing/invalid manifest or a missing entry/worker file.
 */
export async function materialize(zipPath: string, destDir: string): Promise<AppBundleManifest> {
  const buf = await readFile(zipPath);
  const entries = readZipEntries(buf).filter((e) => !e.isDirectory);

  const dest = resolve(destDir);
  await mkdir(dest, { recursive: true });
  try {
    // Vet ALL names first — extraction starts only once the whole listing is safe.
    const targets = entries.map((e) => ({ entry: e, path: safeEntryPath(dest, e.name) }));

    const manifestEntry = entries.find((e) => e.name === "manifest.json");
    if (!manifestEntry) throw new Error("bundle has no manifest.json");
    const manifest = readAppBundleManifest(manifestEntry.data().toString("utf8"));
    if (!manifest) throw new Error("manifest.json is not a valid glyphh-app manifest");

    for (const t of targets) {
      await mkdir(dirname(t.path), { recursive: true });
      await writeFile(t.path, t.entry.data());
    }
    if (!existsSync(join(dest, manifest.entry))) {
      throw new Error(`bundle entry not found after extract: ${manifest.entry}`);
    }
    if (manifest.worker && !existsSync(join(dest, manifest.worker))) {
      throw new Error(`bundle worker not found after extract: ${manifest.worker}`);
    }
    return manifest;
  } catch (err) {
    await rm(dest, { recursive: true, force: true });
    throw err;
  }
}

export interface AppBundleCacheOptions {
  /** Root for materialized bundles: `<cacheDir>/<sha256>/…`. */
  cacheDir: string;
  controlPlaneUrl: string;
  token: string;
  fetchImpl?: typeof fetch;
  maxBytes?: number;
}

/** Marker written after a bundle is fully extracted — a dir without it is a
 *  partial extraction and is re-materialized. */
const READY_MARKER = ".materialized";

/**
 * The pod's content-addressed bundle cache. `resolveApp(slug)` asks the
 * control plane which release is current (cheap control-plane GET), and only
 * downloads + extracts when that content hash is not already materialized —
 * a re-fetch with the same hash is a no-op.
 */
export class AppBundleCache {
  private readonly inflight = new Map<string, Promise<ResolvedApp>>();

  constructor(private readonly opts: AppBundleCacheOptions) {}

  /** Resolve a slug to its current, locally-materialized bundle. */
  async resolveApp(slug: string): Promise<ResolvedApp> {
    // Collapse concurrent resolves of one slug into a single fetch/extract.
    const running = this.inflight.get(slug);
    if (running) return running;
    const p = this.resolveUncoalesced(slug).finally(() => this.inflight.delete(slug));
    this.inflight.set(slug, p);
    return p;
  }

  private async resolveUncoalesced(slug: string): Promise<ResolvedApp> {
    const { cacheDir, controlPlaneUrl, token, fetchImpl, maxBytes } = this.opts;
    const f = fetchImpl ?? fetch;
    const minted = await mintBundleDownload(controlPlaneUrl, token, slug, f);

    // Known digest already on disk → serve the extraction we have.
    if (minted.sha256) {
      const cached = await this.fromDisk(minted.sha256);
      if (cached) return cached;
    }

    const fetched = await downloadMinted(controlPlaneUrl, token, slug, minted, {
      fetchImpl: f,
      maxBytes,
      tmpDir: join(cacheDir, "tmp"),
    });
    try {
      // The digest can be newly learned (dev releases may not report one) —
      // check the cache again under the ACTUAL hash before extracting.
      const cached = await this.fromDisk(fetched.sha256);
      if (cached) return cached;

      // Extract to a staging dir, then rename into place: the content-hash dir
      // either exists fully materialized or not at all.
      const finalDir = join(cacheDir, fetched.sha256);
      const staging = join(cacheDir, "tmp", `extract-${fetched.sha256}-${process.pid}`);
      await rm(staging, { recursive: true, force: true });
      const manifest = await materialize(fetched.zipPath, staging);
      await writeFile(join(staging, READY_MARKER), fetched.sha256);
      await rm(finalDir, { recursive: true, force: true });
      await rename(staging, finalDir);
      return { dir: finalDir, manifest, sha256: fetched.sha256 };
    } finally {
      await rm(fetched.zipPath, { force: true });
    }
  }

  /** A materialized dir for this hash, or null. Rejects partial extractions. */
  private async fromDisk(sha256: string): Promise<ResolvedApp | null> {
    const dir = join(this.opts.cacheDir, sha256);
    if (!existsSync(join(dir, READY_MARKER))) return null;
    try {
      const manifest = readAppBundleManifest(await readFile(join(dir, "manifest.json"), "utf8"));
      if (!manifest) return null;
      return { dir, manifest, sha256 };
    } catch {
      return null;
    }
  }
}

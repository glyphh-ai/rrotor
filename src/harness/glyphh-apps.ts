/**
 * glyphh-apps.ts — the control plane's app tool surface, wired into every run.
 *
 * WHY THIS IS POD-SIDE AND NOT CLIENT-SIDE. Publishing an app is the one thing
 * a harness run cannot do with files and a shell: it needs the control plane.
 * The desktop solves this for a LOCAL pod by lending its own loopback MCP
 * server, but a CLOUD pod cannot reach a user's loopback — so a cloud agent
 * used to write an app and then have nowhere to put it, and would fall back to
 * `python3 -m http.server` inside a container nothing can reach.
 *
 * The fix could have been "make each client pass another `mcpServers` entry",
 * but there is more than one client (desktop, web, CLI) and every one of them
 * would have to agree, forever, or the surfaces drift apart again. Instead the
 * POD derives the entry itself from configuration it is ALREADY REQUIRED to
 * have: a gateway URL and a runtime token. Every caller therefore gets the same
 * app tools with no client change, which is what makes desktop and cloud
 * identical rather than merely similar.
 *
 * TRUST. This ref is derived from the pod's own gateway configuration, not from
 * caller-supplied JSON, so it does not go through `parseMcpServers` (whose
 * loopback-or-https rule exists to stop a CALLER pointing a cloud pod at an
 * arbitrary plaintext URL). A dev control plane on `http://192.168.x.y:3000` is
 * a legitimate target here and would fail that rule for the wrong reason. The
 * URL is still validated as http(s) before it is used.
 */

import { readdir, stat, readFile } from "node:fs/promises";
import { join, resolve, sep } from "node:path";

/** The lent server's name on the wire. Must match rrotor's own
 *  `^[a-z0-9_-]{1,32}$` rule and must not be `glyphh` (the in-process ask_user
 *  server). Tools reach the model as `mcp__glyphh_apps__<tool>`. */
export const APPS_SERVER_NAME = "glyphh_apps";

/** The publish tool's wire name — the seam engine.ts intercepts to expand
 *  `{ distDir }` into the `{ files }` map the server contract requires. */
export const BUILD_APP_TOOL = `mcp__${APPS_SERVER_NAME}__build_app`;
export const SAVE_FILE_TOOL = `mcp__${APPS_SERVER_NAME}__save_file`;

/** save_file { path }: the POD reads the workspace file and inlines it as
 *  contentBase64 — the model passes a path, never bytes, and the SERVER
 *  contract ({ name, contentBase64, mime }) is unchanged. Bounded by the
 *  control plane's 16 MB limit on /api/runtime/mcp (base64 ≈ 4/3 raw). */
const SAVE_FILE_MAX_BYTES = 10 * 1024 * 1024;
const MIME_BY_EXT: Record<string, string> = {
  md: "text/markdown", txt: "text/plain", html: "text/html", css: "text/css",
  js: "text/javascript", json: "application/json", csv: "text/csv",
  pdf: "application/pdf", png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
  gif: "image/gif", svg: "image/svg+xml", webp: "image/webp", zip: "application/zip",
};
export async function expandSaveFileInput(
  input: unknown,
  workdir: string,
): Promise<{ ok: true; input: Record<string, unknown> } | { ok: false; error: string } | null> {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  const args = { ...(input as Record<string, unknown>) };
  const path = typeof args.path === "string" ? args.path.trim() : "";
  if (!path) return null;   // inline content/contentBase64 — untouched
  const root = resolve(workdir || ".");
  const abs = resolve(root, path);
  if (abs !== root && !abs.startsWith(root + sep)) {
    return { ok: false, error: `path must be a file inside the working folder (got "${path}")` };
  }
  let bytes: Buffer;
  try { bytes = await readFile(abs); }
  catch { return { ok: false, error: `no such file: ${path}` }; }
  if (!bytes.byteLength) return { ok: false, error: `${path} is empty — nothing to save` };
  if (bytes.byteLength > SAVE_FILE_MAX_BYTES) {
    return { ok: false, error: `${path} is ${Math.round(bytes.byteLength / 1024)}KB — save_file is capped at ${Math.round(SAVE_FILE_MAX_BYTES / 1024)}KB (the runtime MCP request limit); export something smaller or split it` };
  }
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  delete args.path;
  if (typeof args.name !== "string" || !args.name.trim()) args.name = path.split("/").pop() || "artifact";
  args.contentBase64 = bytes.toString("base64");
  if (typeof args.mime !== "string" || !args.mime) {
    const m = MIME_BY_EXT[ext];
    if (m) args.mime = m;
  }
  return { ok: true, input: args };
}

/** The control plane's pod-facing MCP route. Mirrors the server's
 *  `RUNTIME_MCP_PATH` — the two must agree. */
const RUNTIME_MCP_PATH = "/api/runtime/mcp";

/** What one lent HTTP MCP server looks like (structurally `McpServerRef`). */
export interface AppsServerRef {
  name: string;
  url: string;
  headers: Record<string, string>;
}

/**
 * Turn a gateway URL into the control plane's runtime MCP URL.
 *
 * The gateway is always `<control-plane-origin>/api/gateway`, so the origin is
 * recoverable by dropping that suffix — which keeps a working default for every
 * caller that never learns about a second URL. An explicit `controlUrl` (body
 * or `GLYPHH_CONTROL_URL`) always wins, so a deployment that ever splits the
 * gateway from the control plane has a seam that does not require a code change.
 *
 * Returns null when nothing usable can be derived — the run then proceeds
 * WITHOUT app tools rather than failing, because a chat turn that needs no
 * publishing must not be broken by a missing control-plane URL.
 */
export function runtimeMcpUrl(gatewayUrl: string, controlUrl?: string): string | null {
  const explicit = (controlUrl ?? "").trim();
  const base = explicit || (gatewayUrl ?? "").trim();
  if (!base) return null;

  let parsed: URL;
  try {
    parsed = new URL(base);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;

  // An explicit control URL may already BE the full endpoint; otherwise it is
  // an origin/prefix. Either way, normalize onto the one route.
  let path = parsed.pathname.replace(/\/+$/, "");
  if (path.endsWith(RUNTIME_MCP_PATH)) {
    return `${parsed.origin}${path}`;
  }
  path = path.replace(/\/api\/gateway$/, "").replace(/\/gateway$/, "");
  return `${parsed.origin}${path}${RUNTIME_MCP_PATH}`;
}

/**
 * The app tool surface for one run, or null when it cannot be addressed.
 *
 * The credential is the run's OWN runtime token: `gy_rk_` for a cloud pod
 * (resolved by the control plane to the session owner) or the user's `gy_at_`
 * for a desktop-local pod. `resolvePrincipal` accepts both, so one code path
 * serves both surfaces.
 */
export function appsServerRef(cfg: {
  gatewayUrl: string;
  runtimeToken: string;
  controlUrl?: string | undefined;
}): AppsServerRef | null {
  if (!cfg.runtimeToken) return null;
  const url = runtimeMcpUrl(cfg.gatewayUrl, cfg.controlUrl);
  if (!url) return null;
  return {
    name: APPS_SERVER_NAME,
    url,
    // The bearer is a secret; it is never logged (redactSecrets scrubs the
    // runtime token from every diagnostic string).
    headers: { authorization: `Bearer ${cfg.runtimeToken}` },
  };
}

// ── build_app { distDir } — the pod inlines the bundle itself ──────────────
//
// The server's `build_app` contract is `{ slug, files }` — a path→content map.
// Forcing the MODEL to produce that map means reading every built file and
// pasting its contents into a tool call, which fails outright on a real Vite
// bundle (the JS is "too large" to read) and sends the agent hunting for an
// upload path that does not exist. The pod fixes this on ITS side of the wire:
// `build_app { slug, distDir }` is expanded here — the pod walks the directory
// on its own filesystem, builds the map, and the server receives the exact
// contract it always had. The server was NOT changed.

/** The control plane parses `/api/runtime/mcp` with its default 1 MB JSON body
 *  limit (server `express.json({ limit: "1mb" })`; the deeper 50 MB source cap
 *  never binds first on this route). Stay under it with headroom for the
 *  JSON-RPC envelope. */
const MAX_INLINE_JSON_BYTES = 15 * 1024 * 1024;

/** Never walk into these — build output should not contain them, and a stray
 *  `node_modules` would blow the payload instantly. */
const SKIP_DIRS = new Set(["node_modules", ".git"]);

/** Text rides as a plain string; anything else as `{ base64 }`, which the
 *  server's build_app value contract explicitly supports. */
const TEXT_EXT = new Set(["html", "htm", "js", "mjs", "cjs", "css", "json", "svg", "txt", "md", "xml", "webmanifest", "csv", "tsv"]);

const MAX_FILES = 2000;

type FileValue = string | { base64: string };

export type DistExpansion =
  | { ok: true; input: Record<string, unknown>; fileCount: number; jsonBytes: number; skipped: string[] }
  | { ok: false; error: string };

async function walkDist(dir: string, rel: string, out: Array<{ rel: string; abs: string }>, skipped: string[]): Promise<void> {
  const entries = await readdir(dir, { withFileTypes: true });
  for (const e of entries) {
    const childRel = rel ? `${rel}/${e.name}` : e.name;
    if (e.isDirectory()) {
      // Dot-directories and dependency/VCS trees are never build output.
      if (SKIP_DIRS.has(e.name) || e.name.startsWith(".")) { skipped.push(`${childRel}/`); continue; }
      await walkDist(join(dir, e.name), childRel, out, skipped);
      continue;
    }
    if (!e.isFile()) continue;
    // Sourcemaps are dropped by default: the server contract does not require
    // them, and they routinely double a bundle's inline size.
    if (e.name.endsWith(".map") || e.name === ".DS_Store") { skipped.push(childRel); continue; }
    out.push({ rel: childRel, abs: join(dir, e.name) });
    if (out.length > MAX_FILES) throw new Error(`more than ${MAX_FILES} files under the dist folder — that is not a built bundle`);
  }
}

/**
 * Expand a `build_app { slug, distDir }` call into the server's
 * `{ slug, files }` contract by walking `distDir` on the POD's filesystem.
 *
 * Returns null when no expansion applies (an inline `files` map was passed and
 * no `distDir` named) — the call goes through untouched. `distDir` must stay
 * INSIDE the run's working folder: a local pod's workdir is the user's real
 * project, and an absolute path outside it would let a published app inhale
 * arbitrary files from the machine.
 */
export async function expandBuildAppInput(input: unknown, workdir: string): Promise<DistExpansion | null> {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  const args = input as Record<string, unknown>;
  const inlineFiles = args.files && typeof args.files === "object" && !Array.isArray(args.files) && Object.keys(args.files as object).length > 0;
  const named = typeof args.distDir === "string" ? args.distDir.trim() : "";
  // Inline files with no distDir: the small hand-written case — untouched.
  if (inlineFiles && !named) return null;
  const distDir = named || "dist";

  const root = resolve(workdir || ".");
  const dir = resolve(root, distDir);
  if (dir !== root && !dir.startsWith(root + sep)) {
    return { ok: false, error: `distDir must be a folder inside the working folder (got "${distDir}")` };
  }

  try {
    const st = await stat(dir).catch(() => null);
    if (!st?.isDirectory()) {
      return { ok: false, error: `no build output at "${distDir}" — run the build first (e.g. \`npm run build\`), or pass the folder that contains the built index.html as distDir` };
    }

    const found: Array<{ rel: string; abs: string }> = [];
    const skipped: string[] = [];
    await walkDist(dir, "", found, skipped);
    if (!found.length) return { ok: false, error: `"${distDir}" is empty — run the build first` };
    if (!found.some((f) => f.rel.toLowerCase() === "index.html")) {
      return { ok: false, error: `"${distDir}" has no index.html at its root — pass the folder that CONTAINS the built index.html as distDir` };
    }

    const files: Record<string, FileValue> = {};
    for (const f of found) {
      const buf = await readFile(f.abs);
      const ext = f.rel.split(".").pop()?.toLowerCase() ?? "";
      files[f.rel] = TEXT_EXT.has(ext) ? buf.toString("utf8") : { base64: buf.toString("base64") };
    }

    const jsonBytes = Buffer.byteLength(JSON.stringify(files), "utf8");
    if (jsonBytes > MAX_INLINE_JSON_BYTES) {
      return {
        ok: false,
        error: `the built bundle inlines to ~${Math.round(jsonBytes / 1024)} KB of JSON — over the control plane's 16 MB request limit on build_app (/api/runtime/mcp). Shrink the bundle: drop large assets, split vendor chunks, compress images.`,
      };
    }

    const { distDir: _dropped, ...rest } = args;
    return { ok: true, input: { ...rest, files }, fileCount: found.length, jsonBytes, skipped };
  } catch (err) {
    return { ok: false, error: `could not read "${distDir}": ${err instanceof Error ? err.message : String(err)}` };
  }
}

/**
 * The non-negotiable publish policy, appended to whatever system prompt a run
 * carries.
 *
 * It lives HERE, next to the tools it describes, rather than in each client's
 * prompt builder. A client-side rule only steers the clients that were updated;
 * this one steers every run that has the tools, which is the same set that can
 * act on it. The dev-server prohibition is explicit because it is the exact
 * failure this whole path exists to remove: an agent with nowhere to publish
 * reaches for `http.server`, and in a cloud pod that produces a URL nobody can
 * open.
 */
export const PUBLISH_POLICY = [
  "## Building and running a Glyphh app",
  "",
  "When the user asks for an app, build a REAL project and RUN IT LOCALLY on their machine. Developing and showing an app NEVER touch a public URL. The develop loop:",
  "1. scaffold_app { slug } — the shared starting Vite project as a { path: content } map. Write those files into the working folder. Do not hand-roll a project instead; this scaffold is the same on every surface.",
  "2. npm install && npm run build — Vite emits dist/.",
  "3. update_app() — install the built app LOCALLY as a panel on the user's machine (it packages dist/, never your source). After ANY change: rebuild + update_app, or tools run the stale installed version.",
  "4. open_app(<slug>) — show it. It opens as a LOCAL panel (no URL, no login). THIS is the preview; there is nothing to deploy to see it.",
  "",
  "Working on an EXISTING app in a fresh workspace? pull_source { app } FIRST — the working folder is not the source of record; the snapshot is.",
  "",
  "PUBLISHING is a SEPARATE, EXPLICIT step — ONLY when the user asks to put the app on the PUBLIC web at https://<slug>.glyphh.app (external access). NEVER publish just to show or test an app during development. When they ask to publish: create_app_entry { kind: 'glyphh', slug, name } (returns the MINTED slug — use it from then on) → build_app { slug, distDir: 'dist' } (the runtime walks the folder, inlines every file, uploads, cuts a release, deploys live; NEVER read built files or paste their contents) → push_source { app: <slug> } (source of record to R2) → then give the user the returned https://<slug>.glyphh.app URL.",
  "",
  "NEVER start a local web server to show an app — not `python3 -m http.server`, not `npx serve`, not `vite preview` — and do NOT deploy to a URL just to preview: use update_app + open_app (the local panel). EXCEPTION: a HEADLESS session with NO connected desktop cannot open a local panel; only there, deploy a preview and show that URL.",
  "Bundle every dependency at build time. The host CSP blocks remote scripts, stylesheets and fetches, so a CDN reference produces an app that will not run.",
].join("\n");

/** Append the publish policy to a run's system prompt, once. */
export function withPublishPolicy(system: string): string {
  if (system.includes("## Building and running a Glyphh app")) return system;
  return system ? `${system}\n\n${PUBLISH_POLICY}` : PUBLISH_POLICY;
}

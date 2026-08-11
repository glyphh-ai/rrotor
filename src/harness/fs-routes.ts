/**
 * harness/fs-routes.ts — the WORKSPACE FS API of a harness pod: the pod-side
 * twin of the desktop's working-directory surface, so the WEB workbench's
 * Files / Diff / Editor panels work against a CLOUD session's workspace
 * exactly as they do against the attached desktop.
 *
 *   GET|POST /fs/info    {scope}                → { root, isRepo, branch? }
 *   GET|POST /fs/list    {scope, dir?}          → { entries:[{name,path,kind}], error? }
 *   GET|POST /fs/read    {scope, path}          → { content, error? }         (1MB cap)
 *   POST     /fs/write   {scope, path, content} → { ok, error? }              (2MB cap)
 *   GET|POST /fs/diff    {scope}                → { text, error? }
 *   GET|POST /fs/search  {scope, query}         → { entries:[{name,rel,path,kind}], truncated?, error? }
 *   GET      /fs/events  ?scope=&from=<seq>     → { seq, root, events:[{seq,type,path?}] }
 *
 * RESPONSE SHAPES REPLICATE the desktop implementations 1:1 (app/src/main/
 * working-directory.ts: listWorkdirDir / readWorkdirFile / writeWorkdirFile /
 * gitDiffWorkdir / gitInfoForDir / searchWorkdirFiles) — same listing filters,
 * same size caps, same error shapes — so the workbench panels render pod and
 * desktop answers identically. fs errors ride IN the shape (200), exactly as
 * the desktop returns them; only parameter/auth failures are HTTP errors.
 *
 * SCOPE → WORKSPACE ROOT rides config.ts's ONE keying rule (workspaceSegment):
 * a token-bound session (dedicated pod) forces its own workspace; a shared-pod
 * caller names its client thread id as `scope`, prefixed server-side by the
 * INTROSPECTED owner's user id — so no caller can reach another user's
 * workspace by guessing a thread id, and the fs surface always resolves the
 * same directory a run with that threadId executes in.
 *
 * AUTH: gated by the server's introspection gate like everything else, and —
 * same as the stator routes — a caller that resolved to NO principal is
 * rejected (403): workspaces are owner-scoped, an unattributed caller cannot
 * be. (A local auth-off pod has no principal either — and no need for this
 * API: the desktop's wb is the machine itself.)
 *
 * PATH CONTAINMENT: every path is resolved against the workspace root and
 * must stay inside it — `..`, absolute escapes, and symlink hops out of the
 * root (realpath on the deepest existing ancestor) are all refused. See
 * {@link containPath} (unit-tested in test/unit/harness-fs-routes.test.ts).
 *
 * EVENTS: one recursive fs.watch per workspace (started by the first
 * /fs/events poll, swept after idle), debounced ~250ms per path, `.git`
 * skipped EXCEPT index/HEAD which coalesce into `git-changed` — the same
 * timing/filtering as the desktop watcher. Events land in a small ring with
 * monotonic seq numbers; clients cursor-poll with `from`.
 */

import * as http from "node:http";
import { watch, type FSWatcher } from "node:fs";
import { mkdir, readdir, readFile, stat, writeFile, realpath } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { dirname, isAbsolute, join, normalize, relative, resolve, sep } from "node:path";

import { log } from "../obs/logger.js";
import type { Principal } from "../auth/introspect.js";
import { sessionWorkspace, workspaceSegment } from "./config.js";

const execFileP = promisify(execFile);

/** What the server's auth gate resolved (structural mirror of server.ts's
 *  AuthContext — kept here so server.ts's diff stays one import + one hook). */
export interface FsAuthContext {
  principal?: Principal;
  sessionId?: string;
  enabled: boolean;
}

const READ_MAX_BYTES = 1_000_000; // desktop readWorkdirFile's cap, verbatim
const WRITE_MAX_BYTES = 2_000_000; // per the fs surface's contract
const BODY_LIMIT = 4_000_000; // write = content cap + JSON envelope headroom
const GIT_BUFFER = 10 * 1024 * 1024;
const SCOPE_RE = /^[A-Za-z0-9_-]{1,64}$/; // config.ts THREAD_ID_RE, same ids

// ── plumbing (same conventions as server.ts: JSON + wildcard CORS) ──────────

function sendJson(res: http.ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload),
    "cache-control": "no-store",
    "access-control-allow-origin": "*",
  });
  res.end(payload);
}

function readBody(req: http.IncomingMessage, limit = BODY_LIMIT): Promise<string> {
  return new Promise<string>((resolveBody, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error("body too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolveBody(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

const msg = (err: unknown): string => (err instanceof Error ? err.message : String(err));

/** Merge GET query params with a POST JSON body (body wins where both speak). */
async function paramsOf(req: http.IncomingMessage, method: string): Promise<Record<string, unknown>> {
  const query = (req.url ?? "").split("?", 2)[1] ?? "";
  const out: Record<string, unknown> = {};
  for (const [k, v] of new URLSearchParams(query)) out[k] = v;
  if (method === "POST") {
    const raw = await readBody(req);
    if (raw.length) {
      const body = JSON.parse(raw) as unknown; // caller maps a throw to 400
      if (body && typeof body === "object") Object.assign(out, body as Record<string, unknown>);
    }
  }
  return out;
}

// ── scope → workspace root ──────────────────────────────────────────────────

/** Resolve the caller's workspace root. Token-bound session (dedicated pod)
 *  wins outright; otherwise the caller's `scope` (its client thread id) keys a
 *  workspace under the introspected OWNER — config.ts's exact run keying. */
function resolveRoot(
  authn: FsAuthContext,
  env: NodeJS.ProcessEnv,
  scope: unknown,
): { root: string } | { status: number; error: string; detail: string } {
  const bound = authn.sessionId ?? (typeof env.ROTOR_SESSION_ID === "string" && env.ROTOR_SESSION_ID.trim() ? env.ROTOR_SESSION_ID.trim() : "");
  if (bound) return { root: sessionWorkspace(env, workspaceSegment({ sessionId: bound, runId: "" })) };
  const owner = authn.principal?.userId ?? "";
  const threadId = typeof scope === "string" ? scope.trim() : "";
  if (!threadId || !SCOPE_RE.test(threadId)) {
    return { status: 400, error: "bad-scope", detail: "`scope` (the session's thread id, ^[A-Za-z0-9_-]{1,64}$) is required on a shared pod" };
  }
  return { root: sessionWorkspace(env, workspaceSegment({ owner, threadId, runId: "" })) };
}

// ── path containment ────────────────────────────────────────────────────────

/** Is `candidate` the root itself or inside it? (Both sides pre-normalized.) */
function within(root: string, candidate: string): boolean {
  const rel = relative(root, candidate);
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

/** Realpath the deepest EXISTING ancestor of `abs`, re-appending the missing
 *  tail — so a symlink anywhere on the path cannot hop out of the root even
 *  when the leaf does not exist yet (writes create files). */
async function realpathDeepest(abs: string): Promise<string> {
  let base = abs;
  let tail = "";
  // Walk up until something exists; the root's own dir always does.
  for (;;) {
    try {
      const real = await realpath(base);
      return tail ? join(real, tail) : real;
    } catch {
      const parent = dirname(base);
      if (parent === base) return abs; // filesystem root — nothing resolved
      tail = tail ? join(base.slice(parent.length + 1), tail) : base.slice(parent.length + 1);
      base = parent;
    }
  }
}

/**
 * Resolve a caller path against the workspace root and prove containment.
 * Accepts absolute paths (the panels echo back listing paths, which are
 * absolute) and root-relative ones. Returns the absolute path to act on, or
 * null when the path is missing/escapes. Exported for the unit tests.
 */
export async function containPath(root: string, raw: unknown): Promise<string | null> {
  if (typeof raw !== "string" || !raw.trim()) return null;
  const p = raw.trim();
  if (p.includes("\0")) return null;
  const abs = normalize(isAbsolute(p) ? p : resolve(root, p));
  if (!within(root, abs)) return null;
  // Symlink escape: judge the REAL location too (deepest existing ancestor —
  // the leaf may not exist yet for a write). The root is realpath'd so a
  // tmpdir-symlinked root (macOS /tmp) compares in the same coordinates.
  try {
    const realRoot = await realpathDeepest(root);
    const realAbs = await realpathDeepest(abs);
    if (!within(realRoot, realAbs)) return null;
  } catch {
    return null; // cannot prove containment → refuse
  }
  return abs;
}

// ── the desktop implementations, replicated pod-side (same shapes) ──────────

interface DirEntry { name: string; path: string; kind: "dir" | "file" }
interface SearchEntry extends DirEntry { rel: string }

/** Immediate children — the desktop's listWorkdirDir, verbatim behavior. */
async function listDir(dir: string): Promise<{ entries: DirEntry[]; error?: string }> {
  try {
    const items = await readdir(dir, { withFileTypes: true });
    const entries = items
      .filter((d) => d.name !== ".git" && d.name !== "node_modules")
      .map((d) => ({
        name: d.name,
        path: join(dir, d.name),
        kind: d.isDirectory() ? ("dir" as const) : ("file" as const),
      }))
      .sort((a, b) => {
        if (a.kind !== b.kind) return a.kind === "dir" ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
    return { entries };
  } catch (err) {
    return { entries: [], error: msg(err) };
  }
}

/** Recursive NAME search — the desktop's searchWorkdirFiles, verbatim caps. */
async function searchFiles(root: string, query: unknown): Promise<{ entries: SearchEntry[]; truncated?: boolean; error?: string }> {
  const q = String(query ?? "").trim().toLowerCase();
  if (!q) return { entries: [] };
  const SKIP = new Set([".git", "node_modules", "dist", "build", "out", ".next", "coverage", "vendor", "__pycache__", ".venv", "target"]);
  const MAX_RESULTS = 200;
  const MAX_DIRS = 4000;
  const entries: SearchEntry[] = [];
  const stack = [root];
  let visited = 0;
  try {
    while (stack.length && entries.length < MAX_RESULTS && visited < MAX_DIRS) {
      const dir = stack.pop()!;
      visited++;
      let items: Array<{ name: string; isDirectory(): boolean }> = [];
      try { items = await readdir(dir, { withFileTypes: true }); } catch { continue; }
      for (const d of items) {
        if (SKIP.has(d.name)) continue;
        const abs = join(dir, d.name);
        const isDir = d.isDirectory();
        if (d.name.toLowerCase().includes(q)) {
          entries.push({ name: d.name, rel: relative(root, abs), path: abs, kind: isDir ? "dir" : "file" });
          if (entries.length >= MAX_RESULTS) break;
        }
        if (isDir) stack.push(abs);
      }
    }
    entries.sort((a, b) => {
      const da = a.rel.split(sep).length;
      const db = b.rel.split(sep).length;
      if (da !== db) return da - db;
      return a.rel.localeCompare(b.rel);
    });
    return { entries, truncated: entries.length >= MAX_RESULTS };
  } catch (err) {
    return { entries: [], error: msg(err) };
  }
}

/** Read a text file — the desktop's readWorkdirFile (1MB cap, same message). */
async function readWorkdirFile(filePath: string): Promise<{ content: string; error?: string }> {
  try {
    const s = await stat(filePath);
    if (s.size > READ_MAX_BYTES) {
      return { content: "", error: `file too large (${(s.size / 1024 / 1024).toFixed(1)}MB)` };
    }
    return { content: await readFile(filePath, "utf8") };
  } catch (err) {
    return { content: "", error: msg(err) };
  }
}

/** Write a text file, creating parents — the desktop's writeWorkdirFile. */
async function writeWorkdirFile(filePath: string, content: unknown): Promise<{ ok: boolean; error?: string }> {
  try {
    await mkdir(dirname(filePath), { recursive: true });
    await writeFile(filePath, typeof content === "string" ? content : "", "utf8");
    return { ok: true };
  } catch (err) {
    return { ok: false, error: msg(err) };
  }
}

/** Repo probe + branch — the desktop's gitInfoForDir. */
async function gitInfo(root: string): Promise<{ isRepo: boolean; branch?: string }> {
  const result: { isRepo: boolean; branch?: string } = { isRepo: false };
  try {
    const inside = await execFileP("git", ["rev-parse", "--is-inside-work-tree"], { cwd: root });
    if (inside.stdout.trim() !== "true") return result;
    result.isRepo = true;
    try {
      const head = await execFileP("git", ["rev-parse", "--abbrev-ref", "HEAD"], { cwd: root });
      const branch = head.stdout.trim();
      if (branch && branch !== "HEAD") result.branch = branch;
    } catch { /* detached HEAD — leave branch undefined */ }
  } catch { /* not a repo or git unavailable */ }
  return result;
}

/** Untracked files as added-file diffs — the desktop's untrackedAsDiff. */
async function untrackedAsDiff(root: string): Promise<string> {
  try {
    const { stdout } = await execFileP("git", ["ls-files", "--others", "--exclude-standard"], { cwd: root });
    const files = stdout.split("\n").filter(Boolean).slice(0, 200);
    let text = "";
    for (const f of files) {
      try {
        await execFileP("git", ["diff", "--no-index", "--", "/dev/null", f], { cwd: root, maxBuffer: GIT_BUFFER });
      } catch (err) {
        const out = (err as { stdout?: string }).stdout;
        if (out) text += out;
      }
    }
    return text;
  } catch {
    return ""; // untracked listing failed — the tracked diff still shows
  }
}

/** Working-tree diff (staged + unstaged + untracked) — the desktop's
 *  gitDiffWorkdir, including the initial-commit (no HEAD) fallback. */
async function gitDiff(root: string): Promise<{ text: string; error?: string }> {
  try {
    const probe = await execFileP("git", ["rev-parse", "--is-inside-work-tree"], { cwd: root });
    if (probe.stdout.trim() !== "true") return { text: "", error: "not a git repo" };
  } catch {
    return { text: "", error: "not a git repo" };
  }
  try {
    const { stdout } = await execFileP("git", ["diff", "HEAD"], { cwd: root, maxBuffer: GIT_BUFFER });
    return { text: stdout + (await untrackedAsDiff(root)) };
  } catch (err) {
    const m = err instanceof Error ? (err as Error & { stderr?: string }).stderr || err.message : String(err);
    if (/unknown revision|ambiguous argument 'HEAD'|bad revision/i.test(m)) {
      try {
        const { stdout } = await execFileP("git", ["diff"], { cwd: root, maxBuffer: GIT_BUFFER });
        return { text: stdout + (await untrackedAsDiff(root)) };
      } catch (err2) {
        const m2 = err2 instanceof Error ? (err2 as Error & { stderr?: string }).stderr || err2.message : String(err2);
        return { text: "", error: m2.split("\n")[0] || "git error" };
      }
    }
    return { text: "", error: m.split("\n")[0] || "git error" };
  }
}

// ── change events (cursor-polled ring over one recursive watcher) ───────────

export interface FsEvent {
  seq: number;
  type: "file-changed" | "git-changed";
  path?: string;
}

interface WatchState {
  watcher: FSWatcher | null;
  seq: number;
  ring: FsEvent[];
  timers: Map<string, ReturnType<typeof setTimeout>>;
  gitTimer: ReturnType<typeof setTimeout> | null;
  lastPoll: number;
}

const RING_CAP = 500;
const DEBOUNCE_MS = 250;
const IDLE_SWEEP_MS = 5 * 60_000; // no poll for 5min → the watcher closes

const watchers = new Map<string, WatchState>();
let sweeper: ReturnType<typeof setInterval> | null = null;

function pushEvent(state: WatchState, ev: Omit<FsEvent, "seq">): void {
  state.seq += 1;
  state.ring.push({ seq: state.seq, ...ev });
  if (state.ring.length > RING_CAP) state.ring.splice(0, state.ring.length - RING_CAP);
}

function stopWatch(root: string, state: WatchState): void {
  try { state.watcher?.close(); } catch { /* already gone */ }
  for (const t of state.timers.values()) clearTimeout(t);
  if (state.gitTimer) clearTimeout(state.gitTimer);
  watchers.delete(root);
}

/** Close every workspace watcher (tests; also safe on shutdown). */
export function closeAllFsWatchers(): void {
  for (const [root, state] of [...watchers]) stopWatch(root, state);
  if (sweeper) { clearInterval(sweeper); sweeper = null; }
}

function ensureSweeper(): void {
  if (sweeper) return;
  sweeper = setInterval(() => {
    const now = Date.now();
    for (const [root, state] of [...watchers]) {
      if (now - state.lastPoll > IDLE_SWEEP_MS) stopWatch(root, state);
    }
    if (!watchers.size && sweeper) { clearInterval(sweeper); sweeper = null; }
  }, 60_000);
  sweeper.unref?.();
}

/** Start (or reuse) the workspace's watcher — same filtering/timing as the
 *  desktop's workdir watcher: debounced per path, `.git` skipped except
 *  index/HEAD which coalesce into ONE git-changed per git operation. */
function ensureWatch(root: string): WatchState {
  const existing = watchers.get(root);
  if (existing) { existing.lastPoll = Date.now(); return existing; }
  const state: WatchState = { watcher: null, seq: 0, ring: [], timers: new Map(), gitTimer: null, lastPoll: Date.now() };
  try {
    state.watcher = watch(root, { recursive: true, persistent: false }, (_eventType, filename) => {
      if (!filename) return;
      const f = String(filename).replace(/\\/g, "/");
      if (f === ".git/index" || f === ".git/HEAD") {
        if (state.gitTimer) return;
        state.gitTimer = setTimeout(() => {
          state.gitTimer = null;
          pushEvent(state, { type: "git-changed" });
        }, DEBOUNCE_MS);
        return;
      }
      if (f.includes("node_modules") || f.startsWith(".git/") || f.includes("/.git/")) return;
      const abs = join(root, f);
      const prev = state.timers.get(abs);
      if (prev) clearTimeout(prev);
      state.timers.set(abs, setTimeout(() => {
        state.timers.delete(abs);
        pushEvent(state, { type: "file-changed", path: abs });
      }, DEBOUNCE_MS));
    });
  } catch (err) {
    // Recursive watch unavailable (old kernel/filesystem) — events degrade to
    // an empty stream; the panels still work, they just refresh on demand.
    log.warn("fs watch unavailable for workspace", { detail: msg(err) });
    state.watcher = null;
  }
  watchers.set(root, state);
  ensureSweeper();
  return state;
}

// ── the route face ──────────────────────────────────────────────────────────

/**
 * Serve one `/fs/*` request. The server's auth gate has already run; this
 * enforces the stator routes' principal requirement, resolves the workspace
 * root, and answers with the desktop-identical shapes. Never throws.
 */
export function handleFsRequest(
  authn: FsAuthContext,
  req: http.IncomingMessage,
  res: http.ServerResponse,
  method: string,
  path: string,
  env: NodeJS.ProcessEnv = process.env,
): void {
  void (async (): Promise<void> => {
    // The stator routes' rule, verbatim: workspaces are owner-scoped; a caller
    // that resolved to no principal cannot be scoped. (Auth OFF = the local
    // pod — the machine's own wb serves files there, not this API.)
    if (authn.enabled && !authn.principal) {
      sendJson(res, 403, { error: "no-principal", detail: "the fs API needs introspection auth — the token's org/user scopes every workspace" });
      return;
    }
    if (!authn.enabled) {
      sendJson(res, 403, { error: "no-principal", detail: "the fs API is only served on an authenticated pod (the local machine's own workbench reads its files directly)" });
      return;
    }

    let params: Record<string, unknown>;
    try {
      params = await paramsOf(req, method);
    } catch (err) {
      sendJson(res, 400, { error: "bad-request", detail: /JSON/i.test(msg(err)) ? "body must be JSON" : msg(err) });
      return;
    }

    const rootOr = resolveRoot(authn, env, params.scope);
    if ("error" in rootOr) {
      sendJson(res, rootOr.status, { error: rootOr.error, detail: rootOr.detail });
      return;
    }
    const root = normalize(rootOr.root);
    // The workspace may predate its first run — the panels must still open on
    // an honest empty tree, and the watcher needs a directory to watch.
    try { await mkdir(root, { recursive: true }); } catch { /* fs error surfaces per-op below */ }

    const deny = { escape: "path is outside this session's workspace" };

    switch (`${method} ${path}`) {
      case "GET /fs/info":
      case "POST /fs/info": {
        const info = await gitInfo(root);
        sendJson(res, 200, { root, ...info });
        return;
      }
      case "GET /fs/list":
      case "POST /fs/list": {
        const dirRaw = params.dir ?? root;
        const dir = await containPath(root, dirRaw);
        if (!dir) { sendJson(res, 200, { entries: [], error: deny.escape }); return; }
        sendJson(res, 200, await listDir(dir));
        return;
      }
      case "GET /fs/read":
      case "POST /fs/read": {
        const p = await containPath(root, params.path);
        if (!p) { sendJson(res, 200, { content: "", error: deny.escape }); return; }
        sendJson(res, 200, await readWorkdirFile(p));
        return;
      }
      case "POST /fs/write": {
        const p = await containPath(root, params.path);
        if (!p) { sendJson(res, 200, { ok: false, error: deny.escape }); return; }
        const content = params.content;
        if (typeof content !== "string") { sendJson(res, 400, { error: "bad-write", detail: "`content` (string) is required" }); return; }
        if (Buffer.byteLength(content, "utf8") > WRITE_MAX_BYTES) {
          sendJson(res, 200, { ok: false, error: `file too large to save (>${WRITE_MAX_BYTES / 1_000_000}MB)` });
          return;
        }
        sendJson(res, 200, await writeWorkdirFile(p, content));
        return;
      }
      case "GET /fs/diff":
      case "POST /fs/diff": {
        sendJson(res, 200, await gitDiff(root));
        return;
      }
      case "GET /fs/search":
      case "POST /fs/search": {
        sendJson(res, 200, await searchFiles(root, params.query));
        return;
      }
      case "GET /fs/events": {
        const state = ensureWatch(root);
        const from = Number(params.from ?? -1);
        // A cursor-less first poll just establishes the cursor — no backlog
        // flood, the panels already painted current state from a fresh read.
        const events = Number.isFinite(from) && from >= 0 ? state.ring.filter((e) => e.seq > from) : [];
        sendJson(res, 200, { seq: state.seq, root, events });
        return;
      }
      default:
        sendJson(res, 404, { error: "not-found", detail: `no route for ${method} ${path}` });
    }
  })().catch((err: unknown) => {
    log.error("fs route failed", { path, detail: msg(err) });
    try { sendJson(res, 500, { error: "fs-error" }); } catch { /* socket gone */ }
  });
}

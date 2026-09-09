/**
 * harness/config.ts — run configuration + the agent subprocess environment.
 *
 * Two invariants live here, both ported from the desktop harness
 * (`app/src/main/glyphh-agent.ts` sdkRun env block) and both load-bearing:
 *
 * 1. CONFIG-DIR ISOLATION. The Claude Agent SDK subprocess gets its OWN
 *    `CLAUDE_CONFIG_DIR` inside the pod's harness home — NEVER a personal
 *    `~/.claude`. Without it the subprocess reads whatever Claude Code login
 *    exists in the container's home and presents that instead of our
 *    credential — the gateway 401s it, the CLI treats 401 as retryable, and
 *    the run spins forever. It is also the governance line: a turn must never
 *    ride a personal Anthropic subscription instead of the org's metered path.
 *
 * 2. GATEWAY-ONLY MODEL ROUTING. `ANTHROPIC_BASE_URL` points at the Glyphh
 *    control-plane gateway and nowhere else; `ANTHROPIC_AUTH_TOKEN` carries
 *    the SESSION'S RUNTIME TOKEN — the Anthropic client sends AUTH_TOKEN as
 *    `Authorization: Bearer`, which is the ONLY credential channel the
 *    gateway's bearer() middleware reads (API_KEY would ride the `x-api-key`
 *    header and 401 "missing bearer token"). The gateway resolves the real
 *    provider key server-side — no provider credential ever exists in the
 *    pod. `ANTHROPIC_CUSTOM_HEADERS` tags every model call
 *    `x-glyphh-run: <runId>` so metering attributes usage to this run.
 *
 * Secrets (the runtime token) are never logged — `redactSecrets` scrubs any
 * diagnostic string that could embed one.
 */

import { mkdirSync, realpathSync, statSync } from "node:fs";
import { isAbsolute } from "node:path";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { ActionKind, AskQuestion } from "./frames.js";

/** The permission modes, verbatim from the desktop (glyphh-agent-permissions):
 *    ask         — every mutating action needs approval; reads are free
 *    plan        — read-only: the agent plans, nothing changes
 *    acceptEdits — file edits auto-approved; commands still ask
 *    auto        — full auto: everything runs without asking, dangerous included
 *
 *  "bypass" is retired (2026-08-24) — auto IS full auto now. Old clients still
 *  sending it are normalized to auto at both parse seams.
 */
export type PermissionMode = "ask" | "plan" | "acceptEdits" | "auto";
const MODES: PermissionMode[] = ["ask", "plan", "acceptEdits", "auto"];

/** True when `v` is a valid permission mode — the shared validator (config +
 *  the mid-run POST /runs/:id/permission route both gate on it). Accepts the
 *  retired "bypass" for old clients; pair with normalizePermissionMode. */
export function isPermissionMode(v: unknown): v is PermissionMode {
  return typeof v === "string" && ((MODES as string[]).includes(v) || v === "bypass");
}

/** The one seam that maps the retired "bypass" onto auto. */
export function normalizePermissionMode(v: string): PermissionMode {
  const m = v === "bypass" ? "auto" : v;
  return (MODES as string[]).includes(m) ? (m as PermissionMode) : "auto";
}

/** Session modes, as the desktop's session records carry them. `chat` runs
 *  tool-less; `cowork`/`code` get the sandbox toolset. */
export type SessionMode = "chat" | "cowork" | "code";
const SESSION_MODES: SessionMode[] = ["chat", "cowork", "code"];

export interface AttachmentRef {
  name: string;
  /** Server-signed URL — treated as opaque; fetched once at run start. */
  url: string;
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

/** One caller-lent HTTP MCP server (streamable-http). This is how the DESKTOP
 *  lends a LOCAL pod its full tool surface — its loopback MCP server exposes
 *  connectors, update_app, and machine tools while the loop runs in the pod.
 *  Plaintext http is loopback-only ({@link parseMcpServers}); a cloud pod is
 *  never pointed at an arbitrary plaintext url. */
export interface McpServerRef {
  name: string;
  url: string;
  headers?: Record<string, string>;
}

/** One inline image for THIS turn — base64, ridden to the model as a vision
 *  content block ({@link parseImages}). The body's `prompt` is text, so this
 *  is the only path a pasted screenshot reaches the model. */
export interface ImageRef {
  mediaType: string;
  /** Base64 payload (no `data:` prefix). */
  data: string;
}

/** Everything one harness run needs. Built by {@link resolveRunConfig} from
 *  env + request body (body wins where both speak). */
export interface HarnessRunConfig {
  runId: string;
  sessionId: string;
  /** The CLIENT's thread id for transcript persistence (clients mint `c<ts36>`
   *  chat ids; the provisioned session is `sess_*`). Absent → the recorder
   *  falls back to sessionId. Auth is untouched by it: the runtime token
   *  stays bound to `sessionId`. */
  threadId?: string;
  /** Caller-lent HTTP MCP servers, wired into the SDK loop alongside the
   *  sandbox toolset + ask_user (cowork/code only; chat stays tool-less). */
  mcpServers?: McpServerRef[];
  /** Inline images for THIS turn — carried to the model as vision blocks. */
  images?: ImageRef[];
  prompt: string;
  history: ChatTurn[];
  /** The session's GitHub repo (`owner/repo`) — the cloud workspace's source.
   *  The engine tells the agent and instructs clone-if-absent. */
  repo?: string;
  /** Short-lived clone credential for {@link repo}, minted server-side (the
   *  front door). ENGINE-ONLY: used for the pre-run clone, scrubbed from the
   *  remote afterwards, NEVER logged, never in the transcript. */
  repoToken?: string;
  system?: string;
  model?: string;
  /** The session's LOOP — the program that shapes this turn (program-per-turn,
   *  see server/docs/program-per-turn.md). P0: carried + stamped for
   *  attribution; the envelope hooks read it from P1 on. Absent = the org's
   *  default loop semantics (today's behavior exactly). */
  loop?: string;
  /** The loop's PROGRAM SOURCE, caller-supplied (P3, desktop parity). A LOCAL
   *  auth-off pod has no principal and no org store to read `loops` from, so
   *  the DESKTOP fetches the program via the SDK and hands it over — exactly
   *  the workdir pattern. NEVER accepted on an authed (cloud) pod: there the
   *  org store is the only source of program truth. */
  loopProgram?: string;
  mode: SessionMode;
  permission: PermissionMode;
  /** The Glyphh gateway's Anthropic-compatible base URL. REQUIRED. */
  gatewayUrl: string;
  /** The control plane's origin, for the app tool surface the pod lends itself
   *  (see glyphh-apps.ts). Optional: it is derived from {@link gatewayUrl} when
   *  absent, which is the case for every caller today. */
  controlUrl?: string;
  /** The session's runtime token — the pod's ONLY credential. REQUIRED. */
  runtimeToken: string;
  /** The run's cwd. Normally the per-session sandbox dir INSIDE the pod; on a
   *  LOCAL pod (auth off) the caller may name its own folder — see
   *  {@link resolveRunConfig}'s `allowWorkdir`. */
  workdir: string;
  /** Where attachments materialize. Always the POD's sandbox — never a
   *  caller-named workdir, so a run cannot litter the user's own folder. */
  attachDir: string;
  /** The subprocess's isolated config home (inside the harness home). */
  configDir: string;
  attachments: AttachmentRef[];
  /** Per-attachment size cap, bytes. */
  attachmentMaxBytes: number;
  /** Hard cap on agent turns (0 = SDK default). */
  maxTurns: number;
  /** FULL-CONTEXT sizing (see harness/transcript.ts): the transcript token
   *  budget before compaction
   *  and how many recent turns always stay verbatim. Absent fields take the
   *  engine defaults (transcript.CONTEXT_DEFAULTS). Body `context` overrides
   *  env (HARNESS_CONTEXT_BUDGET_TOKENS / HARNESS_CONTEXT_KEEP_TURNS). */
  context?: { budgetTokens?: number; keepTurns?: number };
  /** ENGINE-INTERNAL (never body-parsed): the pre-run clone's outcome note,
   *  appended to the WORKSPACE system line so the agent knows the repo state
   *  without a token ever entering the transcript. */
  repoNote?: string;
  /** ENGINE-INTERNAL (never body-parsed): the pre-rendered full-conversation
   *  block. When present, {@link import("./engine.js").assemblePrompt} carries
   *  it as the COMPLETE `<conversation_so_far>` instead of rendering (and
   *  slicing) `history` — the full-context fallback's delivery seam. */
  contextBlock?: string;
}

/** The pod's harness home: sandboxes + config dirs live under it. Writable by
 *  the pod user; defaults under tmp so a bare container needs no setup. */
export function harnessHome(env: NodeJS.ProcessEnv = process.env): string {
  return env.HARNESS_HOME ?? join(tmpdir(), "glyphh-harness");
}

/**
 * The ONE workspace-keying rule, shared by run config (below) and the fs API
 * (fs-routes.ts) so the Files/Diff panels browse EXACTLY where runs execute:
 *
 *   · a BOUND session (dedicated pod / provisioned `sess_*`) → the session id,
 *     unchanged — one session, one workspace.
 *   · a SHARED pod turn (front-door: user token, no session binding) → the
 *     OWNER'S user id + the client thread id, so a chat's turns share one
 *     stable workspace AND no org member can reach another's by guessing a
 *     thread id (thread ids are client-minted `c<ts36>` — guessable).
 *   · neither → the run id (an unattributed one-off stays isolated per run).
 */
export function workspaceSegment(key: { sessionId?: string; owner?: string; threadId?: string; runId: string }): string {
  if (key.sessionId) return sanitizeSegment(key.sessionId);
  if (key.owner && key.threadId) return `${sanitizeSegment(key.owner)}~${sanitizeSegment(key.threadId)}`;
  return sanitizeSegment(key.runId);
}

/** A session workspace root under the harness home, by its keyed segment. */
export function sessionWorkspace(env: NodeJS.ProcessEnv, segment: string): string {
  return join(harnessHome(env), "sessions", segment, "workspace");
}

/** The POST /run body shape (all optional except `prompt`). */
export interface RunRequestBody {
  prompt?: unknown;
  sessionId?: unknown;
  threadId?: unknown;
  repo?: unknown;
  repoToken?: unknown;
  mcpServers?: unknown;
  images?: unknown;
  workdir?: unknown;
  history?: unknown;
  system?: unknown;
  model?: unknown;
  loop?: unknown;
  loopProgram?: unknown;
  mode?: unknown;
  permission?: unknown;
  attachments?: unknown;
  gatewayUrl?: unknown;
  controlUrl?: unknown;
  runtimeToken?: unknown;
  maxTurns?: unknown;
  context?: unknown;
}

/** A validation failure the HTTP layer maps to 400. */
export class BadRunRequest extends Error {}

/** What a client-minted thread id may look like (`c<ts36>` and friends). */
const THREAD_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

const MCP_NAME_RE = /^[a-z0-9_-]{1,32}$/;
const MCP_SERVER_CAP = 4;

/**
 * Validate the caller-lent MCP servers (max {@link MCP_SERVER_CAP}). Names are
 * short slugs (and never `glyphh` — that name is the pod's own ask_user
 * server); urls must parse as http(s), and plaintext http is allowed ONLY for
 * loopback hosts (127.0.0.1 / localhost) — the desktop's loopback MCP is the
 * whole point, but a cloud pod must never be pointed at an arbitrary
 * plaintext url. Throws {@link BadRunRequest} on any violation.
 */
const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const IMAGE_CAP = 8;
/** Per-image base64 ceiling (~7.5 MiB decoded) — a pasted screenshot fits far
 *  under it; anything larger is a mistake, not a turn. */
const IMAGE_B64_MAX = 10_000_000;

/** Validate inline turn images: known media types, plausible base64, capped
 *  count and size. Throws {@link BadRunRequest} on any violation. */
export function parseImages(raw: unknown): ImageRef[] {
  if (raw === undefined) return [];
  if (!Array.isArray(raw)) throw new BadRunRequest("`images` must be an array");
  if (raw.length > IMAGE_CAP) throw new BadRunRequest(`\`images\` allows at most ${IMAGE_CAP} images`);
  return raw.map((i) => {
    const mediaType = (i as { mediaType?: unknown })?.mediaType;
    const data = (i as { data?: unknown })?.data;
    if (typeof mediaType !== "string" || !IMAGE_TYPES.includes(mediaType)) {
      throw new BadRunRequest(`\`images[].mediaType\` must be one of ${IMAGE_TYPES.join(", ")}`);
    }
    if (typeof data !== "string" || !data || !/^[A-Za-z0-9+/\r\n]+={0,2}$/.test(data)) {
      throw new BadRunRequest("`images[].data` must be base64 (no `data:` prefix)");
    }
    if (data.length > IMAGE_B64_MAX) throw new BadRunRequest("`images[].data` exceeds the per-image size cap");
    return { mediaType, data };
  });
}

/**
 * Validate a caller-named working directory. HONORED ONLY IN LOCAL MODE
 * (`allowWorkdir`, i.e. auth OFF — the loopback-bound pod on the user's own
 * machine, where naming a host path is the entire point). A cloud/auth-ON pod
 * REJECTS it: a shared tenant pod must never let a caller point the sandbox
 * at a host path. Must be absolute, exist, and be a directory; symlinks and
 * `..` are resolved (realpath) so the value used downstream is canonical.
 */
export function resolveWorkdir(raw: unknown, allowWorkdir: boolean): string | undefined {
  if (raw === undefined) return undefined;
  const given = typeof raw === "string" ? raw.trim() : "";
  if (!allowWorkdir) {
    throw new BadRunRequest("`workdir` is only honored on a local pod (auth disabled); this pod runs the sandbox workspace");
  }
  if (!given || !isAbsolute(given)) throw new BadRunRequest("`workdir` must be an absolute path");
  let real: string;
  try {
    real = realpathSync(given);
  } catch {
    throw new BadRunRequest(`\`workdir\` does not exist: ${given}`);
  }
  if (!statSync(real).isDirectory()) throw new BadRunRequest(`\`workdir\` is not a directory: ${given}`);
  return real;
}

export function parseMcpServers(raw: unknown): McpServerRef[] {
  if (raw === undefined) return [];
  if (!Array.isArray(raw)) throw new BadRunRequest("`mcpServers` must be an array");
  if (raw.length > MCP_SERVER_CAP) throw new BadRunRequest(`\`mcpServers\` allows at most ${MCP_SERVER_CAP} servers`);
  const refs: McpServerRef[] = [];
  for (const s of raw) {
    const name = (s as { name?: unknown })?.name;
    const url = (s as { url?: unknown })?.url;
    if (typeof name !== "string" || !MCP_NAME_RE.test(name) || name === "glyphh") {
      throw new BadRunRequest("`mcpServers[].name` must match ^[a-z0-9_-]{1,32}$ (and not be `glyphh`)");
    }
    let parsed: URL;
    try {
      parsed = new URL(String((url as string) ?? ""));
    } catch {
      throw new BadRunRequest("`mcpServers[].url` must be a valid http(s) URL");
    }
    const loopback = parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost";
    if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && loopback)) {
      throw new BadRunRequest("`mcpServers[].url` must be https, or http on a loopback host (127.0.0.1/localhost)");
    }
    const headersRaw = (s as { headers?: unknown })?.headers;
    const headers: Record<string, string> = {};
    if (headersRaw && typeof headersRaw === "object") {
      for (const [k, v] of Object.entries(headersRaw as Record<string, unknown>)) {
        if (typeof v === "string") headers[k] = v;
      }
    }
    refs.push({ name, url: parsed.toString(), ...(Object.keys(headers).length ? { headers } : {}) });
  }
  return refs;
}

/**
 * Merge env + body into a run config. Body overrides env for gateway/model/
 * session so the control plane can provision generic pods and bind at /run
 * time; env alone also works (per-session pods provisioned pre-bound).
 * Throws {@link BadRunRequest} on a missing prompt or missing gateway config.
 *
 * `allowWorkdir` is the LOCAL-MODE switch (the server passes `!auth.enabled`):
 * only a loopback-bound, auth-off pod on the user's own machine may honor a
 * caller-named `workdir`. See {@link resolveWorkdir}.
 */
export function resolveRunConfig(
  runId: string,
  body: RunRequestBody,
  env: NodeJS.ProcessEnv = process.env,
  opts: { allowWorkdir?: boolean; owner?: string } = {},
): HarnessRunConfig {
  const prompt = typeof body.prompt === "string" ? body.prompt.trim() : "";
  if (!prompt) throw new BadRunRequest("`prompt` is required");

  const gatewayUrl = str(body.gatewayUrl) ?? env.GLYPHH_GATEWAY_URL ?? "";
  if (!gatewayUrl) throw new BadRunRequest("no gateway configured: set GLYPHH_GATEWAY_URL or pass `gatewayUrl`");
  const runtimeToken = str(body.runtimeToken) ?? env.GLYPHH_RUNTIME_TOKEN ?? "";
  if (!runtimeToken) throw new BadRunRequest("no runtime token: set GLYPHH_RUNTIME_TOKEN or pass `runtimeToken`");

  const sessionId = str(body.sessionId) ?? env.ROTOR_SESSION_ID ?? "";
  // The client's thread id, when it differs from the provisioned session id
  // (auth binds the token to sessionId; the transcript belongs to threadId).
  let threadId: string | undefined;
  if (body.threadId !== undefined) {
    const t = typeof body.threadId === "string" ? body.threadId.trim() : "";
    if (!THREAD_ID_RE.test(t)) throw new BadRunRequest("`threadId` must match ^[A-Za-z0-9_-]{1,64}$");
    threadId = t;
  }
  // The session's GitHub repo — the cloud workspace's source. Validated to the
  // one shape a clone URL is built from; anything else is refused, not passed
  // to a shell.
  let repo: string | undefined;
  if (body.repo !== undefined) {
    const r = typeof body.repo === "string" ? body.repo.trim() : "";
    if (r && !/^[\w.-]+\/[\w.-]+$/.test(r)) throw new BadRunRequest("`repo` must be owner/repo");
    if (r) repo = r;
  }
  const modeRaw = str(body.mode) ?? "code";
  const permissionRaw = str(body.permission) ?? env.HARNESS_PERMISSION_MODE ?? "auto";
  const home = harnessHome(env);
  // One sandbox + one config home PER SESSION (a session's runs share their
  // files; sessions never see each other's). On a SHARED pod (no bound session)
  // the workspace keys by OWNER + THREAD so a chat's turns share one stable
  // workspace (workspaceSegment) — previously each front-door run got a fresh
  // per-run sandbox, so nothing persisted across turns and nothing was
  // browsable. A blank session id AND no owner/thread still isolates per run.
  const segment = workspaceSegment({
    ...(sessionId ? { sessionId } : {}),
    ...(opts.owner ? { owner: opts.owner } : {}),
    ...(threadId ? { threadId } : {}),
    runId,
  });
  const sandbox = sessionWorkspace(env, segment);
  const configDir = join(home, "sessions", segment, "agent-config");
  // LOCAL MODE: the caller's own folder becomes the run's cwd, so the SDK's
  // preset tools (Bash/Read/Edit/Glob/Grep) act on the user's real files
  // instead of an empty pod sandbox. Attachments still land in the POD's
  // sandbox — a run never litters the user's folder.
  const named = resolveWorkdir(body.workdir, opts.allowWorkdir === true);

  const mcpServers = parseMcpServers(body.mcpServers);
  const images = parseImages(body.images);

  return {
    runId,
    sessionId,
    ...(threadId ? { threadId } : {}),
    ...(repo ? { repo } : {}),
    ...(repo && typeof body.repoToken === "string" && body.repoToken.trim() ? { repoToken: body.repoToken.trim() } : {}),
    ...(mcpServers.length ? { mcpServers } : {}),
    ...(images.length ? { images } : {}),
    prompt,
    history: parseHistory(body.history),
    ...(str(body.system) ? { system: str(body.system) } : {}),
    ...(str(body.model) ?? env.HARNESS_MODEL ? { model: str(body.model) ?? env.HARNESS_MODEL } : {}),
    // Loop refs are slugs/ids — same shape law as thread ids; anything else is
    // dropped rather than 400'd (the field is advisory until the envelope).
    ...(str(body.loop) && THREAD_ID_RE.test(str(body.loop)!) ? { loop: str(body.loop)! } : {}),
    // The program source rides ONLY on a local auth-off pod (the workdir law):
    // an authed cloud pod reads programs from the org store, never the wire —
    // a caller-supplied program there would bypass org program truth. 256KB cap.
    ...(opts.allowWorkdir === true && typeof body.loopProgram === "string" && body.loopProgram.trim() && body.loopProgram.length <= 256 * 1024
      ? { loopProgram: body.loopProgram }
      : {}),
    mode: SESSION_MODES.includes(modeRaw as SessionMode) ? (modeRaw as SessionMode) : "code",
    permission: normalizePermissionMode(String(permissionRaw ?? "")),
    gatewayUrl: gatewayUrl.replace(/\/+$/, ""),
    ...(str(body.controlUrl) ?? env.GLYPHH_CONTROL_URL
      ? { controlUrl: (str(body.controlUrl) ?? env.GLYPHH_CONTROL_URL ?? "").replace(/\/+$/, "") }
      : {}),
    runtimeToken,
    workdir: named ?? sandbox,
    attachDir: sandbox,
    configDir,
    attachments: parseAttachments(body.attachments),
    attachmentMaxBytes: intEnv(env.HARNESS_ATTACH_MAX_MB, 50) * 1024 * 1024,
    maxTurns: num(body.maxTurns) ?? intEnv(env.HARNESS_MAX_TURNS, 0),
    ...(parseContext(body.context, env) ? { context: parseContext(body.context, env) } : {}),
  };
}

/** Parse the full-context fallback sizing: body `context` wins over env; absent
 *  everywhere ⇒ undefined (the engine applies transcript.CONTEXT_DEFAULTS). */
function parseContext(v: unknown, env: NodeJS.ProcessEnv): { budgetTokens?: number; keepTurns?: number } | undefined {
  const o = v != null && typeof v === "object" ? (v as Record<string, unknown>) : {};
  const budgetTokens = num(o.budgetTokens) ?? optIntEnv(env.HARNESS_CONTEXT_BUDGET_TOKENS);
  const keepTurns = num(o.keepTurns) ?? optIntEnv(env.HARNESS_CONTEXT_KEEP_TURNS);
  const p = {
    ...(budgetTokens !== undefined ? { budgetTokens } : {}),
    ...(keepTurns !== undefined ? { keepTurns } : {}),
  };
  return Object.keys(p).length ? p : undefined;
}

function optIntEnv(raw: string | undefined): number | undefined {
  if (raw === undefined || raw === "") return undefined;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 ? Math.floor(n) : undefined;
}

function str(v: unknown): string | undefined {
  return typeof v === "string" && v.trim() ? v.trim() : undefined;
}
function num(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) && v >= 0 ? Math.floor(v) : undefined;
}
function intEnv(raw: string | undefined, fallback: number): number {
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 ? Math.floor(n) : fallback;
}

/** A path segment safe for the sandbox tree: no separators, no dot-tricks. */
export function sanitizeSegment(name: string): string {
  const clean = name
    .replace(/[^a-zA-Z0-9._-]/g, "_")
    .replace(/\.{2,}/g, "_")
    .replace(/^\.+/, "_");
  return clean || "_";
}

function parseHistory(raw: unknown): ChatTurn[] {
  if (!Array.isArray(raw)) return [];
  const turns: ChatTurn[] = [];
  for (const t of raw) {
    const role = (t as { role?: unknown })?.role;
    const content = (t as { content?: unknown })?.content;
    if ((role === "user" || role === "assistant") && typeof content === "string") turns.push({ role, content });
  }
  return turns;
}

function parseAttachments(raw: unknown): AttachmentRef[] {
  if (!Array.isArray(raw)) return [];
  const refs: AttachmentRef[] = [];
  for (const a of raw) {
    const name = (a as { name?: unknown })?.name;
    const url = (a as { url?: unknown })?.url;
    if (typeof name === "string" && name && typeof url === "string" && /^https?:\/\//i.test(url)) {
      refs.push({ name, url });
    }
  }
  return refs;
}

// ── the subprocess environment ─────────────────────────────────────────────

/**
 * Build the Claude Agent SDK subprocess env for one run — the exact desktop
 * pattern (glyphh-agent.ts), minus Electron:
 *
 *   ANTHROPIC_BASE_URL        → the Glyphh gateway (the ONLY model egress)
 *   ANTHROPIC_CUSTOM_HEADERS  → `x-glyphh-run: <runId>` (metering attribution)
 *   ANTHROPIC_AUTH_TOKEN      → the session's runtime token, sent as
 *                               `Authorization: Bearer` — the only header the
 *                               gateway reads (the real provider key never
 *                               reaches the pod)
 *   ANTHROPIC_API_KEY         → cleared (it would ride `x-api-key`, which the
 *                               gateway ignores — must not shadow the bearer)
 *   CLAUDE_CONFIG_DIR         → the run's isolated config home (see module doc)
 *   CLAUDE_CODE_USE_BEDROCK/VERTEX → cleared (gateway only, no cloud bypass)
 *   telemetry/nonessential traffic → off
 *
 * Base env is `process.env` minus every ANTHROPIC_ and CLAUDE_ key, so nothing
 * ambient in the container can leak a different endpoint or credential in.
 */
export function buildAgentEnv(
  cfg: Pick<HarnessRunConfig, "runId" | "gatewayUrl" | "runtimeToken" | "configDir">,
  base: NodeJS.ProcessEnv = process.env,
): Record<string, string | undefined> {
  const env: Record<string, string | undefined> = {};
  for (const [k, v] of Object.entries(base)) {
    if (/^(ANTHROPIC_|CLAUDE_)/.test(k)) continue;
    env[k] = v;
  }
  mkdirSync(cfg.configDir, { recursive: true });
  return {
    ...env,
    ANTHROPIC_BASE_URL: cfg.gatewayUrl,
    ANTHROPIC_CUSTOM_HEADERS: `x-glyphh-run: ${cfg.runId}`,
    ANTHROPIC_AUTH_TOKEN: cfg.runtimeToken,
    ANTHROPIC_API_KEY: undefined,
    // LONG CONTEXT, both sides of the wire: without the 1m beta the SDK's OWN
    // context accounting caps big-window models at 200K and refuses the call
    // client-side ("Prompt is too long" with no request ever reaching the
    // gateway — prod, 2026-09-09). The gateway STRIPS this beta for models
    // whose catalog window is ≤200K, so always-on here is safe everywhere.
    ANTHROPIC_BETAS: "context-1m-2025-08-07",
    CLAUDE_CONFIG_DIR: cfg.configDir,
    CLAUDE_CODE_USE_BEDROCK: undefined,
    CLAUDE_CODE_USE_VERTEX: undefined,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    DISABLE_TELEMETRY: "1",
  };
}

/** Scrub secrets from a diagnostic string before it can reach a log line or a
 *  frame. Every configured secret is replaced wholesale. */
export function redactSecrets(text: string, secrets: Array<string | undefined>): string {
  let out = text;
  for (const s of secrets) {
    if (s && s.length >= 6) out = out.split(s).join("•••");
  }
  return out;
}

/** Re-voice hosted-harness errors as Glyphh's own (desktop `brandError`): the
 *  subprocess speaks as "Claude Code" and hints at CLI flags Glyphh users
 *  don't have. Model ids are data, not branding — they pass through. */
export function brandError(message: string): string {
  return message
    .replaceAll("Claude Code", "Glyphh")
    .replace(/Run --model to pick a different model\.?/g, "Pick a different model.")
    .replace(/Run \/login[^.]*\./g, "Sign in again.");
}

// Re-export the taxonomy the gate shares with the frames module.
export type { ActionKind, AskQuestion };

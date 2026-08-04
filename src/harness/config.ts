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
 *    control-plane gateway and nowhere else; `ANTHROPIC_API_KEY` carries the
 *    SESSION'S RUNTIME TOKEN (the gateway resolves the real provider key
 *    server-side — no provider credential ever exists in the pod); and
 *    `ANTHROPIC_CUSTOM_HEADERS` tags every model call `x-glyphh-run: <runId>`
 *    so metering attributes usage to this run.
 *
 * Secrets (the runtime token) are never logged — `redactSecrets` scrubs any
 * diagnostic string that could embed one.
 */

import { mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { ActionKind, AskQuestion } from "./frames.js";

/** The permission modes, verbatim from the desktop (glyphh-agent-permissions):
 *    ask         — every mutating action needs approval; reads are free
 *    plan        — read-only: the agent plans, nothing changes
 *    acceptEdits — file edits auto-approved; commands still ask
 *    auto        — workspace actions auto-approved; dangerous ones still ask
 *    bypass      — everything auto-approved (trusted sessions only)
 */
export type PermissionMode = "ask" | "plan" | "acceptEdits" | "auto" | "bypass";
const MODES: PermissionMode[] = ["ask", "plan", "acceptEdits", "auto", "bypass"];

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

/** Everything one harness run needs. Built by {@link resolveRunConfig} from
 *  env + request body (body wins where both speak). */
export interface HarnessRunConfig {
  runId: string;
  sessionId: string;
  prompt: string;
  history: ChatTurn[];
  system?: string;
  model?: string;
  mode: SessionMode;
  permission: PermissionMode;
  /** The Glyphh gateway's Anthropic-compatible base URL. REQUIRED. */
  gatewayUrl: string;
  /** The session's runtime token — the pod's ONLY credential. REQUIRED. */
  runtimeToken: string;
  /** Per-session sandbox dir INSIDE the pod — the run's cwd. */
  workdir: string;
  /** The subprocess's isolated config home (inside the harness home). */
  configDir: string;
  attachments: AttachmentRef[];
  /** Per-attachment size cap, bytes. */
  attachmentMaxBytes: number;
  /** Hard cap on agent turns (0 = SDK default). */
  maxTurns: number;
}

/** The pod's harness home: sandboxes + config dirs live under it. Writable by
 *  the pod user; defaults under tmp so a bare container needs no setup. */
export function harnessHome(env: NodeJS.ProcessEnv = process.env): string {
  return env.HARNESS_HOME ?? join(tmpdir(), "glyphh-harness");
}

/** The POST /run body shape (all optional except `prompt`). */
export interface RunRequestBody {
  prompt?: unknown;
  sessionId?: unknown;
  history?: unknown;
  system?: unknown;
  model?: unknown;
  mode?: unknown;
  permission?: unknown;
  attachments?: unknown;
  gatewayUrl?: unknown;
  runtimeToken?: unknown;
  maxTurns?: unknown;
}

/** A validation failure the HTTP layer maps to 400. */
export class BadRunRequest extends Error {}

/**
 * Merge env + body into a run config. Body overrides env for gateway/model/
 * session so the control plane can provision generic pods and bind at /run
 * time; env alone also works (per-session pods provisioned pre-bound).
 * Throws {@link BadRunRequest} on a missing prompt or missing gateway config.
 */
export function resolveRunConfig(
  runId: string,
  body: RunRequestBody,
  env: NodeJS.ProcessEnv = process.env,
): HarnessRunConfig {
  const prompt = typeof body.prompt === "string" ? body.prompt.trim() : "";
  if (!prompt) throw new BadRunRequest("`prompt` is required");

  const gatewayUrl = str(body.gatewayUrl) ?? env.GLYPHH_GATEWAY_URL ?? "";
  if (!gatewayUrl) throw new BadRunRequest("no gateway configured: set GLYPHH_GATEWAY_URL or pass `gatewayUrl`");
  const runtimeToken = str(body.runtimeToken) ?? env.GLYPHH_RUNTIME_TOKEN ?? "";
  if (!runtimeToken) throw new BadRunRequest("no runtime token: set GLYPHH_RUNTIME_TOKEN or pass `runtimeToken`");

  const sessionId = str(body.sessionId) ?? env.ROTOR_SESSION_ID ?? "";
  const modeRaw = str(body.mode) ?? "code";
  const permissionRaw = str(body.permission) ?? env.HARNESS_PERMISSION_MODE ?? "auto";
  const home = harnessHome(env);
  // One sandbox + one config home PER SESSION (a session's runs share their
  // files; sessions never see each other's). A blank session id still gets an
  // isolated area keyed by the run.
  const scope = sessionId || runId;
  const workdir = join(home, "sessions", sanitizeSegment(scope), "workspace");
  const configDir = join(home, "sessions", sanitizeSegment(scope), "agent-config");

  return {
    runId,
    sessionId,
    prompt,
    history: parseHistory(body.history),
    ...(str(body.system) ? { system: str(body.system) } : {}),
    ...(str(body.model) ?? env.HARNESS_MODEL ? { model: str(body.model) ?? env.HARNESS_MODEL } : {}),
    mode: SESSION_MODES.includes(modeRaw as SessionMode) ? (modeRaw as SessionMode) : "code",
    permission: MODES.includes(permissionRaw as PermissionMode) ? (permissionRaw as PermissionMode) : "auto",
    gatewayUrl: gatewayUrl.replace(/\/+$/, ""),
    runtimeToken,
    workdir,
    configDir,
    attachments: parseAttachments(body.attachments),
    attachmentMaxBytes: intEnv(env.HARNESS_ATTACH_MAX_MB, 50) * 1024 * 1024,
    maxTurns: num(body.maxTurns) ?? intEnv(env.HARNESS_MAX_TURNS, 0),
  };
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
 *   ANTHROPIC_API_KEY         → the session's runtime token (gateway auth;
 *                               the real provider key never reaches the pod)
 *   ANTHROPIC_AUTH_TOKEN      → cleared (must not shadow the key)
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
    ANTHROPIC_API_KEY: cfg.runtimeToken,
    ANTHROPIC_AUTH_TOKEN: undefined,
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

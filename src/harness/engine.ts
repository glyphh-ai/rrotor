/**
 * harness/engine.ts — the hosted Claude Agent SDK session drive.
 *
 * A headless port of the desktop's `sdkRun` (`app/src/main/glyphh-agent.ts`):
 * the SDK's `query()` runs the agent loop in a subprocess whose every model
 * call rides the Glyphh gateway (see config.ts for the env invariants), and
 * this module translates the SDK's message stream into the frame vocabulary
 * (frames.ts) the renderer already speaks:
 *
 *   stream_event/message_start   → bank the last turn's output, add input toks
 *   stream_event/message_delta   → live output toks → `progress` (throttled)
 *   stream_event/content_block_delta(text) → `delta`
 *   assistant tool_use blocks    → `tool` phase:start
 *   user tool_result blocks      → `tool` phase:done (failed + preview)
 *   assistant text (no partials) → whole-message `delta` fallback
 *   result                       → authoritative usage → `progress`; then
 *                                  `done` or `error`
 *
 * Tool surface v1 — CLOUD-SAFE by default: the SDK's own sandbox toolset
 * (Bash/Read/Write/Edit/Glob/Grep/…) anchored at the per-session workspace,
 * plus ONE in-process MCP tool, `ask_user`, which drives the Ask flow (an
 * `ask` frame parks the run; POST /runs/:id/answer resumes it).
 *
 * EVERY tool call is gated: `canUseTool` → `gateAction` (gate.ts) decides by
 * the run's permission mode. The SDK's `allowedTools` is deliberately NOT set
 * — it means "auto-allow without prompting", so listing the toolset there
 * made the SDK skip the gate entirely (plan mode wrote files; ask mode never
 * asked). Availability lives in `tools`; permission lives in the gate.
 *
 * No connector tools, no desktop reach, no native dialogs — UNLESS lent:
 * body `mcpServers` (validated in config.parseMcpServers, loopback-or-https)
 * are wired in as streamable-HTTP MCP servers, which is how the desktop's
 * loopback MCP hands a LOCAL pod its connectors/update_app/machine tools.
 * `query` is injectable so the translation/pause seams unit-test with the
 * SDK faked.
 */

import { query, type SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { ListToolsRequestSchema, CallToolRequestSchema } from "@modelcontextprotocol/sdk/types.js";

import { buildAgentEnv, brandError, redactSecrets } from "./config.js";
import type { HarnessRunConfig, ChatTurn, ImageRef } from "./config.js";
import { appsServerRef, withPublishPolicy, expandBuildAppInput, expandSaveFileInput, BUILD_APP_TOOL, SAVE_FILE_TOOL } from "./glyphh-apps.js";
import { buildFactsServer, renderFactBlock } from "../facts/server.js";
import { pullSource, pushSource, rebaseSource, type SourceSyncCfg } from "./source-sync.js";
import { classifyTool, gateAction } from "./gate.js";
import { ensureWorkspace, materializeAttachments } from "./sandbox.js";
import type { MaterializedAttachment } from "./sandbox.js";
import { HarnessSession } from "./session.js";
import { ASK_TIMEOUT_MS } from "./gate.js";
import { toolTitle } from "./frames.js";
import type { AskQuestion } from "./frames.js";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readdir } from "node:fs/promises";
import { log } from "../obs/logger.js";
import type { Logger } from "../obs/logger.js";
import type { Principal } from "../auth/introspect.js";
import { sessionTranscripts, gatewaySummarizer, CONTEXT_DEFAULTS, TranscriptStore } from "./transcript.js";

/** The injectable SDK seam: the real `query` in production, a fake in tests. */
export type QueryFn = (args: { prompt: string | AsyncIterable<unknown>; options: Record<string, unknown> }) => AsyncIterable<unknown>;

export interface EngineDeps {
  queryFn?: QueryFn;
  fetchFn?: typeof fetch;
  /** Approval wait override (tests shorten it). */
  approvalTimeoutMs?: number;
  /** Credits-lookup timeout override (tests shorten it). */
  creditsTimeoutMs?: number;
  /** The full-context fallback's transcript store (tests isolate it);
   *  defaults to the pod-wide {@link sessionTranscripts}. */
  transcripts?: TranscriptStore;
  /** The introspected caller — when present (and the run has tools), the
   *  glyphh_facts server mounts and the fact ledger is org/user scoped to
   *  this principal. Absent (auth off) → no fact tools, deliberately. */
  principal?: Principal;
  /** THE INJECTION HALF: the fixed-shape org-fact block for this turn,
   *  rendered by runHarness (selection is deterministic prime-overlap against
   *  the last exchange — sub-ms, no model call). Rides the system prompt. */
  factBlock?: string | null;
}

/** How long the turn's price lookup may take before we give up on it. The
 *  terminal frame waits at most this long — and only when the probe has not
 *  already resolved (it is fired at `result`, so usually it has). */
const CREDITS_TIMEOUT_MS = 3000;

/**
 * The turn's METERED PRICE, from the gateway's per-run usage endpoint
 * (`GET {gatewayUrl}/runs/:runId/usage`, bearer = the run's runtime token).
 * The gateway attributes usage by the `x-glyphh-run: <runId>` header that
 * every model call already carries (config.buildAgentEnv).
 *
 * Advisory by construction: it never throws and never fails a turn. A 404
 * (unknown run), a 401, a timeout, or a dead socket all resolve `null`, which
 * means "emit no credits frame" — the turn still ends normally.
 */
async function fetchCreditsMicro(
  cfg: Pick<HarnessRunConfig, "runId" | "gatewayUrl" | "runtimeToken">,
  deps: EngineDeps,
  logger: Logger,
): Promise<number | null> {
  if (!cfg.gatewayUrl || !cfg.runtimeToken) return null;
  const fetchFn = deps.fetchFn ?? fetch;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), deps.creditsTimeoutMs ?? CREDITS_TIMEOUT_MS);
  timer.unref?.();
  try {
    const res = await fetchFn(`${cfg.gatewayUrl}/runs/${encodeURIComponent(cfg.runId)}/usage`, {
      headers: { authorization: `Bearer ${cfg.runtimeToken}` },
      signal: ctrl.signal,
    });
    if (!res.ok) {
      logger.debug("credits lookup declined", { status: res.status });
      return null;
    }
    const micro = ((await res.json()) as { data?: { creditsMicro?: unknown } })?.data?.creditsMicro;
    return typeof micro === "number" && Number.isFinite(micro) ? micro : null;
  } catch (err) {
    logger.debug("credits lookup failed", { detail: (err as Error).message });
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * The pod's AVAILABLE built-in toolset (v1 sandbox surface) — passed as the
 * SDK's `tools`, which is what governs availability. Everything else —
 * connectors, desktop panels, app deploys, Ada recall — is deliberately absent
 * until later phases.
 *
 * This list does NOT mean "allowed": nothing here is pre-approved. Whether a
 * given call may proceed is decided per call by `canUseTool` → `gateAction`
 * (gate.ts) according to the run's permission mode.
 */
export const SANDBOX_TOOLS = [
  "Bash",
  "BashOutput",
  "KillShell",
  "Read",
  "Write",
  "Edit",
  "Glob",
  "Grep",
  "NotebookEdit",
  "TodoWrite",
  "WebFetch",
  "WebSearch",
];

const DEFAULT_SYSTEM =
  "You are Glyphh, the user's agent, running in a hosted session workspace. " +
  "Work happens INSIDE your workspace directory: create files, run commands, and build there. " +
  "Files the user attached are listed with their absolute paths — read them there. " +
  "Use ask_user when a decision is genuinely the user's. Answer directly and concisely. " +
  "NARRATE YOUR WORK: before your FIRST tool call of a turn, say in one short sentence what you are about to do and why (\"Searching for current comparisons, then I'll build the table.\") — the user should never watch tools fire with no idea what the plan is. Give a brief note when you change direction or find something that changes the answer.";

/** Assemble the per-run prompt the way the desktop does (persistSession is
 *  off — each run carries its own recent history in the user turn). When the
 *  FULL-CONTEXT FALLBACK produced a `contextBlock` (rotor not active), that
 *  block IS the complete conversation — already compaction-sized, never
 *  sliced — and replaces the legacy last-40 history rendering. */
export function assemblePrompt(history: ChatTurn[], prompt: string, attachments: MaterializedAttachment[], contextBlock?: string): string {
  const parts: string[] = [];
  if (contextBlock) {
    parts.push(`<conversation_so_far>\n${contextBlock}\n</conversation_so_far>`);
  } else if (history.length) {
    const recent = history.slice(-40);
    const block = recent.map((m) => `${m.role === "user" ? "User" : "Glyphh"}: ${m.content}`).join("\n");
    parts.push(`<conversation_so_far>\n${block}\n</conversation_so_far>`);
  }
  parts.push(prompt);
  if (attachments.length) {
    parts.push(
      `<attached_files>\n${attachments.map((a) => `${a.path} (${a.bytes} bytes)`).join("\n")}\n</attached_files>`,
    );
  }
  return parts.join("\n\n");
}

/**
 * The prompt as the SDK takes it. Text-only turns pass a plain string (the
 * long-standing path, untouched). A turn carrying IMAGES must use the SDK's
 * streaming-input form — `query({prompt: AsyncIterable<SDKUserMessage>})` —
 * because only a message's `content` may hold blocks; a string prompt has
 * nowhere to put an image. One user message is yielded, then the iterable
 * ends, which is what closes the turn.
 */
export function buildPrompt(text: string, images: ImageRef[]): string | AsyncIterable<unknown> {
  if (!images.length) return text;
  return (async function* () {
    yield {
      type: "user" as const,
      parent_tool_use_id: null,
      message: {
        role: "user" as const,
        content: [
          ...images.map((img) => ({
            type: "image" as const,
            source: { type: "base64" as const, media_type: img.mediaType, data: img.data },
          })),
          { type: "text" as const, text },
        ],
      },
    };
  })();
}

/**
 * Streaming input: the opening user message, then any MID-RUN injections
 * (session.inject → POST /runs/:id/inject) — each drained one starts a fresh
 * turn. The engine closes the stream at the first turn end with an empty
 * inbox, which is what ends the query; a run with no injections behaves
 * exactly like the single-shot form.
 */
export function buildInputStream(text: string, images: ImageRef[], session: HarnessSession): AsyncIterable<unknown> {
  const userMsg = (t: string, imgs: ImageRef[]): unknown => ({
    type: "user" as const,
    parent_tool_use_id: null,
    message: {
      role: "user" as const,
      content: [
        ...imgs.map((img) => ({
          type: "image" as const,
          source: { type: "base64" as const, media_type: img.mediaType, data: img.data },
        })),
        { type: "text" as const, text: t },
      ],
    },
  });
  return (async function* () {
    yield userMsg(text, images);
    for (;;) {
      const next = await session.nextInjected();
      if (next === null) return;
      yield userMsg(next, []);
    }
  })();
}

/** The in-process MCP server carrying the pod's ONE custom tool: `ask_user`.
 *  Same wire pattern as the desktop's glyphh MCP surface. */
const execFileP = promisify(execFile);

async function readdirSafe(dir: string): Promise<string[]> {
  try { return await readdir(dir); } catch { return []; }
}

/**
 * Clone the session's repo into an EMPTY workspace — ENGINE-SIDE, before the
 * model runs, so a private repo's short-lived token never rides the
 * transcript, a tool title, or the workspace's git config (the remote is
 * scrubbed back to the tokenless URL right after). Returns a note for the
 * system prompt; failures degrade to a note the agent can act on.
 */
export async function ensureRepoClone(cfg: { repo?: string; repoToken?: string; workdir: string }): Promise<string> {
  if (!cfg.repo) return "";
  const cleanUrl = `https://github.com/${cfg.repo}`;
  try {
    const entries = await readdirSafe(cfg.workdir);
    if (entries.includes(".git")) return `The workspace already contains the repo (${cfg.repo}).`;
    if (entries.length > 0) return `The workspace has files but no git repo — clone ${cfg.repo} into a subfolder if needed.`;
    const cloneUrl = cfg.repoToken
      ? `https://x-access-token:${cfg.repoToken}@github.com/${cfg.repo}`
      : cleanUrl;
    await execFileP("git", ["clone", "--", cloneUrl, "."], { cwd: cfg.workdir, timeout: 180_000, maxBuffer: 4 * 1024 * 1024 });
    // Scrub the credential from the remote — the workspace is user-browsable.
    if (cfg.repoToken) {
      await execFileP("git", ["remote", "set-url", "origin", cleanUrl], { cwd: cfg.workdir, timeout: 10_000 }).catch(() => { /* best-effort scrub */ });
    }
    return `The repo ${cfg.repo} is already cloned into the workspace root — do not clone again.`;
  } catch (err) {
    const raw = (err as Error).message || String(err);
    const detail = cfg.repoToken ? raw.split(cfg.repoToken).join("[redacted]") : raw;
    log.warn("pre-run clone failed", { repo: cfg.repo, detail: detail.slice(0, 300) });
    return `Cloning ${cfg.repo} failed before the run (${detail.slice(0, 200)}). It may be private without access — tell the user if you cannot proceed.`;
  }
}

/** Control-plane ORIGIN from the run's gateway config (glyphh-apps' rule:
 *  the gateway is always <origin>/api/gateway). */
function controlOrigin(cfg: HarnessRunConfig): string | null {
  const explicit = (cfg.controlUrl ?? "").trim();
  const base = explicit || (cfg.gatewayUrl ?? "").trim();
  try {
    const u = new URL(base);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return u.origin;
  } catch { return null; }
}

function buildAskServer(session: HarnessSession, cfg?: HarnessRunConfig): McpServer {
  const mcp = new McpServer({ name: "glyphh", version: "1.0.0" }, { capabilities: { tools: {} } });
  mcp.server.setRequestHandler(ListToolsRequestSchema, () =>
    Promise.resolve({
      tools: [
        {
          name: "ask_user",
          description:
            "ask_user(questions) — put real questions to the user and WAIT for answers. " +
            "questions: [{question, options?: string[]}] (max 4; short option labels — the user can always type a custom answer). " +
            "Use when a decision is genuinely the user's. Returns the answers in order; an empty answer means the user skipped it.",
          inputSchema: { type: "object" as const, additionalProperties: true },
        },
        {
          name: "open_app",
          description:
            "open_app(app) — show one of the user's installed apps ON THE USER'S SCREEN, whatever surface they are watching " +
            "(desktop panel, web workbench, mobile tray). You never pick a machine — the surface the user is on renders it. " +
            "'app' is the app's slug or name. To show a WEB PAGE or something you built/deployed, use open_browser(url) instead.",
          inputSchema: { type: "object" as const, additionalProperties: true },
        },
        ...(cfg && cfg.runtimeToken && controlOrigin(cfg) ? [
          {
            name: "pull_source",
            description:
              "pull_source(app) — hydrate the working folder from the app's SOURCE OF RECORD (its R2 snapshot head) and stamp the base. " +
              "ALWAYS call this before working on an app you did not just create in this workspace — the folder here may be empty or stale; the snapshot is the truth. Run npm install after.",
            inputSchema: { type: "object" as const, additionalProperties: true },
          },
          {
            name: "push_source",
            description:
              "push_source(app, force?) — publish the working folder as the app's new source head (node_modules/dist/.git excluded). " +
              "COMPARE-AND-SWAP: if a teammate moved the head since your base, this returns their name and a conflict — run rebase_source, review, then push again. force:true overwrites (only after naming the overwrite to the user). " +
              "Call after every build_app so the app's source of record matches what you shipped.",
            inputSchema: { type: "object" as const, additionalProperties: true },
          },
          {
            name: "rebase_source",
            description:
              "rebase_source(app) — pull a moved source head UNDER your local changes: upstream-only files are taken; files you both changed keep YOURS with theirs staged at .glyphh/upstream/<path> for you to merge by hand. " +
              "Returns what changed first — TELL THE USER before republishing.",
            inputSchema: { type: "object" as const, additionalProperties: true },
          },
        ] : []),
        {
          name: "open_browser",
          description:
            "open_browser(url) — show a web page ON THE USER'S SCREEN, whatever surface they are watching. " +
            "THE way to show a built or deployed app: after build_app publishes, open the returned URL here.",
          inputSchema: { type: "object" as const, additionalProperties: true },
        },
      ],
    }),
  );
  mcp.server.setRequestHandler(CallToolRequestSchema, async (rq) => {
    if ((rq.params.name === "pull_source" || rq.params.name === "push_source" || rq.params.name === "rebase_source")) {
      const origin = cfg ? controlOrigin(cfg) : null;
      if (!cfg || !cfg.runtimeToken || !origin) {
        return { content: [{ type: "text" as const, text: "ERROR: source sync needs a control plane + runtime token on this run" }], isError: true };
      }
      const args = (rq.params.arguments ?? {}) as { app?: unknown; slug?: unknown; force?: unknown };
      const slug = String(args.app ?? args.slug ?? "").trim();
      if (!slug) return { content: [{ type: "text" as const, text: "ERROR: pass { app } — the app's slug or id" }], isError: true };
      const sync: SourceSyncCfg = { workdir: cfg.workdir, controlUrl: origin, token: cfg.runtimeToken };
      try {
        const out = rq.params.name === "pull_source" ? await pullSource(sync, slug)
          : rq.params.name === "push_source" ? await pushSource(sync, slug, args.force === true)
          : await rebaseSource(sync, slug);
        return { content: [{ type: "text" as const, text: out }] };
      } catch (err) {
        return { content: [{ type: "text" as const, text: `ERROR: ${(err as Error).message}` }], isError: true };
      }
    }
    if (rq.params.name === "open_app" || rq.params.name === "open_browser") {
      const args = (rq.params.arguments ?? {}) as { app?: unknown; name?: unknown; url?: unknown };
      const ref = String(args.app ?? args.name ?? "").trim();
      const url = String(args.url ?? "").trim();
      if (rq.params.name === "open_app" && !ref) {
        return { content: [{ type: "text" as const, text: "ERROR: open_app needs { app } — the app's slug or name" }], isError: true };
      }
      if (rq.params.name === "open_browser" && !/^https?:\/\//i.test(url)) {
        return { content: [{ type: "text" as const, text: "ERROR: open_browser needs { url } (http/https)" }], isError: true };
      }
      session.emit({ type: "open-app", ...(rq.params.name === "open_app" ? { ref } : { url }) });
      return { content: [{ type: "text" as const, text: `opening ${ref || url} on the user's screen` }] };
    }
    const raw = ((rq.params.arguments ?? {}) as { questions?: unknown }).questions;
    const questions: AskQuestion[] = (Array.isArray(raw) ? raw : [])
      .slice(0, 4)
      .map((q) => ({
        question: String((q as { question?: unknown })?.question ?? ""),
        ...(Array.isArray((q as { options?: unknown })?.options)
          ? { options: ((q as { options: unknown[] }).options).slice(0, 5).map(String) }
          : {}),
      }))
      .filter((q) => q.question);
    if (!questions.length) {
      return { content: [{ type: "text" as const, text: "ERROR: ask_user needs questions:[{question,options?}]" }], isError: true };
    }
    const id = `ask-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
    const answered = session.waitAnswer(id, ASK_TIMEOUT_MS, { answers: [] });
    session.emit({ type: "ask", id, questions });
    const { answers = [] } = await answered;
    const text = questions
      .map((q, i) => `Q: ${q.question}\nA: ${answers[i] ?? "(no answer — proceed on best judgment)"}`)
      .join("\n\n");
    return { content: [{ type: "text" as const, text }] };
  });
  return mcp;
}

/** The RETURN-HOME memory discipline — appended once when glyphh_facts is
 *  mounted. Lives here beside the tools it describes (the publish-policy
 *  convention): a client-side rule only steers updated clients; this steers
 *  every run that can act on it. */
const FACTS_POLICY = [
  "## Org memory (glyphh_facts)",
  "",
  "You have the org's fact ledger — durable, auditable memory across sessions and models.",
  "AT THE START of substantive work: search_facts for what the org already knows about the task's entities.",
  "AT THE END of a turn that taught you something durable (a preference, a decision, a lasting fact — not chit-chat): distill it into ONE universal-schema glyphh and call build_fact; then reason over the candidates it returns — genuinely new → create_fact; a restatement or change → update_fact with the old id. Wrong or retracted facts → delete_fact.",
  "Retrieval is never pure similarity: the candidates are material for YOUR reasoning; when you compose an answer from several facts, record the composition with build_fact_tree citing its sources.",
  "Express fact values toward NSM primes (KNOW/WANT/GOOD/BAD/DO/HAPPEN/BECAUSE/NOT/...); keep names, numbers and domain terms literal. Prefer the universal schema's layers/roles — they strengthen similarity — but never omit a salient value for lack of a fitting slot: off-schema slots are preserved and prime-indexed, not dropped.",
].join("\n");

function withFactsPolicy(system: string, enabled: boolean): string {
  if (!enabled || system.includes("## Org memory (glyphh_facts)")) return system;
  return system ? `${system}\n\n${FACTS_POLICY}` : FACTS_POLICY;
}

/** Append the turn's rendered fact block — a constant-shape window (stable
 *  header, hard caps) so its cost never grows and the model learns where the
 *  org's facts live. */
function withFactBlock(system: string, block: string | null): string {
  if (!block || system.includes("## Org facts (glyphh ledger")) return system;
  return system ? `${system}\n\n${block}` : block;
}

/** Flatten a tool_result content payload to displayable text (CLI port). */
function flattenResult(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((b) => (typeof b === "object" && b && (b as { type?: string }).type === "text" ? ((b as { text?: string }).text ?? "") : ""))
      .filter(Boolean)
      .join("\n");
  }
  return content == null ? "" : JSON.stringify(content);
}

/**
 * Build the SDK query arguments for one run. Exported separately so the env
 * isolation + tool surface + gate wiring are directly unit-testable.
 */
export function buildQueryArgs(
  cfg: HarnessRunConfig,
  session: HarnessSession,
  attachments: MaterializedAttachment[],
  deps: EngineDeps = {},
): { prompt: string | AsyncIterable<unknown>; options: Record<string, unknown> } {
  const chat = cfg.mode === "chat";
  // Seed the session's LIVE permission from the run config's start snapshot.
  // From here the gate reads `session.permission` (not `cfg.permission`), so a
  // mid-run POST /runs/:id/permission takes effect on the next decision.
  if (session.permission === undefined) session.permission = cfg.permission;
  const mcp = chat ? null : buildAskServer(session, cfg);
  // The FACT SUBSTRATE: six glyphh_facts tools over the org's glyph ledger —
  // only for an introspected caller (the ledger is owner-scoped; a principal-
  // less run gets no memory rather than someone else's).
  const facts = chat || !deps.principal ? null : buildFactsServer(deps.principal);
  // The control plane's app tools, derived from the run's OWN gateway config.
  // This is what lets a CLOUD pod publish an app at all, and what makes a
  // desktop-local pod publish it in exactly the same way. A caller that already
  // lent a server under this name wins — an explicit lend is never overridden.
  const apps = chat ? null : appsServerRef(cfg);
  const caller = cfg.mcpServers ?? [];
  const lent = apps && !caller.some((s) => s.name === apps.name) ? [...caller, apps] : caller;
  // The publish policy rides WITH the tools: no app tools, no policy.
  let system = cfg.system ?? DEFAULT_SYSTEM;
  // The session's GitHub repo — the workspace's SOURCE. Said plainly so the
  // agent never has to ask which repo, and never calls the workspace
  // "throwaway": it persists for this conversation.
  if (cfg.repo) {
    system += `\n\nWORKSPACE: this session's GitHub repo is ${cfg.repo}. Your working folder persists across this conversation's turns. Work on the code there.${cfg.repoNote ? ` ${cfg.repoNote}` : ""}`;
  } else if (cfg.mode !== "chat") {
    system += "\n\nWORKSPACE: your working folder persists across this conversation's turns — files you leave there are still there next turn, and the user can browse them in their Files panel.";
  }
  return {
    // CHAT stays single-shot (one turn, no tools, nothing to steer). The
    // tool-bearing modes take the streaming form so the user can inject
    // follow-ups between turns without stopping the run.
    prompt: chat
      ? buildPrompt(assemblePrompt(cfg.history, cfg.prompt, attachments, cfg.contextBlock), cfg.images ?? [])
      : buildInputStream(assemblePrompt(cfg.history, cfg.prompt, attachments, cfg.contextBlock), cfg.images ?? [], session),
    options: {
      cwd: cfg.workdir,
      ...(cfg.model ? { model: cfg.model } : {}),
      systemPrompt: withFactBlock(withFactsPolicy(apps ? withPublishPolicy(system) : system, !!facts), deps.factBlock ?? null),
      settingSources: [],
      // chat = a tool-less streamed turn; cowork/code = the sandbox toolset
      // plus any caller-lent HTTP MCP servers (streamable-http — the desktop's
      // loopback tool surface).
      //
      // NOTHING IS PRE-APPROVED. `allowedTools` is the SDK's "auto-allow
      // without prompting" list — naming a tool there makes the SDK skip
      // `canUseTool` entirely, which silently disabled the permission gate
      // (plan mode still wrote files; ask mode never asked). Availability is
      // `tools`; the DECISION belongs to canUseTool → gateAction, per call,
      // for built-ins and lent MCP tools alike. `ask_user` needs no allowance:
      // the gate classifies it as a read, which is free in every mode.
      ...(chat
        ? { tools: [] as string[] }
        : {
            tools: SANDBOX_TOOLS,
            mcpServers: {
              glyphh: { type: "sdk", name: "glyphh", instance: mcp as never },
              ...(facts ? { glyphh_facts: { type: "sdk", name: "glyphh_facts", instance: facts as never } } : {}),
              ...Object.fromEntries(
                lent.map((s) => [s.name, { type: "http", url: s.url, ...(s.headers ? { headers: s.headers } : {}) }]),
              ),
            },
          }),
      // The gate: mode model + approval frames (gate.ts). The SDK's own
      // prompt layer always defers to it.
      canUseTool: async (tool: string, input: unknown) => {
        // build_app { distDir }: the POD walks the built folder and inlines the
        // { files } map itself (glyphh-apps.ts) — the model passes a path, never
        // file bytes, and the SERVER contract ({ slug, files }) is unchanged. A
        // failed expansion denies with the reason so the model can re-plan.
        if (tool === BUILD_APP_TOOL) {
          const expanded = await expandBuildAppInput(input, cfg.workdir).catch(
            (err): { ok: false; error: string } => ({ ok: false, error: (err as Error).message }),
          );
          if (expanded) {
            if (!expanded.ok) return { behavior: "deny" as const, message: expanded.error };
            input = expanded.input;
          }
        }
        // save_file { path }: same seam — the pod inlines the workspace file
        // as contentBase64; a failed expansion denies with the reason.
        if (tool === SAVE_FILE_TOOL) {
          const expanded = await expandSaveFileInput(input, cfg.workdir).catch(
            (err): { ok: false; error: string } => ({ ok: false, error: (err as Error).message }),
          );
          if (expanded) {
            if (!expanded.ok) return { behavior: "deny" as const, message: expanded.error };
            input = expanded.input;
          }
        }
        const action = classifyTool(tool, input);
        // The LIVE mode, re-read per call — a mid-run change governs the next
        // decision. Falls back to the start snapshot before the engine seeds it.
        const mode = session.permission ?? cfg.permission;
        const { allowed, reason } = await gateAction(session, mode, action, deps.approvalTimeoutMs);
        return allowed
          ? { behavior: "allow" as const, updatedInput: input as Record<string, unknown> }
          : { behavior: "deny" as const, message: reason ?? "denied" };
      },
      permissionMode: "default",
      persistSession: false,
      includePartialMessages: true,
      ...(cfg.maxTurns > 0 ? { maxTurns: cfg.maxTurns } : {}),
      abortController: session.ctrl,
      env: buildAgentEnv(cfg),
    },
  };
}

/**
 * Run one hosted harness session end to end: materialize attachments, drive
 * the SDK loop, translate every message into frames. Resolves when the run
 * has emitted its terminal frame; never throws.
 */
export async function runHarness(session: HarnessSession, cfg: HarnessRunConfig, deps: EngineDeps = {}, principal?: Principal): Promise<void> {
  if (principal && !deps.principal) deps = { ...deps, principal };
  // THE INJECTION HALF: select this turn's fact block against the newest
  // exchange (last assistant reply + the new prompt — the client resends
  // history, so t-1 is right here). Deterministic prime-overlap: microseconds,
  // no model call; failures degrade to a memory-less turn, never a dead one.
  if (deps.principal && cfg.mode !== "chat" && deps.factBlock === undefined) {
    const tail = cfg.history.slice(-2).map((t) => t.content).join("\n");
    deps = { ...deps, factBlock: await renderFactBlock(deps.principal, `${tail}\n${cfg.prompt}`) };
  }
  const runLog = log.child({ run_id: cfg.runId, session: cfg.sessionId || undefined });
  const secrets = [cfg.runtimeToken];
  const fail = (message: string): void => {
    session.emit({ type: "error", error: redactSecrets(brandError(message), secrets) });
  };

  let attachments: MaterializedAttachment[] = [];
  let repoNote = "";
  try {
    await ensureWorkspace(cfg.workdir);
    // The session's repo lands BEFORE the model runs — a private repo's token
    // never enters the transcript (see ensureRepoClone).
    if (cfg.repo) {
      session.emit({ type: "setup", phase: "clone" });
      repoNote = await ensureRepoClone(cfg);
    }
    // Attachments always land in the POD's sandbox (cfg.attachDir), never in a
    // caller-named local workdir — a run must not litter the user's folder.
    // The prompt names them by ABSOLUTE path, so the agent reads them either way.
    attachments = await materializeAttachments(cfg.attachDir, cfg.attachments, cfg.attachmentMaxBytes, deps.fetchFn);
  } catch (err) {
    runLog.error("run setup failed", { detail: redactSecrets((err as Error).message, secrets) });
    fail((err as Error).message);
    return;
  }

  const queryFn: QueryFn = deps.queryFn ?? ((args) => query(args as Parameters<typeof query>[0]) as AsyncIterable<unknown>);

  let finalText = "";
  let runError = "";
  let sawPartials = false;
  let pendingSep = false;
  let turnsUsed = 0;
  // Real token accounting, like the desktop: input (incl. cache) accumulates
  // per turn from message_start; output ticks live from message_delta.
  let inTok = 0;
  let outDone = 0;
  let outCur = 0;
  const outTok = (): number => outDone + outCur;
  let lastProgressAt = 0;
  // The turn's price lookup, fired at `result` and awaited before the terminal
  // frame. Null until the turn produces a result (an aborted run never asks).
  let creditsProbe: Promise<number | null> | null = null;
  // Correlate tool_use ids → names/titles so tool_result frames name their
  // tool AND keep the call's human title ("Run: <cmd>", "Write /path") — the
  // result block carries no input, so the done frame's title must be remembered
  // from the start.
  const toolNames = new Map<string, string>();
  const toolTitles = new Map<string, string>();

  runLog.info("run start", {
    mode: cfg.mode,
    permission: cfg.permission,
    model: cfg.model ?? "(default)",
    attachments: attachments.length,
    images: (cfg.images ?? []).length,
    // The cwd the agent's tools act on — a local run names the user's folder.
    workdir: cfg.workdir,
  });

  let runCfg = cfg;

  // ── THE FULL-CONTEXT FALLBACK (harness/transcript.ts) ──────────────────────
  // The worker's prompt carries the COMPLETE conversation — every turn of the
  // session, verbatim — with first-class COMPACTION when it outgrows the
  // budget (the memory plane is gone; the transcript IS the thread). The pod
  // retains the transcript per thread; the client's resent history reconciles
  // a pod restart (adopt); compaction folds the oldest turns into a
  // record-once summary + deterministic anchors, keeping the newest verbatim.
  const transcripts = deps.transcripts ?? sessionTranscripts;
  const threadKey = cfg.threadId || cfg.sessionId || cfg.runId;
  {
    transcripts.adopt(threadKey, cfg.history);
    const budgetTokens = Math.max(1, cfg.context?.budgetTokens ?? CONTEXT_DEFAULTS.budgetTokens);
    const keepTurns = Math.max(1, cfg.context?.keepTurns ?? CONTEXT_DEFAULTS.keepTurns);
    // The summarizer is the back path's cheap LLM (the enricher pattern) over
    // the metered gateway; the store degrades to a deterministic gist fold on
    // any failure — compaction never blocks or loses the thread.
    const summarize = gatewaySummarizer({
      gatewayUrl: cfg.gatewayUrl,
      token: cfg.runtimeToken,
      runId: cfg.runId,
      ...(deps.fetchFn ? { fetchFn: deps.fetchFn } : {}),
    });
    const compaction = await transcripts.compact(threadKey, { budgetTokens, keepTurns, summarize });
    if (compaction) {
      session.emit({ type: "compaction", folded: compaction.folded, kept: compaction.kept, summaryChars: compaction.summaryChars, via: compaction.via });
      runLog.info("transcript compacted", {
        thread: threadKey, folded: compaction.folded, kept: compaction.kept,
        summary_chars: compaction.summaryChars, via: compaction.via,
      });
    }
    const contextBlock = transcripts.render(threadKey);
    if (contextBlock) {
      runCfg = { ...runCfg, contextBlock };
      runLog.info("full-context fallback engaged", { thread: threadKey, chars: contextBlock.length, compacted: !!compaction });
    }
  }

  try {
    if (repoNote) runCfg = { ...runCfg, repoNote };
    const q = queryFn(buildQueryArgs(runCfg, session, attachments, deps));
    for await (const raw of q) {
      if (session.ctrl.signal.aborted) break;
      const msg = raw as SDKMessage;
      if (msg.type === "stream_event") {
        const ev = (msg as unknown as {
          event?: {
            type?: string;
            delta?: { type?: string; text?: string };
            message?: { usage?: { input_tokens?: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number } };
            usage?: { output_tokens?: number };
          };
        }).event;
        if (ev?.type === "message_start") {
          outDone += outCur;
          outCur = 0;
          const u = ev.message?.usage;
          if (u) inTok += (u.input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0);
        } else if (ev?.type === "message_delta" && typeof ev.usage?.output_tokens === "number") {
          outCur = ev.usage.output_tokens;
          const now = Date.now();
          if (now - lastProgressAt > 200) {
            lastProgressAt = now;
            session.emit({ type: "progress", inTokens: inTok, outTokens: outTok() });
          }
        } else if (ev?.type === "content_block_delta" && ev.delta?.type === "text_delta" && ev.delta.text) {
          sawPartials = true;
          if (pendingSep && finalText) {
            finalText += "\n\n";
            session.emit({ type: "delta", delta: "\n\n" });
          }
          pendingSep = false;
          finalText += ev.delta.text;
          session.emit({ type: "delta", delta: ev.delta.text });
        }
      } else if (msg.type === "assistant") {
        turnsUsed++;
        const blocks = msg.message.content as Array<{ type: string; text?: string; id?: string; name?: string; input?: unknown }>;
        for (const b of blocks) {
          if (b.type === "tool_use" && b.name) {
            const action = classifyTool(b.name, b.input);
            // The breadcrumb title carries the call's KEY INPUT (command, file
            // path, pattern…), redacted — without it every successful row in a
            // client renders as a bare tool name.
            const title = redactSecrets(toolTitle(b.name, b.input), secrets);
            if (b.id) {
              toolNames.set(b.id, b.name);
              if (title) toolTitles.set(b.id, title);
            }
            session.emit({
              type: "tool",
              phase: "start",
              name: b.name.replace(/^mcp__glyphh__/, ""),
              thought: action.title,
              ...(title ? { title } : {}),
            });
          }
        }
        const text = blocks.filter((b) => b.type === "text").map((b) => b.text || "").join("");
        if (text) {
          if (sawPartials) {
            pendingSep = true;
          } else {
            finalText = finalText ? `${finalText}\n\n${text}` : text;
            session.emit({ type: "delta", delta: finalText === text ? text : `\n\n${text}` });
          }
          const u = (msg as unknown as { message?: { usage?: { input_tokens?: number; output_tokens?: number; cache_read_input_tokens?: number } } }).message?.usage;
          if (u) {
            inTok += (u.input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0);
            outDone += u.output_tokens ?? 0;
          }
          session.emit({ type: "progress", inTokens: inTok, outTokens: outTok() });
        }
      } else if (msg.type === "user") {
        const blocks = (msg as unknown as { message?: { content?: unknown } }).message?.content;
        if (Array.isArray(blocks)) {
          for (const b of blocks as Array<{ type?: string; tool_use_id?: string; content?: unknown; is_error?: boolean }>) {
            if (b.type !== "tool_result") continue;
            const name = (b.tool_use_id && toolNames.get(b.tool_use_id)) || "tool";
            const title = (b.tool_use_id && toolTitles.get(b.tool_use_id)) || "";
            if (b.tool_use_id) toolTitles.delete(b.tool_use_id);
            const result = redactSecrets(flattenResult(b.content), secrets);
            const failed = Boolean(b.is_error);
            session.emit({
              type: "tool",
              phase: "done",
              name: name.replace(/^mcp__glyphh__/, ""),
              ...(title ? { title } : {}),
              failed,
              denied: failed && /denied this action|read-only/.test(result),
              // Errors keep enough of the message to diagnose (desktop: 600/160).
              preview: result.slice(0, failed ? 600 : 160),
            });
          }
        }
      } else if (msg.type === "result") {
        const r = msg as { subtype: string; result?: string; num_turns?: number };
        if (r.subtype === "success") {
          if (!finalText && r.result) {
            finalText = r.result;
            session.emit({ type: "delta", delta: finalText });
          }
        } else {
          runError = `run ended: ${r.subtype.replaceAll("_", " ")}`;
        }
        if (typeof r.num_turns === "number") turnsUsed = r.num_turns;
        // BETWEEN-TURN boundary: queued injected messages start the next turn
        // (the input stream yields them); an empty inbox closes the stream,
        // which is what ends the query. Errors always close.
        if (r.subtype !== "success" || !session.hasInjected()) session.closeInput();
        // The turn's usage is final here, so start the price lookup NOW — it
        // overlaps the loop's drain and has normally resolved by the time the
        // terminal frame goes out, costing the run nothing.
        creditsProbe = fetchCreditsMicro(cfg, deps, runLog);
        // The result's usage is the run's authoritative total.
        const ru = (msg as unknown as { usage?: { input_tokens?: number; output_tokens?: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number } }).usage;
        if (ru) {
          inTok = (ru.input_tokens ?? 0) + (ru.cache_read_input_tokens ?? 0) + (ru.cache_creation_input_tokens ?? 0);
          outDone = ru.output_tokens ?? 0;
          outCur = 0;
          session.emit({ type: "progress", inTokens: inTok, outTokens: outTok() });
        }
      }
    }
    if (session.ctrl.signal.aborted) {
      // A stopped run still happened — retain the partial exchange so the
      // thread survives the stop (record-once per runId).
      transcripts.append(threadKey, cfg.runId, cfg.prompt, finalText);
      session.emit({ type: "done", stopped: true });
      runLog.info("run stopped", { turns: turnsUsed });
      return;
    }
    session.emit({ type: "progress", inTokens: inTok, outTokens: outTok() });
    // The metered price, BEFORE the terminal frame: consumers map `credits` to
    // their price display and `done` to turn end, so credits-then-done is the
    // order they expect. A failed/slow lookup simply yields no frame — it can
    // delay `done` by at most the probe timeout and can never lose it.
    const creditsMicro = creditsProbe ? await creditsProbe : null;
    if (creditsMicro !== null) session.emit({ type: "credits", creditsMicro });
    if (runError) {
      fail(runError);
      runLog.warn("run failed", { detail: runError, turns: turnsUsed });
    } else {
      // ALWAYS retain the exchange in the pod transcript (record-once per
      // runId): the next turn's full-context path must carry this turn. The
      // thread is never lost.
      transcripts.append(threadKey, cfg.runId, cfg.prompt, finalText);
      session.emit({ type: "done", stopped: false });
      runLog.info("run complete", { turns: turnsUsed, in_tokens: inTok, out_tokens: outTok(), credits_micro: creditsMicro ?? undefined });
    }
  } catch (err) {
    if (session.ctrl.signal.aborted) {
      transcripts.append(threadKey, cfg.runId, cfg.prompt, finalText);
      session.emit({ type: "done", stopped: true });
      runLog.info("run stopped", { turns: turnsUsed });
      return;
    }
    const detail = redactSecrets((err as Error).message ?? String(err), secrets);
    runLog.error("run error", { detail });
    fail(detail);
  } finally {
    // A query that ends any way but the result boundary (throw, abort, SDK
    // drain) must still release the input stream's parked waiter.
    session.closeInput();
  }
}

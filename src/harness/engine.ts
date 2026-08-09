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
import { appsServerRef, withPublishPolicy } from "./glyphh-apps.js";
import { classifyTool, gateAction } from "./gate.js";
import { ensureWorkspace, materializeAttachments } from "./sandbox.js";
import type { MaterializedAttachment } from "./sandbox.js";
import { HarnessSession } from "./session.js";
import { ASK_TIMEOUT_MS } from "./gate.js";
import type { AskQuestion } from "./frames.js";
import { log } from "../obs/logger.js";
import type { Logger } from "../obs/logger.js";
import type { Principal } from "../auth/introspect.js";
import { recallForTurn, persistTurn, recallForTurnViaApi, persistTurnViaApi, controlBaseFromGateway } from "./stator-api.js";

/** The injectable SDK seam: the real `query` in production, a fake in tests. */
export type QueryFn = (args: { prompt: string | AsyncIterable<unknown>; options: Record<string, unknown> }) => AsyncIterable<unknown>;

export interface EngineDeps {
  queryFn?: QueryFn;
  fetchFn?: typeof fetch;
  /** Approval wait override (tests shorten it). */
  approvalTimeoutMs?: number;
  /** Credits-lookup timeout override (tests shorten it). */
  creditsTimeoutMs?: number;
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
  "Use ask_user when a decision is genuinely the user's. Answer directly and concisely.";

/** Assemble the per-run prompt the way the desktop does (persistSession is
 *  off — each run carries its own recent history in the user turn). */
export function assemblePrompt(history: ChatTurn[], prompt: string, attachments: MaterializedAttachment[]): string {
  const parts: string[] = [];
  if (history.length) {
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

/** The in-process MCP server carrying the pod's ONE custom tool: `ask_user`.
 *  Same wire pattern as the desktop's glyphh MCP surface. */
function buildAskServer(session: HarnessSession): McpServer {
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
      ],
    }),
  );
  mcp.server.setRequestHandler(CallToolRequestSchema, async (rq) => {
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
  const mcp = chat ? null : buildAskServer(session);
  // The control plane's app tools, derived from the run's OWN gateway config.
  // This is what lets a CLOUD pod publish an app at all, and what makes a
  // desktop-local pod publish it in exactly the same way. A caller that already
  // lent a server under this name wins — an explicit lend is never overridden.
  const apps = chat ? null : appsServerRef(cfg);
  const caller = cfg.mcpServers ?? [];
  const lent = apps && !caller.some((s) => s.name === apps.name) ? [...caller, apps] : caller;
  // The publish policy rides WITH the tools: no app tools, no policy.
  const system = cfg.system ?? DEFAULT_SYSTEM;
  return {
    prompt: buildPrompt(assemblePrompt(cfg.history, cfg.prompt, attachments), cfg.images ?? []),
    options: {
      cwd: cfg.workdir,
      ...(cfg.model ? { model: cfg.model } : {}),
      systemPrompt: apps ? withPublishPolicy(system) : system,
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
              ...Object.fromEntries(
                lent.map((s) => [s.name, { type: "http", url: s.url, ...(s.headers ? { headers: s.headers } : {}) }]),
              ),
            },
          }),
      // The gate: mode model + approval frames (gate.ts). The SDK's own
      // prompt layer always defers to it.
      canUseTool: async (tool: string, input: unknown) => {
        const action = classifyTool(tool, input);
        const { allowed, reason } = await gateAction(session, cfg.permission, action, deps.approvalTimeoutMs);
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
  const runLog = log.child({ run_id: cfg.runId, session: cfg.sessionId || undefined });
  const secrets = [cfg.runtimeToken];
  const fail = (message: string): void => {
    session.emit({ type: "error", error: redactSecrets(brandError(message), secrets) });
  };

  let attachments: MaterializedAttachment[] = [];
  try {
    await ensureWorkspace(cfg.workdir);
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
  // Correlate tool_use ids → names so tool_result frames name their tool.
  const toolNames = new Map<string, string>();

  runLog.info("run start", {
    mode: cfg.mode,
    permission: cfg.permission,
    model: cfg.model ?? "(default)",
    attachments: attachments.length,
    images: (cfg.images ?? []).length,
    // The cwd the agent's tools act on — a local run names the user's folder.
    workdir: cfg.workdir,
  });

  // ROTATE THE TURN AROUND THE STATOR: recall prior facts/directives + similar
  // turns and fold them into the system prompt; write the exchange back after.
  // TWO paths to ONE regional memory plane (docs/runtime-stator-split.md):
  //   cloud pod  — principal present → the org-scoped store, direct.
  //   local pod  — auth off (no principal) → the control plane's /api/stator/*,
  //                bearer = the run's runtimeToken (the user's access token),
  //                control base derived from the gatewayUrl the run carries.
  // Best-effort either way; a memory miss never blocks the turn.
  const statorBase = principal ? null : controlBaseFromGateway(cfg.gatewayUrl);
  const memoryOn = !!(principal || (statorBase && cfg.runtimeToken));
  // Memory silently unarmed is the failure mode that burns hours — say WHY once.
  if (!memoryOn) runLog.info("memory unarmed", { principal: !!principal, stator_base: !!statorBase, token: !!cfg.runtimeToken });
  const recallOpts = {
    ...(cfg.memory?.topK !== undefined ? { topK: cfg.memory.topK } : {}),
    ...(cfg.memory?.threshold !== undefined ? { threshold: cfg.memory.threshold } : {}),
    ...(cfg.memory?.entity ? { entity: cfg.memory.entity } : {}),
  };
  let runCfg = cfg;
  if (memoryOn && cfg.memory?.recall !== false) {
    const thread = cfg.threadId ?? cfg.sessionId;
    const block = principal
      ? await recallForTurn(principal, thread, cfg.prompt, recallOpts)
      : await recallForTurnViaApi(statorBase!, cfg.runtimeToken, thread, cfg.prompt, recallOpts);
    if (block) {
      runCfg = { ...cfg, system: `${cfg.system ?? DEFAULT_SYSTEM}\n\n<memory>\n${block}\n</memory>` };
      runLog.info("recall injected", { chars: block.length, via: principal ? "store" : "api" });
    }
  }

  try {
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
            if (b.id) toolNames.set(b.id, b.name);
            const action = classifyTool(b.name, b.input);
            session.emit({ type: "tool", phase: "start", name: b.name.replace(/^mcp__glyphh__/, ""), thought: action.title });
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
            const result = redactSecrets(flattenResult(b.content), secrets);
            const failed = Boolean(b.is_error);
            session.emit({
              type: "tool",
              phase: "done",
              name: name.replace(/^mcp__glyphh__/, ""),
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
      // Persist the exchange to the stator so the NEXT turn recalls it: log both
      // turns + absorb the user turn into facts. Best-effort; never fails the run.
      if (memoryOn && cfg.memory?.write !== false) {
        const thread = cfg.threadId ?? cfg.sessionId;
        if (principal) await persistTurn(principal, thread, cfg.prompt, finalText, cfg.memory?.entity);
        else await persistTurnViaApi(statorBase!, cfg.runtimeToken, thread, cfg.prompt, finalText, cfg.memory?.entity);
        runLog.info("turn persisted to stator", { via: principal ? "store" : "api" });
      }
      session.emit({ type: "done", stopped: false });
      runLog.info("run complete", { turns: turnsUsed, in_tokens: inTok, out_tokens: outTok(), credits_micro: creditsMicro ?? undefined });
    }
  } catch (err) {
    if (session.ctrl.signal.aborted) {
      session.emit({ type: "done", stopped: true });
      runLog.info("run stopped", { turns: turnsUsed });
      return;
    }
    const detail = redactSecrets((err as Error).message ?? String(err), secrets);
    runLog.error("run error", { detail });
    fail(detail);
  }
}

/**
 * harness/gate.ts — the permission gate, headless.
 *
 * The desktop routes every tool through `gateGlyphhAction` (mode model +
 * approval cards over IPC). The pod keeps the SAME decision table but the
 * "card" is an `approval` frame on the run's stream, and the response arrives
 * via POST /runs/:id/answer — the session's pause/resume registry bridges the
 * two. Defaults mirror the desktop: deny on timeout, deny when nothing can
 * answer.
 *
 * Tool classification covers the SDK's built-in toolset (the pod's v1
 * surface): reads are free in every mode; edits/commands gate by mode; a
 * command matching a danger rule always asks (except bypass).
 */

import type { ActionKind } from "./frames.js";
import type { PermissionMode } from "./config.js";
import type { HarnessSession } from "./session.js";

export const APPROVAL_TIMEOUT_MS = 120_000;
export const ASK_TIMEOUT_MS = 10 * 60_000;

/** A classified action: what the approval card shows. */
export interface ClassifiedAction {
  kind: ActionKind;
  title: string;
  detail: string;
}

/** SDK built-in tool names by class. Anything unknown is treated as a command
 *  (the conservative bucket — it gates in ask mode). */
const READ_TOOLS = new Set(["Read", "Glob", "Grep", "NotebookRead", "TodoWrite", "BashOutput", "WebFetch", "WebSearch", "Task", "ExitPlanMode"]);
const EDIT_TOOLS = new Set(["Write", "Edit", "MultiEdit", "NotebookEdit"]);
const COMMAND_TOOLS = new Set(["Bash", "KillShell"]);

/** Danger heuristics for shell commands (desktop DANGEROUS_RULES, the cloud-
 *  relevant subset — no sudo prompts or GUI here, but a wiped sandbox or a
 *  crypto-miner is still a bad day). */
const DANGEROUS_RULES: Array<{ re: RegExp; why: string }> = [
  { re: /\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)[a-z]*\s+([/~]|\$HOME)/i, why: "recursive force-delete outside the workspace" },
  { re: /\bsudo\b/, why: "privilege escalation" },
  { re: /\bmkfs\b|\bdd\s+if=/, why: "raw device write" },
  { re: /:\(\)\s*\{.*\}\s*;?\s*:/, why: "fork bomb" },
  { re: /\b(curl|wget)\b[^|;&]*\|\s*(ba)?sh\b/, why: "pipe-to-shell from the network" },
  { re: /\bshutdown\b|\breboot\b/, why: "host power control" },
];

function dangerReasons(cmd: string): string[] {
  return DANGEROUS_RULES.filter((r) => r.re.test(cmd)).map((r) => r.why);
}

/** Strip the SDK's MCP prefix so gate rules see bare tool names. */
export function bareToolName(name: string): string {
  return name.replace(/^mcp__[a-z0-9_-]+__/i, "");
}

/** Classify one tool call into the action taxonomy + a human card. */
export function classifyTool(name: string, input: unknown): ClassifiedAction {
  const bare = bareToolName(name);
  const args = (input ?? {}) as Record<string, unknown>;
  if (bare === "ask_user") return { kind: "read", title: "Ask the user", detail: "" };
  if (READ_TOOLS.has(bare)) {
    const target = String(args.file_path ?? args.pattern ?? args.query ?? args.url ?? "");
    return { kind: "read", title: `${bare}${target ? ` ${target}` : ""}`.trim(), detail: "" };
  }
  if (EDIT_TOOLS.has(bare)) {
    const target = String(args.file_path ?? args.notebook_path ?? "");
    return { kind: "edit", title: `Edit ${target || "a file"}`, detail: target };
  }
  if (COMMAND_TOOLS.has(bare)) {
    const cmd = String(args.command ?? args.shell_id ?? "");
    const danger = dangerReasons(cmd);
    if (danger.length) return { kind: "dangerous", title: `Run ${cmd.slice(0, 80)}`, detail: `${cmd}\n⚠ ${danger.join("; ")}` };
    return { kind: "command", title: `Run ${cmd.slice(0, 80) || bare}`, detail: cmd };
  }
  return { kind: "command", title: bare, detail: JSON.stringify(args).slice(0, 200) };
}

/**
 * Decide whether the run may perform `action` under `mode`. Never throws —
 * callers branch on `{allowed}` and hand `reason` back to the model so it can
 * re-plan. An "ask" resolution emits an `approval` frame and PARKS on the
 * session until POST /runs/:id/answer resolves it (or the timeout denies).
 */
export async function gateAction(
  session: HarnessSession,
  mode: PermissionMode,
  action: ClassifiedAction,
  timeoutMs = APPROVAL_TIMEOUT_MS,
): Promise<{ allowed: boolean; reason?: string }> {
  if (action.kind === "read") return { allowed: true };
  if (mode === "bypass") return { allowed: true };
  if (mode === "plan") {
    return { allowed: false, reason: "plan mode is read-only — the user must switch modes before changes can be made" };
  }
  if (mode === "auto" && action.kind !== "dangerous") return { allowed: true };
  if (mode === "acceptEdits" && action.kind === "edit") return { allowed: true };

  const id = `apr-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
  const answered = session.waitAnswer(id, timeoutMs, { allow: false });
  session.emit({ type: "approval", id, kind: action.kind, title: action.title, detail: action.detail });
  const { allow } = await answered;
  return allow ? { allowed: true } : { allowed: false, reason: "the user denied this action" };
}

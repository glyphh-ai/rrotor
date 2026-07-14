/**
 * The TUI session engine — the testable core of the `glyphh` interactive shell.
 *
 * The shell is "three things in one terminal" — chat, co-work, code — but to the
 * runtime it is ONE thing: run the selected **rotor** with the user's prompt. The
 * rotor drives everything (its `metadata.labels.mode` picks the tool/permission set);
 * the user only chooses a rotor and a fallback model (default `auto`). This module is
 * the loop: take a prompt, execute the rotor, and STREAM each step live — via a
 * callback drain, so nothing in the executor changes.
 *
 * The interactive I/O lives in `shell.ts`; everything here is pure enough to unit
 * test (drive `turn`/`command`, collect the events).
 */

import { readdirSync } from "node:fs";
import { join } from "node:path";

import { loadRotor, validateRotor } from "../parser/index.js";
import { execute, type RunResult } from "../exec/executor.js";
import { buildBasicPlugins } from "../plugins/index.js";
import { toolModeFromLabels } from "../tools/index.js";
import { describe as describeError } from "../errors.js";
import type { CapabilityStatus } from "../runtime/registry.js";
import type { Stator } from "../exec/store.js";
import type { DrainPlugin } from "../plugins/interfaces.js";
import type { RotorDocument, StepRecord } from "../types.js";

/** A live event the shell renders as the turn streams. */
export type TurnEvent =
  | { kind: "step"; step_id: string; type: string; status: string; frames: string[]; error?: string }
  | { kind: "answer"; text: string }
  | { kind: "interrupt"; step_id: string; awaiting: unknown }
  | { kind: "error"; code: string; detail: string; remediation: string }
  | { kind: "info"; text: string };

export type EventSink = (e: TurnEvent) => void;

/** A DrainPlugin that renders each recorded StepRecord live (the streaming seam). */
class CallbackDrain implements DrainPlugin {
  readonly name = "drain";
  constructor(private readonly onRec: (rec: StepRecord) => void) {}
  emit(rec: StepRecord): void {
    this.onRec(rec);
  }
  async flush(): Promise<void> {}
  async close(): Promise<void> {}
  status(): CapabilityStatus {
    return { ready: true, detail: "tui stream", tier: "basic" };
  }
}

export interface SessionOptions {
  store: Stator;
  /** The workspace sandbox for fs/exec/git tools. */
  workspace: string;
  /** Available rotors by name. */
  rotors: Map<string, RotorDocument>;
  /** The rotor selected at launch. */
  rotor: string;
  /** A stable session id for memory tier scoping. */
  sessionId?: string;
}

export interface CommandResult {
  handled: boolean;
  quit?: boolean;
  message?: string;
}

/** Load every `*.rotor.yaml` under a directory, keyed by `metadata.name`. */
export function loadRotors(dir: string): Map<string, RotorDocument> {
  const map = new Map<string, RotorDocument>();
  let files: string[] = [];
  try {
    files = readdirSync(dir).filter((f) => f.endsWith(".rotor.yaml") || f.endsWith(".rotor.yml"));
  } catch {
    return map;
  }
  for (const f of files) {
    try {
      const doc = loadRotor(join(dir, f)) as RotorDocument;
      if (validateRotor(doc).valid) map.set(doc.metadata.name, doc);
    } catch {
      /* skip an unparseable rotor */
    }
  }
  return map;
}

export class Session {
  readonly store: Stator;
  readonly workspace: string;
  readonly rotors: Map<string, RotorDocument>;
  readonly sessionId: string;
  rotorName: string;
  model = "auto";
  /** Set while a turn is paused at an approval/wait step, so `resume` can continue it. */
  private pending?: { runId: string; doc: RotorDocument; inputs: Record<string, unknown> };

  constructor(opts: SessionOptions) {
    this.store = opts.store;
    this.workspace = opts.workspace;
    this.rotors = opts.rotors;
    this.rotorName = opts.rotor;
    this.sessionId = opts.sessionId ?? "cli";
    if (!this.rotors.has(this.rotorName)) {
      // Fall back to any available rotor so the session is always runnable.
      const first = [...this.rotors.keys()][0];
      if (first) this.rotorName = first;
    }
  }

  get rotor(): RotorDocument | undefined {
    return this.rotors.get(this.rotorName);
  }

  /** The permission/tool mode the current rotor drives. */
  get mode(): string {
    return toolModeFromLabels(this.rotor?.metadata.labels);
  }

  private plugins(onEvent: EventSink) {
    const drain = new CallbackDrain((rec) =>
      onEvent({
        kind: "step",
        step_id: rec.step_id,
        type: (rec as { type?: string }).type ?? rec.step_id,
        status: rec.status,
        frames: (rec.frames ?? []).map((f) => f.type),
        error: rec.error?.name,
      }),
    );
    return buildBasicPlugins({
      store: this.store,
      drain,
      models: { defaultModel: this.model === "auto" ? undefined : this.model },
      tools: { root: this.workspace, mode: toolModeFromLabels(this.rotor?.metadata.labels) as "chat" | "cowork" | "code" },
    });
  }

  /** Run one prompt through the selected rotor, streaming each step to `onEvent`. */
  async turn(prompt: string, onEvent: EventSink): Promise<RunResult | undefined> {
    const doc = this.rotor;
    if (!doc) {
      onEvent({ kind: "error", code: "E_NO_ROTOR", detail: "no rotor selected", remediation: "Use /rotor <name>." });
      return undefined;
    }
    await this.store.addTurn(prompt);
    const inputs = { prompt, entity: "user" };
    const result = await execute(doc, inputs, this.plugins(onEvent), { session: this.sessionId });
    this.report(result, onEvent, doc, inputs);
    return result;
  }

  /** Continue a turn paused at an approval/wait step (§7.14). */
  async resume(payload: Record<string, unknown>, onEvent: EventSink): Promise<RunResult | undefined> {
    if (!this.pending || !this.pendingInterrupt) {
      onEvent({ kind: "info", text: "nothing to resume" });
      return undefined;
    }
    const { runId, doc, inputs } = this.pending;
    const stepId = this.pendingInterrupt.stepId;
    const result = await execute(doc, inputs, this.plugins(onEvent), { runId, session: this.sessionId, resume: { stepId, payload } });
    this.report(result, onEvent, doc, inputs);
    return result;
  }

  private pendingInterrupt?: { stepId: string; awaiting?: unknown };

  /** Whether a turn is currently paused awaiting a resume (approval). */
  get awaiting(): boolean {
    return this.pending !== undefined;
  }

  private report(result: RunResult, onEvent: EventSink, doc: RotorDocument, inputs: Record<string, unknown>): void {
    if (result.status === "interrupted" && result.interrupt) {
      this.pending = { runId: result.run_id, doc, inputs };
      this.pendingInterrupt = result.interrupt;
      onEvent({ kind: "interrupt", step_id: result.interrupt.stepId, awaiting: result.interrupt.awaiting });
      return;
    }
    this.pending = undefined;
    this.pendingInterrupt = undefined;
    if (result.error) {
      const d = describeError(result.error.name);
      onEvent({ kind: "error", code: d.code, detail: result.error.cause ?? d.summary, remediation: d.remediation });
      return;
    }
    onEvent({ kind: "answer", text: answerOf(result) });
  }

  /** Handle a slash command. Returns whether it was handled (and any message/quit). */
  command(input: string): CommandResult {
    const [cmd, ...rest] = input.slice(1).trim().split(/\s+/);
    const arg = rest.join(" ");
    switch (cmd) {
      case "help":
        return { handled: true, message: HELP };
      case "exit":
      case "quit":
        return { handled: true, quit: true, message: "bye" };
      case "rotors":
        return { handled: true, message: [...this.rotors.keys()].map((n) => (n === this.rotorName ? `* ${n}` : `  ${n}`)).join("\n") || "(no rotors found)" };
      case "rotor":
        if (!arg) return { handled: true, message: `current rotor: ${this.rotorName} (mode: ${this.mode})` };
        if (!this.rotors.has(arg)) return { handled: true, message: `unknown rotor: ${arg} (see /rotors)` };
        this.rotorName = arg;
        return { handled: true, message: `rotor → ${arg} (mode: ${this.mode})` };
      case "model":
        if (!arg) return { handled: true, message: `model: ${this.model}` };
        this.model = arg;
        return { handled: true, message: `model → ${arg}` };
      case "status":
        return { handled: true, message: `rotor=${this.rotorName} mode=${this.mode} model=${this.model} workspace=${this.workspace}` };
      default:
        return { handled: true, message: `unknown command: /${cmd} (try /help)` };
    }
  }
}

function answerOf(result: RunResult): string {
  const out = result.outputs as Record<string, unknown>;
  if (typeof out.answer === "string" && out.answer) return out.answer;
  // Fall back to the terminal step's text output.
  for (let i = result.history.length - 1; i >= 0; i--) {
    const o = result.history[i].output as { text?: unknown } | undefined;
    if (o && typeof o.text === "string" && o.text) return o.text;
  }
  return "";
}

const HELP = `commands:
  /rotor [name]     switch the rotor that drives the loop (no arg: show current)
  /rotors           list available rotors
  /model [name]     set the fallback model (default: auto)
  /status           show rotor · mode · model · workspace
  /help             this help
  /exit             quit
Type anything else to run the current rotor on your prompt.`;

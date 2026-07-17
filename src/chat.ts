/**
 * chat.ts — turn-by-turn chat over the IN-PROCESS runtime (the REPL's chat mode).
 *
 * A ChatSession pins one rotor document + one stator + one memory-scoping session id;
 * every `turn()` is a full rotor run (`open → step* → answer → done`) streamed through
 * {@link runInProcess}, so each turn is checkpointed, replayable, and supportable
 * (`rrotor support <run_id>`) like any other run. Memory carries across turns via
 * the shared stator — durable when `ROTOR_STATOR_BACKEND=sqlite|pgvector`, per-process
 * otherwise. Rendering is injected as a raw `sink` so the REPL owns the TTY and tests
 * can capture output without one.
 */

import { join, dirname } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";

import { runInProcess, type EmbedOptions } from "./embed.js";
import { rotorManifest, type RotorManifest } from "./manifest.js";
import { statorFromEnvAsync } from "./exec/stator.js";
import { loadRotor, validateRotor } from "./parser/index.js";
import type { WireEvent } from "./transport/events.js";
import type { Stator } from "./exec/store.js";
import type { RotorDocument } from "./types.js";

const C = "\x1b[36m";
const W = "\x1b[97m";
const D = "\x1b[90m";
const R = "\x1b[0m";

/** The bundled default chat rotor — the front-door router: every turn is
 *  classified and dispatched to the right domain rotor (chat ↔ code). */
export function defaultChatRotorPath(): string {
  return join(dirname(fileURLToPath(import.meta.url)), "..", "rotors", "router.rotor.yaml");
}

/** Resolve a chat rotor argument: a path is used as-is; a bare name (`base-memory`)
 *  resolves to the bundled `rotors/<name>.rotor.yaml` when one exists. */
export function resolveChatRotor(arg: string): string {
  if (arg.includes("/") || arg.includes("\\") || arg.endsWith(".yaml") || arg.endsWith(".yml")) return arg;
  const bundled = join(dirname(fileURLToPath(import.meta.url)), "..", "rotors", `${arg}.rotor.yaml`);
  return existsSync(bundled) ? bundled : arg;
}

export interface TurnOptions {
  /** Animate a live spinner + elapsed time while the turn runs (TTY only —
   *  redraws with `\r`, so keep it off for pipes and tests). */
  spinner?: boolean;
  /** Values for the rotor's required inputs beyond the primary one (the chat
   *  line). The REPL collects these per turn from the manifest. */
  inputs?: Record<string, unknown>;
}

/** The chat input contract derived from the manifest: the PRIMARY required
 *  input receives the chat line; the EXTRAS must be collected by the client. */
export interface ChatInputs {
  primary: string;
  extras: Array<{ name: string; description?: string }>;
}

export interface ChatSession {
  /** `namespace/name@version` of the pinned rotor, for the banner line. */
  rotor: string;
  /** The memory-scoping session id every turn runs under. */
  session: string;
  /** The rotor's client config contract (roles, mode, tools, inputs). */
  manifest: RotorManifest;
  /** How chat maps onto the rotor's inputs (line → primary; extras collected). */
  inputs: ChatInputs;
  /** Run one turn: stream progress + the answer to `sink` (raw writes, no
   *  trailing-newline contract per call). Never throws — failures render as
   *  taxonomy error lines. */
  turn(prompt: string, sink: (text: string) => void, opts?: TurnOptions): Promise<void>;
  /** Run one turn streaming RAW wire events — the TUI/SDK face; no rendering. */
  turnEvents(prompt: string, onEvent: (ev: WireEvent) => void, inputs?: Record<string, unknown>): Promise<void>;
  /** Stored size per retention tier: fact count + actual bytes of content. */
  memoryStats(): Promise<Record<"short" | "mid" | "long", { count: number; bytes: number }>>;
  close(): Promise<void>;
}

/**
 * Open a chat session over `file` (default: the bundled router — the front door).
 * Throws with a human message when the rotor cannot be loaded or is invalid —
 * the caller decides how to render that.
 */
export async function openChat(file?: string, opts?: { models?: EmbedOptions["models"] }): Promise<ChatSession> {
  const models = opts?.models;
  const path = file ? resolveChatRotor(file) : defaultChatRotorPath();
  let doc: RotorDocument;
  try {
    doc = loadRotor(path);
  } catch (err) {
    throw new Error(`cannot load ${path}: ${(err as Error).message}`);
  }
  const { valid, errors } = validateRotor(doc);
  if (!valid) {
    throw new Error(`${path} is not a valid RotorSpec document (${errors.length} error${errors.length === 1 ? "" : "s"}) — try \`rrotor validate ${path}\``);
  }

  const store: Stator = await statorFromEnvAsync();
  const session = `chat-${randomBytes(4).toString("hex")}`;
  let turnSeq = 0;
  const ns = doc.metadata.namespace ? `${doc.metadata.namespace}/` : "";
  const rotor = `${ns}${doc.metadata.name}@${doc.metadata.version}`;
  const manifest = rotorManifest(doc);
  const required = manifest.inputs.filter((i) => i.required);
  const inputs: ChatInputs = {
    primary: required[0]?.name ?? "prompt",
    extras: required.slice(1).map((i) => ({ name: i.name, ...(i.description ? { description: i.description } : {}) })),
  };

  return {
    rotor,
    session,
    manifest,
    inputs,
    async turn(prompt: string, sink: (text: string) => void, opts?: TurnOptions): Promise<void> {
      const render = new TurnRenderer(sink, opts?.spinner ?? false);
      try {
        const runInputs = { ...(opts?.inputs ?? {}), [inputs.primary]: prompt };
        await runInProcess(doc, runInputs, (ev) => render.onEvent(ev), { store, session, runId: `run-${session}-t${++turnSeq}`, ...(models ? { models } : {}) });
      } catch (err) {
        render.stop();
        sink(`\n  ${D}✗ turn failed: ${(err as Error).message}${R}\n\n`);
      } finally {
        render.stop();
      }
    },
    async turnEvents(prompt: string, onEvent: (ev: WireEvent) => void, extra?: Record<string, unknown>): Promise<void> {
      const runInputs = { ...(extra ?? {}), [inputs.primary]: prompt };
      await runInProcess(doc, runInputs, onEvent, { store, session, runId: `run-${session}-t${++turnSeq}`, ...(models ? { models } : {}) });
    },
    async memoryStats(): Promise<Record<"short" | "mid" | "long", { count: number; bytes: number }>> {
      const facts = await store.snapshotFacts();
      const stats = {
        short: { count: 0, bytes: 0 },
        mid: { count: 0, bytes: 0 },
        long: { count: 0, bytes: 0 },
      };
      for (const f of facts as Array<{ tier?: string; is_current?: boolean; entity?: string; role?: string; filler?: string }>) {
        if (f.is_current === false) continue;
        const tier = f.tier === "short" || f.tier === "long" ? f.tier : "mid";
        stats[tier].count++;
        stats[tier].bytes += Buffer.byteLength(`${f.entity ?? ""}${f.role ?? ""}${f.filler ?? ""}`, "utf8");
      }
      return stats;
    },
    async close(): Promise<void> {
      await store.close?.();
    },
  };
}

const SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const CLEAR_LINE = "\r\x1b[2K";

/**
 * Renders one turn's event stream as a task list. Each completed step prints
 * as a checked row with its cost — `✓ label · 1.2s · 412↑ 89↓` (✓ ok, ✗
 * failed; duration is client-observed arrival spacing, tokens are the step's
 * metered usage §8.5) — with the step's wire `output` summarized as dim tabbed
 * lines beneath, so the user sees WHAT happened, not just that it happened.
 * The live tail is one short spinner line (`⠸ 2.3s · 210↑ 13↓ tok`, redrawn
 * in place — fixed shape, can never wrap). The agent's answer prints as a
 * `●`-marked response line, then the dim run summary.
 */
class TurnRenderer {
  private tokensIn = 0;
  private tokensOut = 0;
  private stubbed = false;
  private readonly startedAt = Date.now();
  private lastStepAt = Date.now();
  private timer?: ReturnType<typeof setInterval>;
  private frame = 0;
  private live = false;

  constructor(
    private readonly sink: (text: string) => void,
    private readonly spinner: boolean,
  ) {
    if (spinner) {
      this.timer = setInterval(() => this.redraw(), 120);
      this.timer.unref?.();
    }
  }

  onEvent(ev: WireEvent): void {
    switch (ev.kind) {
      case "open":
        return;
      case "step": {
        if (ev.frames.includes("stub")) this.stubbed = true;
        if (ev.usage) {
          this.tokensIn += ev.usage.input ?? 0;
          this.tokensOut += ev.usage.output ?? 0;
        }
        const rows = this.stepRows(ev);
        if (this.spinner) {
          this.clearLive();
          this.sink(rows);
          this.redraw();
        } else {
          this.sink(rows);
        }
        return;
      }
      case "answer":
        this.settle();
        this.sink(`\n  ${C}●${R} ${(ev.text || `${D}(no answer)${R}`).replace(/\n/g, "\n    ")}\n`);
        return;
      case "interrupt":
        // §7.14 human-in-the-loop pause. Chat mode has no resume surface (yet) —
        // say so instead of silently dropping the run.
        this.settle();
        this.sink(`\n  ${D}⏸ paused at ${ev.step_id} awaiting input — resume is not supported in chat mode${R}\n`);
        return;
      case "error":
        this.settle();
        this.sink(`\n  ${D}✗ ${ev.code}: ${ev.detail}\n    ↳ fix: ${ev.remediation}${R}\n`);
        return;
      case "done": {
        const status = ev.status === "ok" ? "" : ` · ${ev.status} (${ev.terminal})`;
        this.sink(`  ${D}${this.elapsed()}s${this.tokens()}${status}${R}\n`);
        // A configured model URL that the stub answered for means the endpoint
        // was unreachable mid-turn — say so instead of silently degrading.
        if (this.stubbed && process.env.ROTOR_MODEL_URL) {
          this.sink(`  ${D}⚠ stub answered — model endpoint unreachable (${process.env.ROTOR_MODEL_URL})${R}\n`);
        }
        this.sink("\n");
        return;
      }
    }
  }

  /** One step as printable rows: the checked headline with its cost, then the
   *  step's wire output summarized as dim tabbed lines. `text` fields are
   *  omitted (compose/model bodies — the interpolated label already narrates
   *  them); everything else prints its first line, bounded. */
  private stepRows(ev: Extract<WireEvent, { kind: "step" }>): string {
    const now = Date.now();
    const stepSecs = (now - this.lastStepAt) / 1000;
    this.lastStepAt = now;

    const ok = !ev.error && ev.status !== "failed";
    const mark = ok ? `${C}✓${R}` : `${W}✗${R}`;
    const meta: string[] = [];
    if (stepSecs >= 0.1) meta.push(`${stepSecs.toFixed(1)}s`);
    if (ev.usage && (ev.usage.input ?? 0) + (ev.usage.output ?? 0) > 0) {
      meta.push(`${ev.usage.input ?? 0}↑ ${ev.usage.output ?? 0}↓`);
    }
    if (ev.error) meta.push(ev.error);
    const metaStr = meta.length ? ` ${D}· ${meta.join(" · ")}${R}` : "";
    let rows = `  ${mark} ${D}${ev.display?.label ?? ev.step_id}${R}${metaStr}\n`;

    for (const line of summarizeOutput(ev.output)) {
      rows += `      ${D}${line}${R}\n`;
    }
    return rows;
  }

  /** Stop the spinner (idempotent) — also the abort path on a thrown turn. */
  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
    this.clearLive();
  }

  private elapsed(): string {
    return ((Date.now() - this.startedAt) / 1000).toFixed(1);
  }

  private tokens(): string {
    return this.tokensIn + this.tokensOut > 0 ? ` · ${this.tokensIn}↑ ${this.tokensOut}↓ tok` : "";
  }

  /** Erase the live spinner line so a finished row (or the answer) can print. */
  private clearLive(): void {
    if (this.live) {
      this.sink(CLEAR_LINE);
      this.live = false;
    }
  }

  /** Rewrite the live tail: spinner + elapsed + running token count. Always a
   *  short fixed-shape line, so it can never wrap (wrapping breaks `\r`-based
   *  redraws — erase-line only clears one row). */
  private redraw(): void {
    this.frame = (this.frame + 1) % SPIN.length;
    this.sink(`${CLEAR_LINE}  ${C}${SPIN[this.frame]}${R} ${D}${this.elapsed()}s${this.tokens()}${R}`);
    this.live = true;
  }

  /** Finalize before a terminal frame: the finished rows are already printed —
   *  just take the live line down. */
  private settle(): void {
    this.stop();
  }
}

/** Render a step's wire output as its WORK: short values become one-line
 *  facts (`verdict pass`), multi-line/long strings become indented blocks
 *  capped at 12 lines (`… (+N more lines)`). The rotor's `events.output`
 *  mode already decided how much arrives — `min` steps send nothing, so
 *  plumbing stays silent by authorship, not by client guessing. `usage` is
 *  skipped (already on the row header). */
const BLOCK_LINES = 12;
function summarizeOutput(output?: Record<string, unknown>): string[] {
  if (!output) return [];
  const lines: string[] = [];
  for (const key of Object.keys(output).sort()) {
    if (key === "usage") continue;
    const v = output[key];
    if (v === undefined || v === null) continue;
    if (typeof v === "string" && (key === "text" || v.includes("\n") || v.length > 110)) {
      // A body — show the work as a block.
      const body = v.replace(/\n+$/, "").split("\n");
      for (const line of body.slice(0, BLOCK_LINES)) lines.push(line.length > 110 ? `${line.slice(0, 109)}…` : line);
      if (body.length > BLOCK_LINES) lines.push(`… (+${body.length - BLOCK_LINES} more lines)`);
      continue;
    }
    const img = typeof v === "string" ? v : (JSON.stringify(v) ?? "");
    if (!img || img === "[]" || img === "{}") continue;
    lines.push(`${key} ${img.length > 110 ? `${img.slice(0, 109)}…` : img}`);
  }
  return lines;
}

/** The chat-mode banner lines printed on entry. */
export function chatBannerLines(chat: ChatSession): string[] {
  const url = process.env.ROTOR_MODEL_URL;
  const lane = url
    ? `${D}· model ${url}${R}`
    : `${D}·${R} model ${C}stub${R} ${D}(set ROTOR_MODEL_URL for a live model)${R}`;
  const m = chat.manifest;
  const roles = m.roles.length
    ? `roles ${m.roles.map((r) => `${r.role}→${r.lane}`).join(" ")}`
    : "no model roles";
  const home = homedir();
  const cwd = process.cwd();
  const ws = cwd.startsWith(home) ? `~${cwd.slice(home.length)}` : cwd;
  const tools = m.tools.length ? ` · tools ${m.tools.map((t) => t.name).join(" ")} · ws ${ws}` : "";
  const extras = chat.inputs.extras.length
    ? [`  ${D}your line is \`${chat.inputs.primary}\`; also asks for ${chat.inputs.extras.map((e) => e.name).join(", ")} each turn (Enter reuses the last value)${R}`]
    : [];
  return [
    `  ${C}chat${R} ${D}·${R} ${chat.rotor} ${D}· session ${chat.session}${R} ${lane}`,
    `  ${D}mode ${m.mode} · ${roles}${tools}${R}`,
    ...extras,
    `  ${D}every line is a turn · /exit to leave chat · /quit to exit${R}`,
    "",
  ];
}

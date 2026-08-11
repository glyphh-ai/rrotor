/**
 * harness/frames.ts — the EVENT FRAME VOCABULARY of a hosted harness session.
 *
 * This is the renderer contract, extracted verbatim from the desktop app's
 * frame senders (`app/src/main/glyphh-agent.ts`, `glyphh-agent-permissions.ts`)
 * and the renderer handlers (`app/src/renderer/help/code.ts`,
 * `window.glyphh.onGlyphhAgent*`). The pod emits the SAME frames over its
 * transport so a web bridge can dispatch them unchanged: a frame with
 * `type: "delta"` maps to the desktop IPC channel `glyphh-agent:delta`, and so
 * on for every type — channel = `glyphh-agent:${frame.type}`.
 *
 * Frame schema (payload shapes mirror the desktop emit sites exactly):
 *
 *   delta     { delta }                          — streamed answer text; append
 *   tool      { phase:"start", name, thought? }  — a tool began
 *             { phase:"done", name, title?, denied?, failed?, preview? }
 *   progress  { inTokens, outTokens }            — live token accounting
 *   credits   { creditsMicro }                   — running metered price (see note)
 *   approval  { id, kind, title, detail }        — PAUSES the run; answer with
 *                                                  { id, allow } to resume
 *   ask       { id, questions:[{question,options?}] } — PAUSES the run; answer
 *                                                  with { id, answers:[…] }
 *   setup     { phase, … }                       — reserved (desktop model
 *                                                  download/build UI); the pod
 *                                                  does not emit it in v1
 *   done      { stopped }                        — terminal; stopped=true on abort
 *   error     { error }                          — terminal; human-readable
 *
 * Not ported: `connect` (inline account-connect cards) — deeply coupled to the
 * desktop's connector stack; v1 pods have no connector tools.
 *
 * Credits note: on desktop, `credits` frames come from the local interceptor's
 * usage stream. The pod has no interceptor — the gateway meters server-side by
 * the `x-glyphh-run` tag every model call carries — so the engine reads the
 * turn's price back from the gateway's per-run usage endpoint at turn end and
 * emits ONE `credits` frame immediately BEFORE the terminal frame. The lookup
 * is advisory: if it 404s, 401s, or times out the turn ends normally with no
 * `credits` frame at all, so consumers must treat the frame as optional.
 *
 * On the wire every frame is enveloped with a monotonic `seq` (the replay
 * cursor), the `runId` it belongs to, and an `at` timestamp. A late subscriber
 * replays the ring from its cursor and misses nothing (same reconnect story as
 * the rotor stream's WireEvent tape, in-memory for v1 — stator-backed history
 * is the later convergence).
 */

/** One question the agent puts to the user (the desktop's `ask_user` shape). */
export interface AskQuestion {
  question: string;
  options?: string[];
}

/** The action taxonomy the approval gate classifies tools into (desktop
 *  `glyphh-agent-permissions.ts` GlyphhActionKind). */
export type ActionKind = "read" | "edit" | "command" | "network" | "dangerous";

/** A harness event frame, pre-envelope. Shapes mirror the desktop emit sites. */
export type AgentFrame =
  | { type: "delta"; delta: string }
  | {
      type: "tool";
      phase: "start" | "done";
      name: string;
      thought?: string;
      title?: string;
      denied?: boolean;
      failed?: boolean;
      preview?: string;
    }
  | { type: "progress"; inTokens: number; outTokens: number }
  | { type: "credits"; creditsMicro: number }
  | { type: "approval"; id: string; kind: ActionKind; title: string; detail: string }
  | { type: "ask"; id: string; questions: AskQuestion[] }
  | { type: "setup"; phase: string; [k: string]: unknown }
  // The run's LIVE permission mode changed mid-flight (POST /runs/:id/permission).
  // Advisory + non-terminal: it rides the frame stream so EVERY surface observing
  // the run re-renders its mode chip in lockstep (the gate change itself is
  // server-side). Older consumers ignore the unknown type harmlessly.
  | { type: "permission"; permission: "ask" | "plan" | "acceptEdits" | "auto" | "bypass" }
  // The FULL-CONTEXT FALLBACK compacted the session transcript before this run
  // (harness/transcript.ts): `folded` oldest turns became the summary, `kept`
  // recent turns stayed verbatim. Advisory + additive — surfaces may render a
  // "conversation compacted" notice; older consumers ignore it harmlessly.
  | { type: "compaction"; folded: number; kept: number; summaryChars: number; via: "model" | "deterministic" }
  | { type: "done"; stopped: boolean }
  | { type: "error"; error: string };

/** A frame as it rides the transport: enveloped with run identity + cursor. */
export type WireFrame = AgentFrame & { seq: number; runId: string; at: number };

/** Bump on any breaking change to the frame shapes above. */
export const HARNESS_WIRE_VERSION = "glyphh.harness/v1";

/** Terminal frame types — after one of these the run emits nothing further. */
export function isTerminal(frame: AgentFrame): boolean {
  return frame.type === "done" || frame.type === "error";
}

/**
 * A bounded in-memory frame ring: the v1 replay store. Push assigns nothing —
 * the session assigns `seq` — the ring just retains the most recent `cap`
 * frames for late subscribers. 5000 frames comfortably covers a long
 * interactive run (deltas dominate); the stator-backed tape replaces this at
 * convergence.
 */
export class FrameRing {
  private frames: WireFrame[] = [];

  constructor(private readonly cap = 5000) {}

  push(frame: WireFrame): void {
    this.frames.push(frame);
    if (this.frames.length > this.cap) this.frames.splice(0, this.frames.length - this.cap);
  }

  /** Frames with `seq` strictly greater than `from` (use -1 for everything). */
  since(from: number): WireFrame[] {
    // Frames are seq-ordered by construction; find the first past the cursor.
    let lo = 0;
    while (lo < this.frames.length && this.frames[lo].seq <= from) lo++;
    return this.frames.slice(lo);
  }

  get size(): number {
    return this.frames.length;
  }
}

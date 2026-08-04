/**
 * panel/idle.ts — the PURE encoder idle gate: motion signal in, encoder
 * commands out.
 *
 * The measured problem this solves: with `-crf` + `-maxrate` a STILL page still
 * costs real bandwidth (~59.5 kbit/s at 15fps) and real CPU (~0.42 core/panel),
 * because the encoder keeps emitting delta frames for pixels that never change —
 * while the change-driven JPEG screencast costs literally ZERO when nothing
 * moves. So on a still page the encoder must not run at all, and the panel
 * must be strictly cheaper than v1 in EVERY workload, idle included.
 *
 * The motion signal is the CDP screencast itself: it is change-driven, so a
 * `Page.screencastFrame` arriving IS motion and its absence IS stillness. No
 * pixel diffing, no polling the page — the signal already exists and already
 * costs nothing when the page is still.
 *
 * This module is deliberately PURE (no timers, no encoder, no peers): the
 * session feeds it events with an injected clock and acts on the returned
 * commands, so the whole idle/active decision is unit-testable with everything
 * faked. The commands:
 *
 *   "pause"   the page has been still for `idleAfterMs` AND the encoder has
 *             proven media since it last started → stop the encoder, put the
 *             WebRTC subscribers back on the JPEG screencast (which costs
 *             nothing while the page stays still).
 *   "resume"  motion arrived while paused → restart the encoder. The JPEG
 *             screencast is already carrying the motion to the parked
 *             subscribers, so the user sees the change immediately; WebRTC
 *             takes back over only when it has re-proven itself.
 *   "remute"  the restarted encoder has emitted `proofPackets` RTP packets —
 *             media is genuinely flowing again → move the parked subscribers
 *             back onto WebRTC and mute their JPEG. Proof is required EVERY
 *             activation: promising a client "webrtc" before packets exist is
 *             exactly the black-panel class of bug.
 *
 * The gate never pauses an unproven encoder: until `proofPackets` packets have
 * flowed since the last start, negotiation-level fallback (PanelPeer's timeout)
 * owns the failure story and the gate stays out of the way.
 */

export type GateState = "active" | "idle";

export interface IdleGateConfig {
  /** How long the page must be still before the encoder is paused.
   *  `<= 0` disables the gate entirely (it then never pauses and never
   *  resumes — the encoder lifecycle is exactly the pre-gate one). */
  idleAfterMs: number;
  /** RTP packets that must flow after a (re)start before subscribers are put
   *  (back) on WebRTC and before the gate is allowed to pause. */
  proofPackets: number;
}

/**
 * One gate per panel. The session owns the timer and the side effects; this
 * class owns every decision, with an explicit `now` on every input so tests
 * control the clock.
 */
export class EncoderIdleGate {
  /** Whether the encoder should be running right now. */
  state: GateState = "active";
  /** Total idle⇄active transitions — the flap counter the stats report. */
  transitions = 0;
  /** Motion→first-RTP-packet latency of the most recent resume, in ms — the
   *  number the "user doesn't notice" requirement is judged by. */
  lastResumeMs: number | null = null;

  private lastMotionAt = 0;
  private proven = false;
  private packetsSinceStart = 0;
  private resumeMotionAt: number | null = null;

  constructor(private readonly cfg: IdleGateConfig) {}

  get enabled(): boolean {
    return this.cfg.idleAfterMs > 0;
  }

  /** The encoder (re)started — arm the stillness countdown and demand fresh
   *  proof. Also covers a peer attaching while the gate is idle: the attach
   *  starts the encoder, so the gate must follow it back to active. */
  started(now: number): void {
    this.packetsSinceStart = 0;
    this.proven = false;
    this.lastMotionAt = now;
    if (this.state === "idle") {
      this.state = "active";
      this.transitions++;
    }
  }

  /** A motion signal (one screencast frame). Returns `"resume"` exactly when it
   *  wakes an idle gate. */
  motion(now: number): "resume" | null {
    this.lastMotionAt = now;
    if (!this.enabled || this.state === "active") return null;
    this.state = "active";
    this.transitions++;
    this.resumeMotionAt = now;
    // Fresh proof from the restarted encoder, not stale proof from before the
    // pause — subscribers move back only on packets that actually exist.
    this.proven = false;
    this.packetsSinceStart = 0;
    return "resume";
  }

  /** One RTP packet left the encoder. Returns `"remute"` exactly once per
   *  activation, when proof is reached. Packets while idle (a stopping
   *  encoder's tail) are ignored. */
  packet(now: number): "remute" | null {
    if (this.state !== "active" || this.proven) return null;
    this.packetsSinceStart++;
    if (this.packetsSinceStart === 1 && this.resumeMotionAt !== null) {
      this.lastResumeMs = Math.max(0, now - this.resumeMotionAt);
      this.resumeMotionAt = null;
    }
    if (this.packetsSinceStart >= this.cfg.proofPackets) {
      this.proven = true;
      return "remute";
    }
    return null;
  }

  /** Timer poll: pause now? Only a PROVEN, enabled, active gate that has seen
   *  no motion for the whole window pauses. */
  check(now: number): "pause" | null {
    if (!this.enabled || this.state !== "active" || !this.proven) return null;
    if (now - this.lastMotionAt < this.cfg.idleAfterMs) return null;
    this.state = "idle";
    this.transitions++;
    this.proven = false;
    return "pause";
  }

  /** When the next `check` is due, or null when no check could ever pause
   *  (disabled, already idle, or not yet proven). The session's one timer is
   *  armed from this. */
  nextCheckAt(): number | null {
    if (!this.enabled || this.state !== "active" || !this.proven) return null;
    return this.lastMotionAt + this.cfg.idleAfterMs;
  }
}

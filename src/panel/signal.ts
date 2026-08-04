/**
 * panel/signal.ts — the PURE client→server signaling vocabulary and the PURE
 * transport-selection rule.
 *
 * Everything here is side-effect free so the two decisions that actually matter
 * — "is this inbound frame a valid signaling message?" and "which video
 * transport does this subscriber end up on?" — are unit-testable with no
 * Chromium, no ffmpeg and no peer connection. The stateful machinery
 * (panel/webrtc.ts, panel/encoder.ts) imports these; it never re-implements
 * them.
 *
 * Signaling is deliberately a THIN, closed vocabulary: `hello`, `answer`, `ice`.
 * Everything the wire carries is validated here before it reaches werift —
 * an SDP is passed through as an opaque string (werift parses it) but the
 * ENVELOPE is checked, so a malformed/hostile frame is a logged drop, never an
 * exception inside the peer connection.
 *
 * Secret hygiene: nothing in this module logs. SDP and ICE candidate strings
 * carry DTLS fingerprints and host IP addresses; {@link describeCandidate}
 * exists so callers can log a candidate's TYPE and PROTOCOL without ever
 * emitting the candidate line itself.
 */

import type { PanelIceCandidate, PanelTransport } from "./frames.js";

// ── client → server signaling ────────────────────────────────────────────────

/** The client's capability advertisement. Sending it is what opts a client into
 *  v2 negotiation at all — a v1 client never sends it and stays on screencast. */
export interface HelloMessage {
  type: "hello";
  /** The client can do WebRTC and wants an offer. */
  webrtc: boolean;
  /** Codecs the client prefers, most-preferred first (advisory). */
  codecs?: string[];
}

/** The client's SDP answer to the pod's offer. */
export interface AnswerMessage {
  type: "answer";
  sdp: string;
}

/** One trickled remote ICE candidate (`candidate:null` = end-of-candidates). */
export interface IceMessage {
  type: "ice";
  candidate: PanelIceCandidate | null;
}

/** Any client→server signaling message. */
export type SignalMessage = HelloMessage | AnswerMessage | IceMessage;

/** The signaling `type` values, so the session can route a frame to signaling
 *  vs input without duplicating the list. */
export const SIGNAL_TYPES: ReadonlySet<string> = new Set(["hello", "answer", "ice"]);

/** True when a decoded wire frame is addressed to the signaling channel (rather
 *  than being an input event). Cheap discriminator — validation is
 *  {@link parseSignal}'s job. */
export function isSignalType(type: unknown): boolean {
  return typeof type === "string" && SIGNAL_TYPES.has(type);
}

/** An SDP is opaque to us but not unbounded: reject anything that is not a
 *  plausibly-shaped, sanely-sized session description before werift sees it. */
const MAX_SDP_BYTES = 64 * 1024;
const MAX_CANDIDATE_CHARS = 1024;

/**
 * Validate one decoded inbound frame as a signaling message.
 *
 * Returns the narrowed message, or `null` when the frame is not valid
 * signaling — callers drop-and-log rather than throw, because a WS peer can send
 * anything and one bad frame must never take a panel down.
 */
export function parseSignal(raw: unknown): SignalMessage | null {
  if (!raw || typeof raw !== "object") return null;
  const msg = raw as Record<string, unknown>;

  if (msg.type === "hello") {
    const codecs = Array.isArray(msg.codecs)
      ? msg.codecs.filter((c): c is string => typeof c === "string").slice(0, 8)
      : undefined;
    return { type: "hello", webrtc: msg.webrtc === true, ...(codecs?.length ? { codecs } : {}) };
  }

  if (msg.type === "answer") {
    const sdp = typeof msg.sdp === "string" ? msg.sdp : "";
    // An answer always starts with the version line; anything else is not SDP.
    if (!sdp.startsWith("v=") || Buffer.byteLength(sdp) > MAX_SDP_BYTES) return null;
    return { type: "answer", sdp };
  }

  if (msg.type === "ice") {
    if (msg.candidate === null || msg.candidate === undefined) return { type: "ice", candidate: null };
    if (typeof msg.candidate !== "object") return null;
    const c = msg.candidate as Record<string, unknown>;
    const line = typeof c.candidate === "string" ? c.candidate : "";
    // An empty candidate line is the other legal spelling of end-of-candidates.
    if (!line) return { type: "ice", candidate: null };
    if (line.length > MAX_CANDIDATE_CHARS) return null;
    return {
      type: "ice",
      candidate: {
        candidate: line,
        sdpMid: typeof c.sdpMid === "string" ? c.sdpMid : null,
        sdpMLineIndex: Number.isFinite(Number(c.sdpMLineIndex)) ? Number(c.sdpMLineIndex) : null,
        ...(typeof c.usernameFragment === "string" ? { usernameFragment: c.usernameFragment } : {}),
      },
    };
  }

  return null;
}

/** The candidate TYPE + PROTOCOL of an ICE candidate line, for logging without
 *  emitting the line (which carries a host/reflexive IP address). Returns
 *  `"unknown"` for anything unparseable.
 *
 *  A candidate line is `candidate:<foundation> <component> <proto> <pri> <ip>
 *  <port> typ <type> …`. */
export function describeCandidate(line: string): { type: string; protocol: string } {
  const typ = /\btyp\s+(\w+)/.exec(line);
  const parts = line.replace(/^a=/, "").split(/\s+/);
  const protocol = parts[2] && /^(udp|tcp)$/i.test(parts[2]) ? parts[2].toLowerCase() : "unknown";
  return { type: typ?.[1] ?? "unknown", protocol };
}

// ── announcing an address the client can actually reach ──────────────────────

/**
 * Rewrite the address of a HOST candidate to the address the outside world
 * reaches this pod at.
 *
 * A server behind NAT gathers candidates for the address it can see — a
 * container's `172.17.x.x`, a Fly machine's private 6PN address — and offers
 * that to a client that has no route to it. STUN does not solve this for a
 * SERVER: there is nothing to discover, the pod simply has to be TOLD. Every
 * production SFU has this knob (mediasoup calls it `announcedIp`); this is ours,
 * and it is the difference between WebRTC working and not working on any NAT'd
 * host, Fly included.
 *
 * Only `typ host` candidates are rewritten. Server-reflexive and relay
 * candidates already carry an externally-valid address and must be left alone.
 * The candidate line's grammar is positional — `candidate:<foundation>
 * <component> <transport> <priority> <connection-address> <port> typ …` — so the
 * fifth field is the address.
 */
export function announceCandidate(line: string, announceIp: string): string {
  if (!announceIp) return line;
  const prefix = line.startsWith("a=") ? "a=" : "";
  const body = prefix ? line.slice(2) : line;
  const parts = body.split(" ");
  if (parts.length < 8 || !/\btyp\s+host\b/.test(body)) return line;
  parts[4] = announceIp;
  return prefix + parts.join(" ");
}

/** Apply {@link announceCandidate} to every candidate line embedded in an SDP.
 *  werift may include already-gathered candidates in the offer, and those need
 *  the same treatment as the trickled ones or the client races to an
 *  unreachable address first. */
export function announceSdp(sdp: string, announceIp: string): string {
  if (!announceIp) return sdp;
  return sdp
    .split(/\r?\n/)
    .map((line) => (line.startsWith("a=candidate:") ? announceCandidate(line, announceIp) : line))
    .join("\r\n");
}

// ── the transport decision ───────────────────────────────────────────────────

/** Everything the transport rule is allowed to look at. */
export interface TransportInputs {
  /** The client sent `hello{webrtc:true}` — it can render a video track. */
  clientWantsWebrtc: boolean;
  /** The pod has a capturable display AND an encoder. */
  serverCanWebrtc: boolean;
  /** The peer connection reached `connected` AND media is actually flowing. */
  mediaFlowing: boolean;
  /** The negotiation window elapsed without media. */
  timedOut: boolean;
  /** A hard failure (peer failed/closed, encoder died). */
  failure?: string | undefined;
}

/** The decision plus the reason string that goes on the wire and in the log. */
export interface TransportDecision {
  transport: PanelTransport;
  reason: string;
}

/**
 * The ONE rule that picks a subscriber's video transport.
 *
 * Screencast is the floor: it is what a subscriber gets unless WebRTC has
 * PROVEN itself (client asked, pod can, peer connected, media flowing). Every
 * other branch — no client support, no display/encoder, a failure, a timeout —
 * resolves to screencast with a reason, which is exactly the non-negotiable
 * automatic-fallback requirement expressed as a pure function.
 *
 * `failure` wins over `mediaFlowing`: a peer that carried media and THEN died
 * must fall back, not stay on a dead transport.
 */
export function decideTransport(i: TransportInputs): TransportDecision {
  if (i.failure) return { transport: "screencast", reason: `webrtc failed: ${i.failure}` };
  if (!i.clientWantsWebrtc) return { transport: "screencast", reason: "client did not request webrtc" };
  if (!i.serverCanWebrtc) return { transport: "screencast", reason: "pod has no capturable display or encoder" };
  if (i.mediaFlowing) return { transport: "webrtc", reason: "peer connected, media flowing" };
  if (i.timedOut) return { transport: "screencast", reason: "webrtc negotiation timed out" };
  return { transport: "screencast", reason: "webrtc negotiating" };
}

/**
 * Whether the CDP screencast still has to run at all.
 *
 * The screencast is the fallback, so it stays on while ANY subscriber is on it —
 * including subscribers still negotiating. Only when every attached subscriber
 * has landed on WebRTC can `Page.stopScreencast` be issued, which is what makes
 * the WebRTC path's CPU and bandwidth numbers honest (no JPEG encoder quietly
 * running behind them).
 *
 * With NO subscribers it stays ON. That is deliberate, not an oversight: a panel
 * with no viewer is about to be reaped anyway, and keeping the v1 lifecycle
 * (start() casts, unconditionally) means nothing about the pre-existing path
 * changes shape just because v2 exists.
 */
export function screencastNeeded(subscriberTransports: readonly PanelTransport[]): boolean {
  if (subscriberTransports.length === 0) return true;
  return subscriberTransports.some((t) => t !== "webrtc");
}

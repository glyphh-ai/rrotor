/**
 * panel/frames.ts — the SERVER→CLIENT wire vocabulary of a browser panel.
 *
 * The panel WS is bidirectional: the client sends {@link InputEvent}s (panel/
 * input.ts) and the server sends the messages below. The stream is
 * intentionally tiny — a screencast `frame` carries a base64 JPEG, `nav`
 * reports url/title changes, `ready`/`closed`/`error` bracket the lifecycle.
 *
 * Unlike the harness frame tape, panel frames are NOT replayed from a ring: a
 * screencast is live-only (a reconnecting client just gets the next keyframe on
 * the next visual change; there is no value in replaying stale pixels). So there
 * is no seq envelope here — every message is self-contained.
 *
 * Wire messages (server → client), each a JSON text frame:
 *   { type:"ready",  panelId, wire, viewport:{width,height}, capabilities }
 *                                                — hello on attach
 *   { type:"frame",  data, format, meta:{deviceWidth,deviceHeight}, at }
 *                                                — one screencast frame (data = base64)
 *   { type:"nav",    url, title }                — url/title changed
 *   { type:"closed", reason }                    — the panel/context tore down
 *   { type:"error",  detail }                    — a non-fatal problem
 *   { type:"pong" }                              — app-level keepalive reply
 *
 * ── v2: the WebRTC video transport ──────────────────────────────────────────
 * v2 adds a SECOND video transport negotiated over this same socket. The pod
 * captures its X display with ffmpeg (H.264/VP8 — encoded in NATIVE code, never
 * in JS) and werift packetizes the result; only the SIGNALING rides the WS.
 * Added messages:
 *
 *   server → client
 *     { type:"offer",     sdp }                  — the pod's sendonly video offer
 *     { type:"ice",       candidate }            — a trickled local candidate
 *                                                  (candidate:null = end-of-candidates)
 *     { type:"transport", transport, reason }    — WHICH video path is live NOW
 *   client → server
 *     { type:"hello",  webrtc:boolean, codecs? } — client capability advertisement
 *     { type:"answer", sdp }                     — the client's answer
 *     { type:"ice",    candidate }               — a trickled remote candidate
 *
 * BACKWARD COMPATIBILITY IS LOAD-BEARING. A v1 client never sends `hello`, so it
 * never sees an offer and the JPEG screencast keeps flowing exactly as before —
 * the v2 messages are strictly additive. And the screencast is not merely a
 * legacy path, it is the mandatory FALLBACK: it keeps running through negotiation
 * (so a panel is never black) and is muted for a subscriber only once THAT
 * subscriber's peer connection is actually carrying media. Clients without
 * WebRTC, networks that drop UDP, pods with no X display or no ffmpeg — all land
 * silently on `transport:"screencast"`, and the `transport` message tells the
 * client (and the logs) which one it got.
 *
 * Secret hygiene: a `nav` url could embed credentials (e.g. `https://u:p@host`).
 * {@link redactUrl} strips userinfo before the url is logged — NEVER before it
 * reaches the client (the client is the user watching the page; it needs the
 * real url) — logging is the only place it is scrubbed. SDP/ICE payloads are
 * never logged verbatim either (an SDP carries DTLS fingerprints and host IPs);
 * only candidate TYPES and counts are.
 */

/** Bump on any breaking change to the panel wire shapes. */
export const PANEL_WIRE_VERSION = "glyphh.panel/v2";

/** The previous wire, still spoken by any client that never sends `hello`. */
export const PANEL_WIRE_VERSION_V1 = "glyphh.panel/v1";

/** Which video path a subscriber's pixels are actually arriving on. */
export type PanelTransport = "webrtc" | "screencast";

/** What this pod can offer a client, announced in `ready` so a v2 client knows
 *  whether negotiation is even worth attempting. */
export interface PanelCapabilities {
  /** True when the pod has a capturable X display AND a working encoder — i.e.
   *  an offer is worth asking for. */
  webrtc: boolean;
  /** Video codecs the pod can produce, most-preferred first. */
  codecs: string[];
}

/** An ICE candidate as it crosses the wire — the browser's `RTCIceCandidateInit`
 *  shape, so a client can hand it straight to `addIceCandidate`. */
export interface PanelIceCandidate {
  candidate: string;
  sdpMid?: string | null;
  sdpMLineIndex?: number | null;
  usernameFragment?: string | null;
}

/** A server→client panel message. */
export type PanelMessage =
  | { type: "ready"; panelId: string; wire: string; viewport: { width: number; height: number }; capabilities?: PanelCapabilities }
  | { type: "frame"; data: string; format: "jpeg" | "png"; meta: { deviceWidth: number; deviceHeight: number }; at: number }
  | { type: "nav"; url: string; title: string }
  | { type: "closed"; reason: string }
  | { type: "error"; detail: string }
  | { type: "pong" }
  | { type: "offer"; sdp: string }
  | { type: "ice"; candidate: PanelIceCandidate | null }
  | { type: "transport"; transport: PanelTransport; reason: string };

/** Strip userinfo (`user:pass@`) from a url so a credential in a navigated URL
 *  never lands in a log line. Returns the input unchanged when it is not a
 *  parseable absolute url. This is a LOG-ONLY scrub. */
export function redactUrl(url: string): string {
  try {
    const u = new URL(url);
    if (u.username || u.password) {
      u.username = "";
      u.password = "";
      return u.toString();
    }
    return url;
  } catch {
    return url;
  }
}

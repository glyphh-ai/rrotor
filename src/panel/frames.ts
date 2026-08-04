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
 *   { type:"ready",  panelId, wire, viewport:{width,height} }  — hello on attach
 *   { type:"frame",  data, format, meta:{deviceWidth,deviceHeight}, at }
 *                                                — one screencast frame (data = base64)
 *   { type:"nav",    url, title }                — url/title changed
 *   { type:"closed", reason }                    — the panel/context tore down
 *   { type:"error",  detail }                    — a non-fatal problem
 *   { type:"pong" }                              — app-level keepalive reply
 *
 * Secret hygiene: a `nav` url could embed credentials (e.g. `https://u:p@host`).
 * {@link redactUrl} strips userinfo before the url is logged — NEVER before it
 * reaches the client (the client is the user watching the page; it needs the
 * real url) — logging is the only place it is scrubbed.
 */

/** Bump on any breaking change to the panel wire shapes. */
export const PANEL_WIRE_VERSION = "glyphh.panel/v1";

/** A server→client panel message. */
export type PanelMessage =
  | { type: "ready"; panelId: string; wire: string; viewport: { width: number; height: number } }
  | { type: "frame"; data: string; format: "jpeg" | "png"; meta: { deviceWidth: number; deviceHeight: number }; at: number }
  | { type: "nav"; url: string; title: string }
  | { type: "closed"; reason: string }
  | { type: "error"; detail: string }
  | { type: "pong" };

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

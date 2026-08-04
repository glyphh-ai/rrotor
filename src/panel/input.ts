/**
 * panel/input.ts — the PURE client-input → CDP-command mapping.
 *
 * The browser panel takes raw input events off the WS (mouse/key/wheel/resize)
 * and dispatches them into the pod's real browser via CDP. This module holds the
 * pure, side-effect-free translation: `InputEvent` (the wire shape the demo
 * client + app-web canvas send) → a `CdpCommand` (`method` + `params`) the
 * session layer feeds to `CDPSession.send()`. Keeping it pure makes it unit-
 * testable with no browser (the input tests fake nothing — they assert the
 * mapping), and keeps the coordinate/modifier/keycode conventions in ONE place.
 *
 * The wire event vocabulary (client → server), all coordinates in CSS px in the
 * panel's own viewport space (the client scales canvas→viewport before sending):
 *
 *   { type:"mouse", action:"move"|"down"|"up", x, y, button?, buttons?, mods? }
 *   { type:"wheel", x, y, dx, dy, mods? }
 *   { type:"key",   action:"down"|"up",       key, code?, text?, mods? }
 *   { type:"resize", width, height }
 *
 * `mods` is the CDP modifier bitmask (Alt=1, Ctrl=2, Meta=4, Shift=8); the client
 * assembles it from the DOM event. Buttons follow the CDP naming
 * ("none"|"left"|"middle"|"right"|"back"|"forward").
 *
 * `resize` maps to `Emulation.setDeviceMetricsOverride` (device-metric emulation,
 * NOT a real window resize — headless has no window) plus a screencast restart is
 * the session's job; here we only produce the metrics command.
 */

/** CDP mouse button names (`Input.dispatchMouseEvent.button`). */
export type MouseButton = "none" | "left" | "middle" | "right" | "back" | "forward";

/** The client→server input wire vocabulary. */
export type InputEvent =
  | { type: "mouse"; action: "move" | "down" | "up"; x: number; y: number; button?: MouseButton; buttons?: number; mods?: number; clickCount?: number }
  | { type: "wheel"; x: number; y: number; dx: number; dy: number; mods?: number }
  | { type: "key"; action: "down" | "up"; key?: string; code?: string; text?: string; mods?: number }
  | { type: "resize"; width: number; height: number };

/** A CDP command: the `method` and its `params`, ready for `CDPSession.send`. */
export interface CdpCommand {
  method: string;
  params: Record<string, unknown>;
}

const MOUSE_TYPE: Record<"move" | "down" | "up", string> = {
  move: "mouseMoved",
  down: "mousePressed",
  up: "mouseReleased",
};

/** Windows virtual-key codes for the NON-TEXT keys a panel must honor — the
 *  renderer's editing/navigation commands key off these, not `key`/`code`. */
const KEY_VIRTUAL_CODES: Record<string, number> = {
  Backspace: 8, Tab: 9, Enter: 13, Shift: 16, Control: 17, Alt: 18, Pause: 19,
  CapsLock: 20, Escape: 27, " ": 32, PageUp: 33, PageDown: 34, End: 35, Home: 36,
  ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Insert: 45, Delete: 46,
  Meta: 91, ContextMenu: 93,
};

const KEY_TYPE: Record<"down" | "up", string> = { down: "keyDown", up: "keyUp" };

const VALID_BUTTONS: ReadonlySet<string> = new Set(["none", "left", "middle", "right", "back", "forward"]);

/** Clamp a coordinate to a finite non-negative integer (defends against NaN/negatives from the wire). */
function coord(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? Math.max(0, Math.round(n)) : 0;
}

/** Coerce the CDP modifier bitmask to the 0–15 range. */
function mods(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? n & 0b1111 : 0;
}

function button(v: unknown): MouseButton {
  return typeof v === "string" && VALID_BUTTONS.has(v) ? (v as MouseButton) : "none";
}

/**
 * Map ONE input event to its CDP command, or `null` when the event is malformed
 * or is a `resize` (which the session handles specially via
 * {@link resizeMetrics}, not a screencast-stream command). Never throws — a bad
 * event is dropped, not fatal, so a noisy client can't take a panel down.
 */
export function toCdp(ev: InputEvent): CdpCommand | null {
  switch (ev?.type) {
    case "mouse": {
      const t = MOUSE_TYPE[ev.action];
      if (!t) return null;
      const btn = button(ev.button);
      return {
        method: "Input.dispatchMouseEvent",
        params: {
          type: t,
          x: coord(ev.x),
          y: coord(ev.y),
          button: btn,
          // `buttons` is the bitmask of currently-pressed buttons; default to the
          // single pressed button on down so drags register.
          buttons: Number.isFinite(ev.buttons) ? Number(ev.buttons) : btn === "left" && ev.action === "down" ? 1 : 0,
          modifiers: mods(ev.mods),
          clickCount: ev.action === "up" || ev.action === "down" ? Math.max(1, Number(ev.clickCount) || 1) : 0,
        },
      };
    }
    case "wheel":
      return {
        method: "Input.dispatchMouseEvent",
        params: {
          type: "mouseWheel",
          x: coord(ev.x),
          y: coord(ev.y),
          deltaX: Number.isFinite(ev.dx) ? Number(ev.dx) : 0,
          deltaY: Number.isFinite(ev.dy) ? Number(ev.dy) : 0,
          modifiers: mods(ev.mods),
        },
      };
    case "key": {
      const t = KEY_TYPE[ev.action];
      if (!t) return null;
      const params: Record<string, unknown> = { type: t, modifiers: mods(ev.mods) };
      if (typeof ev.key === "string" && ev.key) params.key = ev.key;
      if (typeof ev.code === "string" && ev.code) params.code = ev.code;
      // NON-TEXT keys (Backspace, Delete, arrows, Enter…) do NOTHING in Chromium
      // without a windowsVirtualKeyCode — `key`/`code` alone are ignored by the
      // renderer's editing commands. Printables never needed it because `text`
      // drives insertion, which is why only these keys appeared broken.
      const vk = typeof ev.key === "string" ? KEY_VIRTUAL_CODES[ev.key] : undefined;
      if (vk !== undefined) {
        params.windowsVirtualKeyCode = vk;
        params.nativeVirtualKeyCode = vk;
      }
      // `text` makes a keyDown produce a character (CDP inserts it); only on down.
      // Enter's character is \r — without it a focused field gets the keydown but
      // forms don't submit and textareas don't break lines.
      if (ev.action === "down") {
        if (typeof ev.text === "string" && ev.text) {
          params.text = ev.text;
          params.unmodifiedText = ev.text;
        } else if (ev.key === "Enter") {
          params.text = "\r";
          params.unmodifiedText = "\r";
        }
      }
      return { method: "Input.dispatchKeyEvent", params };
    }
    case "resize":
      return null; // handled by resizeMetrics — no direct CDP command here
    default:
      return null;
  }
}

/** Clamp a viewport dimension to a sane bounded integer (px). Guards against a
 *  client sending 0 / NaN / an absurd size that would OOM the compositor. */
export function clampDim(v: unknown, fallback: number, max = 4096): number {
  const n = Math.round(Number(v));
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.min(max, Math.max(1, n));
}

/** The `Emulation.setDeviceMetricsOverride` command for a resize event. Separate
 *  from {@link toCdp} because the session must also restart the screencast at the
 *  new size — this only builds the metrics command. */
export function resizeMetrics(width: number, height: number, deviceScaleFactor = 1): CdpCommand {
  return {
    method: "Emulation.setDeviceMetricsOverride",
    params: {
      width: clampDim(width, 1),
      height: clampDim(height, 1),
      deviceScaleFactor,
      mobile: false,
    },
  };
}

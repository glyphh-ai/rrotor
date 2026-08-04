/**
 * The pure input-event → CDP-command mapping (panel/input.ts) + the panel wire
 * helpers (panel/frames.ts). These are the load-bearing pure seams: what the
 * client sends must land as the right CDP dispatch, coordinates/modifiers must
 * be coerced, and a nav url must be scrubbed of credentials before it is logged.
 */

import { describe, it, expect } from "vitest";

import { toCdp, resizeMetrics, clampDim } from "../../src/panel/input.js";
import type { InputEvent } from "../../src/panel/input.js";
import { redactUrl, PANEL_WIRE_VERSION } from "../../src/panel/frames.js";

describe("toCdp — mouse", () => {
  it("maps move/down/up to the CDP mouse types with coerced coords + mods", () => {
    expect(toCdp({ type: "mouse", action: "move", x: 10.6, y: 20.2, mods: 8 })).toEqual({
      method: "Input.dispatchMouseEvent",
      params: { type: "mouseMoved", x: 11, y: 20, button: "none", buttons: 0, modifiers: 8, clickCount: 0 },
    });
    const down = toCdp({ type: "mouse", action: "down", x: 5, y: 5, button: "left" });
    expect(down?.params).toMatchObject({ type: "mousePressed", button: "left", buttons: 1, clickCount: 1 });
    const up = toCdp({ type: "mouse", action: "up", x: 5, y: 5, button: "left", clickCount: 2 });
    expect(up?.params).toMatchObject({ type: "mouseReleased", button: "left", clickCount: 2 });
  });

  it("clamps negative/NaN coords to 0 and rejects an unknown button", () => {
    const c = toCdp({ type: "mouse", action: "move", x: -3, y: NaN as unknown as number, button: "wat" as never });
    expect(c?.params).toMatchObject({ x: 0, y: 0, button: "none" });
  });

  it("masks out-of-range modifier bits", () => {
    const c = toCdp({ type: "mouse", action: "move", x: 0, y: 0, mods: 0xff });
    expect(c?.params.modifiers).toBe(0b1111);
  });
});

describe("toCdp — wheel", () => {
  it("maps to a mouseWheel with deltas", () => {
    expect(toCdp({ type: "wheel", x: 1, y: 2, dx: -4, dy: 30 })).toEqual({
      method: "Input.dispatchMouseEvent",
      params: { type: "mouseWheel", x: 1, y: 2, deltaX: -4, deltaY: 30, modifiers: 0 },
    });
  });
  it("defaults bad deltas to 0", () => {
    const c = toCdp({ type: "wheel", x: 0, y: 0, dx: NaN as unknown as number, dy: undefined as unknown as number });
    expect(c?.params).toMatchObject({ deltaX: 0, deltaY: 0 });
  });
});

describe("toCdp — key", () => {
  it("keyDown carries text (character insert); keyUp does not", () => {
    const down = toCdp({ type: "key", action: "down", key: "a", code: "KeyA", text: "a", mods: 8 });
    expect(down).toEqual({
      method: "Input.dispatchKeyEvent",
      params: { type: "keyDown", modifiers: 8, key: "a", code: "KeyA", text: "a", unmodifiedText: "a" },
    });
    const up = toCdp({ type: "key", action: "up", key: "a", code: "KeyA", text: "a" });
    expect(up?.params).not.toHaveProperty("text");
    expect(up?.params.type).toBe("keyUp");
  });
  it("Enter carries \\r + its virtual key code (forms must submit)", () => {
    const c = toCdp({ type: "key", action: "down", key: "Enter", code: "Enter" });
    expect(c?.params).toMatchObject({
      type: "keyDown", key: "Enter", text: "\r", unmodifiedText: "\r",
      windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
    });
  });

  it("editing keys carry their virtual key code (Backspace must delete)", () => {
    const down = toCdp({ type: "key", action: "down", key: "Backspace", code: "Backspace" });
    expect(down?.params).toMatchObject({ type: "keyDown", key: "Backspace", windowsVirtualKeyCode: 8 });
    expect(down?.params).not.toHaveProperty("text");
    const up = toCdp({ type: "key", action: "up", key: "Backspace", code: "Backspace" });
    expect(up?.params).toMatchObject({ type: "keyUp", windowsVirtualKeyCode: 8 });
    for (const [key, vk] of [["Delete", 46], ["ArrowLeft", 37], ["Tab", 9], ["Escape", 27]] as const) {
      expect(toCdp({ type: "key", action: "down", key, code: String(key) })?.params).toMatchObject({ windowsVirtualKeyCode: vk });
    }
  });

  it("printables carry NO virtual key code (text drives insertion, unchanged)", () => {
    const c = toCdp({ type: "key", action: "down", key: "a", code: "KeyA", text: "a" });
    expect(c?.params).toMatchObject({ type: "keyDown", text: "a" });
    expect(c?.params).not.toHaveProperty("windowsVirtualKeyCode");
  });
});

describe("toCdp — rejects", () => {
  it("returns null for resize and for malformed events", () => {
    expect(toCdp({ type: "resize", width: 800, height: 600 })).toBeNull();
    expect(toCdp({ type: "mouse", action: "wat" as never, x: 0, y: 0 })).toBeNull();
    expect(toCdp({ type: "key", action: "wat" as never })).toBeNull();
    expect(toCdp({ type: "nope" } as unknown as InputEvent)).toBeNull();
  });
});

describe("resizeMetrics + clampDim", () => {
  it("builds a setDeviceMetricsOverride with clamped dims", () => {
    expect(resizeMetrics(800, 600)).toEqual({
      method: "Emulation.setDeviceMetricsOverride",
      params: { width: 800, height: 600, deviceScaleFactor: 1, mobile: false },
    });
  });
  it("clampDim falls back on bad input and caps the max", () => {
    expect(clampDim(0, 720)).toBe(720);
    expect(clampDim(NaN, 720)).toBe(720);
    expect(clampDim(99999, 720, 4096)).toBe(4096);
    expect(clampDim(500, 720)).toBe(500);
  });
});

describe("redactUrl (log-only)", () => {
  it("strips userinfo but keeps the rest", () => {
    expect(redactUrl("https://user:pass@example.com/path?q=1")).toBe("https://example.com/path?q=1");
  });
  it("leaves credential-free urls and non-urls untouched", () => {
    expect(redactUrl("https://example.com/x")).toBe("https://example.com/x");
    expect(redactUrl("not a url")).toBe("not a url");
  });
  it("exposes the wire version", () => {
    expect(PANEL_WIRE_VERSION).toBe("glyphh.panel/v2");
  });
});

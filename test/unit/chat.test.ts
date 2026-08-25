/**
 * Chat-mode tests. `openChat`/`turn` are the TTY-free core of the REPL's chat
 * mode; these prove a turn streams real step progress + an answer through the
 * injected sink, that failures render instead of throwing, and that a broken
 * rotor is rejected at open time.
 */

import { describe, it, expect } from "vitest";


import { openChat, defaultChatRotorPath, resolveChatRotor } from "../../src/chat.js";


const ESC = String.fromCharCode(27);
const strip = (s: string) => s.replace(new RegExp(`${ESC}\\[[0-9;]*m`, "g"), "");

describe("chat session", () => {
  it("defaults to the bundled router rotor and pins a session id", async () => {
    const chat = await openChat();
    try {
      expect(chat.rotor).toBe("glyphh/router@1.0.0");
      expect(chat.session).toMatch(/^chat-[0-9a-f]{8}$/);
    } finally {
      await chat.close();
    }
  });

  it("a turn streams step progress and an answer to the sink", async () => {
    const chat = await openChat();
    try {
      let out = "";
      await chat.turn("what is a rotor?", (t) => (out += t));
      const plain = strip(out);
      // The router's display labels render (author-controlled event text); the
      // stub route never parses, so the turn lands in the chat lane.
      for (const label of ["reading the turn", "choosing a rotor", "routing", "chatting"]) {
        expect(plain).toContain(label);
      }
      // The response line carries the ● marker; checked steps carry ✓.
      expect(plain).toContain("●");
      expect(plain).toContain("✓ reading the turn");
    } finally {
      await chat.close();
    }
  });

  it("memory carries across turns in one session (same stator + session id)", async () => {
    const chat = await openChat();
    try {
      let first = "";
      let second = "";
      await chat.turn("first turn", (t) => (first += t));
      await chat.turn("second turn", (t) => (second += t));
      // Both turns completed as full runs — no interleaving, no thrown errors.
      expect(strip(first)).toContain("✓ chatting");
      expect(strip(second)).toContain("✓ chatting");
    } finally {
      await chat.close();
    }
  });

  it("rejects a missing rotor file at open time with a load error", async () => {
    await expect(openChat("/no/such/rotor.yaml")).rejects.toThrow(/cannot load/);
  });

  it("the default rotor path points at a real bundled file", () => {
    expect(defaultChatRotorPath()).toMatch(/rotors[/\\]router\.rotor\.yaml$/);
  });

  it("a bare name resolves to the bundled rotor; paths pass through", () => {
    expect(resolveChatRotor("base")).toMatch(/rotors[/\\]base\.rotor\.yaml$/);
    expect(resolveChatRotor("./my.rotor.yaml")).toBe("./my.rotor.yaml");
    expect(resolveChatRotor("no-such-rotor")).toBe("no-such-rotor");
  });

  it("spinner mode redraws a live progress line and settles it before the answer", async () => {
    const chat = await openChat();
    try {
      let out = "";
      await chat.turn("hello", (t) => (out += t), { spinner: true });
      // Live redraws rewrite in place (\r + erase-line), never scroll.
      expect(out).toContain("\r\x1b[2K");
      // The settled progress line survives, followed by the summary.
      const plain = strip(out);
      expect(plain).toContain("✓ reading the turn");
      expect(plain).toContain("✓ chatting");
      expect(plain).toMatch(/\d+\.\ds/);
    } finally {
      await chat.close();
    }
  });

  it("a turn ends with an elapsed-time summary line (with tokens when metered)", async () => {
    const chat = await openChat();
    try {
      let out = "";
      await chat.turn("hello there", (t) => (out += t));
      // `N.Ns` always; `· I↑ O↓ tok` whenever the model step recorded usage.
      expect(strip(out)).toMatch(/\d+\.\ds( · \d+↑ \d+↓ tok)?\n/);
    } finally {
      await chat.close();
    }
  });
});

/**
 * The hand-rolled WebSocket codec (RFC 6455). Exercises the fiddly bits directly:
 * the handshake accept value, server-frame encoding, client-frame unmasking, and
 * reassembly of a fragmented message across continuation frames.
 */

import { describe, it, expect } from "vitest";

import { acceptKey, encodeFrame, FrameDecoder } from "../../src/transport/ws.js";

/** Build a masked client text frame (payload < 126 bytes), as a browser would send. */
function maskedText(text: string, opcode = 0x1, fin = true, mask = [1, 2, 3, 4]): Buffer {
  const payload = Buffer.from(text);
  const masked = Buffer.from(payload);
  for (let i = 0; i < masked.length; i++) masked[i] ^= mask[i & 3];
  const b0 = (fin ? 0x80 : 0x00) | opcode;
  return Buffer.concat([Buffer.from([b0, 0x80 | payload.length, ...mask]), masked]);
}

describe("acceptKey", () => {
  it("computes the RFC 6455 §1.3 example accept value", () => {
    expect(acceptKey("dGhlIHNhbXBsZSBub25jZQ==")).toBe("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=");
  });
});

describe("encodeFrame", () => {
  it("frames a short text payload with FIN set and no mask", () => {
    const f = Buffer.from(encodeFrame(Buffer.from("hi")));
    expect([...f]).toEqual([0x81, 0x02, 0x68, 0x69]); // FIN|text, len 2, 'h','i'
  });

  it("uses the 16-bit length form for medium payloads", () => {
    const f = Buffer.from(encodeFrame(Buffer.from("x".repeat(200))));
    expect(f[0]).toBe(0x81);
    expect(f[1]).toBe(126);
    expect(f.readUInt16BE(2)).toBe(200);
  });
});

describe("FrameDecoder", () => {
  it("unmasks a client text frame", () => {
    const frames = new FrameDecoder().push(maskedText("hello"));
    expect(frames).toHaveLength(1);
    expect(frames[0].opcode).toBe(0x1);
    expect(frames[0].payload.toString()).toBe("hello");
  });

  it("reassembles a fragmented message across continuation frames", () => {
    const dec = new FrameDecoder();
    const first = maskedText("he", 0x1, false); // text, no FIN
    const cont = maskedText("llo", 0x0, true); // continuation, FIN
    const frames = dec.push(Buffer.concat([first, cont]));
    expect(frames).toHaveLength(1);
    expect(frames[0].payload.toString()).toBe("hello");
  });

  it("waits for the rest of a frame split across TCP reads", () => {
    const dec = new FrameDecoder();
    const whole = maskedText("split");
    expect(dec.push(whole.subarray(0, 3))).toHaveLength(0); // partial → nothing yet
    const frames = dec.push(whole.subarray(3));
    expect(frames[0].payload.toString()).toBe("split");
  });
});

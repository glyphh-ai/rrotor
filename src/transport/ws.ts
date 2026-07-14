/**
 * transport/ws.ts — a minimal, dependency-free WebSocket transport (RFC 6455).
 *
 * WebSocket layers *bidirectional* control on the same {@link WireEvent} model SSE
 * streams: the client sends JSON control messages over one socket and receives the
 * event stream back over the same socket. Kept deliberately small — text frames,
 * ping/pong, and close, which is all a JSON control/event channel needs — so the
 * runtime stays "node:http only" (server.ts). Larger payloads are handled via
 * continuation frames; binary frames are rejected (the protocol is JSON text).
 *
 * Control messages (client → server), each a JSON text frame:
 *   { "type": "turn",   "rotor": <yaml|object>, "inputs": {…}, "session": "…" }
 *   { "type": "resume", "run_id": "…", "payload": {…} }        // HITL approval
 *   { "type": "attach", "run_id": "…", "from": <seq> }         // durable reconnect
 *   { "type": "ping" }                                          // app-level keepalive
 * The server replies with `open → step* → terminal → done` WireEvent frames (plus a
 * one-time `{"type":"ready"}` hello on connect, and `{"type":"pong"}` for app pings).
 */

import { createHash } from "node:crypto";
import type * as http from "node:http";
import type { Duplex } from "node:stream";

import { parseRotor, validateRotor } from "../parser/index.js";
import { log } from "../obs/logger.js";
import { WIRE_VERSION, type WireEvent } from "./events.js";
import { executeToEvents, resumeToEvents, buildReplay, type StreamContext } from "./run.js";
import type { Stator } from "../exec/store.js";
import type { DrainPlugin } from "../plugins/interfaces.js";
import type { RotorDocument } from "../types.js";

/** RFC 6455 §4.2.2 magic GUID for the handshake accept value. */
const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/** The `Sec-WebSocket-Accept` value proving the server spoke the protocol. */
export function acceptKey(key: string): string {
  return createHash("sha1").update(key + WS_GUID).digest("base64");
}

// ── frame codec ──────────────────────────────────────────────────────────────

const OP_TEXT = 0x1;
const OP_CLOSE = 0x8;
const OP_PING = 0x9;
const OP_PONG = 0xa;

/** Encode a server frame (never masked, always FIN). Returns a Uint8Array — the
 *  type `Duplex.write` accepts — sidestepping the Buffer<ArrayBuffer> generic. */
export function encodeFrame(payload: Buffer, opcode = OP_TEXT): Uint8Array {
  const len = payload.length;
  let header: Uint8Array;
  if (len < 126) {
    header = Uint8Array.from([0x80 | opcode, len]);
  } else if (len < 65536) {
    header = Uint8Array.from([0x80 | opcode, 126, (len >> 8) & 0xff, len & 0xff]);
  } else {
    const h = Buffer.alloc(10);
    h[0] = 0x80 | opcode;
    h[1] = 127;
    h.writeBigUInt64BE(BigInt(len), 2);
    header = h;
  }
  return Buffer.concat([header, payload]);
}

/** A decoded inbound message: an opcode + its (unmasked) payload. */
export interface DecodedFrame {
  opcode: number;
  payload: Buffer;
}

/**
 * Stateful decoder: feed it socket chunks, get back complete frames. Buffers partial
 * frames across TCP reads and reassembles continuation frames (opcode 0x0) into the
 * message they began. Client frames MUST be masked (RFC 6455 §5.1); we unmask them.
 */
export class FrameDecoder {
  private buf: Buffer<ArrayBufferLike> = Buffer.alloc(0);
  private fragOpcode = 0;
  private fragParts: Buffer[] = [];

  push(chunk: Buffer): DecodedFrame[] {
    this.buf = this.buf.length ? Buffer.concat([this.buf, chunk]) : chunk;
    const out: DecodedFrame[] = [];
    for (;;) {
      if (this.buf.length < 2) break;
      const b0 = this.buf[0];
      const b1 = this.buf[1];
      const fin = (b0 & 0x80) !== 0;
      const opcode = b0 & 0x0f;
      const masked = (b1 & 0x80) !== 0;
      let len = b1 & 0x7f;
      let offset = 2;
      if (len === 126) {
        if (this.buf.length < offset + 2) break;
        len = this.buf.readUInt16BE(offset);
        offset += 2;
      } else if (len === 127) {
        if (this.buf.length < offset + 8) break;
        len = Number(this.buf.readBigUInt64BE(offset));
        offset += 8;
      }
      const maskLen = masked ? 4 : 0;
      if (this.buf.length < offset + maskLen + len) break; // wait for the rest
      const mask = masked ? this.buf.subarray(offset, offset + 4) : undefined;
      offset += maskLen;
      const payload = Buffer.from(this.buf.subarray(offset, offset + len));
      if (mask) for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i & 3];
      this.buf = this.buf.subarray(offset + len);

      if (opcode === 0x0 || (fin === false && (opcode === OP_TEXT))) {
        // Fragmentation: 0x0 continues; a non-FIN text starts a fragmented message.
        if (opcode !== 0x0) this.fragOpcode = opcode;
        this.fragParts.push(payload);
        if (fin) {
          out.push({ opcode: this.fragOpcode || OP_TEXT, payload: Buffer.concat(this.fragParts) });
          this.fragParts = [];
          this.fragOpcode = 0;
        }
      } else {
        out.push({ opcode, payload });
      }
    }
    return out;
  }
}

// ── connection handling ──────────────────────────────────────────────────────

/** What a WS connection needs to run rotors — same shape as the SSE context, minus
 *  the per-request session (a session id arrives per control message). */
export interface WsDeps {
  store: Stator;
  drain: DrainPlugin;
  workspace: string;
}

interface TurnMsg { type: "turn"; rotor: unknown; inputs?: Record<string, unknown>; session?: string }
interface ResumeMsg { type: "resume"; run_id: string; payload?: Record<string, unknown> }
interface AttachMsg { type: "attach"; run_id: string; from?: number }
interface PingMsg { type: "ping" }
type ControlMsg = TurnMsg | ResumeMsg | AttachMsg | PingMsg;

/** Coerce the `rotor` control field into a document (inline object or YAML/JSON source). */
function coerceRotor(rotor: unknown): RotorDocument {
  if (typeof rotor === "string") return parseRotor(rotor, rotor.trimStart().startsWith("{") ? "json" : "yaml");
  return rotor as RotorDocument;
}

/**
 * Attach a WebSocket endpoint at `path` to a running HTTP server. Handles the
 * upgrade handshake, then serves control messages over the connection.
 */
export function attachWebSocket(server: http.Server, deps: WsDeps, path = "/ws"): void {
  server.on("upgrade", (req: http.IncomingMessage, socket: Duplex) => {
    if ((req.url ?? "").split("?", 1)[0] !== path) {
      socket.destroy();
      return;
    }
    const key = req.headers["sec-websocket-key"];
    if (typeof key !== "string") {
      socket.destroy();
      return;
    }
    socket.write(
      "HTTP/1.1 101 Switching Protocols\r\n" +
        "Upgrade: websocket\r\n" +
        "Connection: Upgrade\r\n" +
        `Sec-WebSocket-Accept: ${acceptKey(key)}\r\n\r\n`,
    );
    serve(socket, deps);
  });
}

/** Serve one upgraded connection: decode frames, run control messages in order. */
function serve(socket: Duplex, deps: WsDeps): void {
  const decoder = new FrameDecoder();
  const send = (ev: WireEvent | { type: string }) => {
    if (!socket.destroyed) socket.write(encodeFrame(Buffer.from(JSON.stringify(ev))));
  };
  // Process control messages strictly in order — one run streams fully before the
  // next message is handled, so a client's turns never interleave on the socket.
  let chain: Promise<void> = Promise.resolve();

  send({ type: "ready", wire: WIRE_VERSION } as unknown as { type: string });

  socket.on("data", (chunk: Buffer) => {
    let frames: DecodedFrame[];
    try {
      frames = decoder.push(chunk);
    } catch (err) {
      log.warn("ws frame decode failed", { detail: (err as Error).message });
      socket.end(encodeFrame(Buffer.alloc(0), OP_CLOSE));
      return;
    }
    for (const f of frames) {
      if (f.opcode === OP_CLOSE) {
        socket.end(encodeFrame(Buffer.alloc(0), OP_CLOSE));
        return;
      }
      if (f.opcode === OP_PING) {
        socket.write(encodeFrame(f.payload, OP_PONG));
        continue;
      }
      if (f.opcode === OP_PONG) continue;
      if (f.opcode !== OP_TEXT) {
        // Binary is not part of the JSON control protocol.
        send({ type: "error", detail: "binary frames are not supported" } as unknown as { type: string });
        continue;
      }
      chain = chain.then(() => onMessage(f.payload.toString("utf8"), deps, send));
    }
  });

  socket.on("error", () => socket.destroy());
}

/** Handle one JSON control message. Errors are reported as a control frame, never thrown. */
async function onMessage(text: string, deps: WsDeps, send: (ev: WireEvent | { type: string }) => void): Promise<void> {
  let msg: ControlMsg;
  try {
    msg = JSON.parse(text) as ControlMsg;
  } catch {
    send({ type: "error", detail: "control message must be JSON" } as unknown as { type: string });
    return;
  }
  try {
    switch (msg.type) {
      case "ping":
        send({ type: "pong" } as unknown as { type: string });
        return;
      case "turn": {
        const doc = coerceRotor(msg.rotor);
        const { valid, errors } = validateRotor(doc);
        if (!valid) {
          send({ type: "error", detail: "invalid rotor", errors } as unknown as { type: string });
          return;
        }
        const ctx: StreamContext = { store: deps.store, drain: deps.drain, workspace: deps.workspace, session: msg.session };
        await executeToEvents(doc, msg.inputs ?? {}, ctx, send);
        return;
      }
      case "resume": {
        const ctx: StreamContext = { store: deps.store, drain: deps.drain, workspace: deps.workspace };
        const ok = await resumeToEvents(msg.run_id, msg.payload ?? {}, ctx, send);
        if (!ok) send({ type: "error", detail: `no interrupted run ${msg.run_id}` } as unknown as { type: string });
        return;
      }
      case "attach": {
        const events = await buildReplay(msg.run_id, msg.from ?? -1, deps.store);
        if (!events) {
          send({ type: "error", detail: `no run ${msg.run_id}` } as unknown as { type: string });
          return;
        }
        for (const ev of events) send(ev);
        return;
      }
      default:
        send({ type: "error", detail: "unknown control type" } as unknown as { type: string });
    }
  } catch (err) {
    log.error("ws control error", { detail: (err as Error).message });
    send({ type: "error", detail: (err as Error).message } as unknown as { type: string });
  }
}

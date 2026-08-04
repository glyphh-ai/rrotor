/**
 * TerminalSession + TerminalRegistry with the pty FAKED (no node-pty): a session
 * fans pty output out as base64 `data`, feeds `input`/`resize` into the pty, emits
 * `exit` on process end, and `close()` kills the pty (no orphan). The registry caps
 * concurrency and clamps a client cwd UNDER the session sandbox (no escape).
 */

import { describe, it, expect } from "vitest";
import { resolve, sep } from "node:path";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { TerminalSession, TerminalRegistry, TerminalAtCapacity } from "../../src/panel/terminal.js";
import type { Pty, TerminalDriver, SpawnPtyOptions } from "../../src/panel/terminal-driver.js";

/** A fake pty: records writes/resizes/kill, lets a test push output + fire exit. */
class FakePty implements Pty {
  readonly pid = 4242;
  readonly writes: string[] = [];
  readonly resizes: Array<{ cols: number; rows: number }> = [];
  killed = false;
  private dataCb: ((d: string) => void) | null = null;
  private exitCb: ((e: { exitCode: number; signal?: number }) => void) | null = null;
  write(data: string): void { this.writes.push(data); }
  resize(cols: number, rows: number): void { this.resizes.push({ cols, rows }); }
  onData(cb: (d: string) => void): void { this.dataCb = cb; }
  onExit(cb: (e: { exitCode: number; signal?: number }) => void): void { this.exitCb = cb; }
  kill(): void { this.killed = true; }
  // test hooks
  emit(d: string): void { this.dataCb?.(d); }
  fireExit(code: number, signal?: number): void { this.exitCb?.({ exitCode: code, ...(signal !== undefined ? { signal } : {}) }); }
}

class FakeDriver implements TerminalDriver {
  readonly spawned: Array<{ opts: SpawnPtyOptions; pty: FakePty }> = [];
  spawn(opts: SpawnPtyOptions): Promise<Pty> {
    const pty = new FakePty();
    this.spawned.push({ opts, pty });
    return Promise.resolve(pty);
  }
  shutdown(): Promise<void> { return Promise.resolve(); }
}

describe("TerminalSession", () => {
  it("greets ready, streams pty output as base64 data, feeds input + resize into the pty", () => {
    const pty = new FakePty();
    const s = new TerminalSession({ panelId: "trm-1", pty, cols: 80, rows: 24 });
    const msgs: Array<Record<string, unknown>> = [];
    s.subscribe((m) => msgs.push(m as Record<string, unknown>));
    expect(msgs[0]).toMatchObject({ type: "ready", panelId: "trm-1", wire: "glyphh.terminal/v1", cols: 80, rows: 24 });

    pty.emit("hello\r\n");
    const data = msgs.find((m) => m.type === "data") as { data: string };
    expect(Buffer.from(data.data, "base64").toString("utf8")).toBe("hello\r\n");

    s.dispatch({ type: "input", data: "ls\r" });
    expect(pty.writes).toContain("ls\r");
    s.dispatch({ type: "resize", cols: 120, rows: 40 });
    expect(pty.resizes.at(-1)).toEqual({ cols: 120, rows: 40 });
  });

  it("emits exit when the pty ends and drops further input", () => {
    const pty = new FakePty();
    const s = new TerminalSession({ panelId: "trm-2", pty, cols: 80, rows: 24 });
    const msgs: Array<Record<string, unknown>> = [];
    s.subscribe((m) => msgs.push(m as Record<string, unknown>));
    pty.fireExit(0);
    expect(msgs.some((m) => m.type === "exit" && m.code === 0)).toBe(true);
    // input after exit is a no-op (status closed)
    s.dispatch({ type: "input", data: "should-be-ignored" });
    expect(pty.writes).not.toContain("should-be-ignored");
  });

  it("close() kills the pty (no orphan shell) — idempotent", () => {
    const pty = new FakePty();
    const s = new TerminalSession({ panelId: "trm-3", pty, cols: 80, rows: 24 });
    s.close();
    expect(pty.killed).toBe(true);
    // second close does not throw
    s.close();
  });
});

describe("TerminalRegistry", () => {
  const sandbox = mkdtempSync(join(tmpdir(), "trm-sbx-"));

  it("spawns a pty in the SESSION sandbox and clamps a client cwd under it", async () => {
    const driver = new FakeDriver();
    const reg = new TerminalRegistry(driver, 16, sandbox);
    const { panelId } = await reg.open({ sessionId: "sess-a", cwd: "sub/dir", cols: 100, rows: 30 });
    expect(panelId).toMatch(/^trm-/);
    const spawn = driver.spawned[0].opts;
    const expectedRoot = resolve(sandbox, "sessions", "sess-a", "workspace");
    expect(spawn.cwd).toBe(resolve(expectedRoot, "sub/dir"));
    expect(spawn.cwd.startsWith(expectedRoot + sep)).toBe(true);
    expect(spawn.cols).toBe(100);
    expect(spawn.rows).toBe(30);
  });

  it("a `../..` escape is clamped back to the session sandbox root", async () => {
    const driver = new FakeDriver();
    const reg = new TerminalRegistry(driver, 16, sandbox);
    await reg.open({ sessionId: "sess-b", cwd: "../../../../etc" });
    const spawn = driver.spawned[0].opts;
    const root = resolve(sandbox, "sessions", "sess-b", "workspace");
    expect(spawn.cwd).toBe(root); // escape refused → root
  });

  it("caps concurrency and close() kills + forgets", async () => {
    const driver = new FakeDriver();
    const reg = new TerminalRegistry(driver, 1, sandbox);
    const { panelId } = await reg.open({ sessionId: "s" });
    await expect(reg.open({ sessionId: "s" })).rejects.toBeInstanceOf(TerminalAtCapacity);
    expect(reg.close(panelId)).toBe(true);
    expect(driver.spawned[0].pty.killed).toBe(true);
    expect(reg.close(panelId)).toBe(false); // gone
    // capacity freed
    await expect(reg.open({ sessionId: "s" })).resolves.toBeTruthy();
  });

  it("closeAll kills every pty (no orphan shells on shutdown)", async () => {
    const driver = new FakeDriver();
    const reg = new TerminalRegistry(driver, 16, sandbox);
    await reg.open({ sessionId: "s1" });
    await reg.open({ sessionId: "s2" });
    await reg.closeAll();
    expect(driver.spawned.every((s) => s.pty.killed)).toBe(true);
    expect(reg.count()).toBe(0);
  });
});

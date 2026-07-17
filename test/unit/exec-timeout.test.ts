/**
 * shell.bash termination guarantees. A tool step must NEVER hang the run: a
 * timed-out command kills its whole process GROUP (a bash wrapper's
 * grandchildren — a spawned clock, a server — would otherwise survive, hold
 * the stdio pipes open, and stall the 'close' event forever), and the promise
 * resolves on recorded facts even if an escapee lingers.
 */

import { describe, it, expect } from "vitest";
import { tmpdir } from "node:os";

import { execPack } from "../../src/tools/exec.js";

const bash = (timeoutMs: number) => {
  const pack = execPack({ root: tmpdir(), timeoutMs });
  return pack.tools.find((t) => t.name === "shell.bash")!;
};

describe("shell.bash timeout", () => {
  it("a never-exiting pipeline with a lingering grandchild times out and resolves", async () => {
    const started = Date.now();
    // The child (`node` running an empty interval) never exits and never
    // matches, so bash blocks on the pipeline — the shape of the clock hang.
    const r = (await bash(300).handler({
      command: `node -e "setInterval(() => {}, 1000)" | grep -q NEVER_MATCHES`,
    })) as { timed_out: boolean; exit_code: number };
    expect(r.timed_out).toBe(true);
    expect(r.exit_code).toBe(-1);
    expect(Date.now() - started).toBeLessThan(3000);
  });

  it("a fast command is untouched by the group-kill plumbing", async () => {
    const r = (await bash(5000).handler({ command: "echo ok" })) as { stdout: string; exit_code: number; timed_out: boolean };
    expect(r.stdout.trim()).toBe("ok");
    expect(r.exit_code).toBe(0);
    expect(r.timed_out).toBe(false);
  });
});

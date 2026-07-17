/**
 * `exec` tool pack — `shell.bash`, the universal escape hatch. `find`, `ripgrep`,
 * `sed`, `git`, `curl` … are all just bash invocations, so a dozen first-class tools
 * plus this one cover the entire OS long tail without hand-writing hundreds of
 * handlers (docs/tools.md).
 *
 * It is `external` effect (bash can do anything) behind the heaviest grant
 * (`shell.exec`), so a rotor only gets it in "code" mode — never in chat/co-work.
 * Output is captured, bounded, and the command runs in the sandbox `root` with a
 * timeout. Effectful → the `tool` step MUST set `idempotency: auto` so replay does
 * not re-run it.
 */

import { spawn } from "node:child_process";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

export interface ExecOptions {
  root: string;
  /** Hard wall-clock cap per command (ms). Default 120_000. */
  timeoutMs?: number;
  /** Max captured stdout/stderr bytes each (bounds tokens). Default 100_000. */
  maxOutput?: number;
}

function runBash(cmd: string, cwd: string, timeoutMs: number, maxOutput: number): Promise<{ stdout: string; stderr: string; exit_code: number; timed_out: boolean }> {
  return new Promise((resolvePromise) => {
    // detached: the child leads its own PROCESS GROUP, so the timeout can kill
    // the whole tree — a bash wrapper's grandchildren (a spawned server, an
    // infinite clock) would otherwise survive the kill, keep the stdio pipes
    // open, and hang this promise forever ('close' waits on the pipes).
    const child = spawn("bash", ["-c", cmd], { cwd, detached: true });
    let out = "";
    let err = "";
    let timedOut = false;
    let settled = false;
    const finish = (r: { stdout: string; stderr: string; exit_code: number; timed_out: boolean }): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolvePromise(r);
    };
    const killTree = (): void => {
      try {
        if (child.pid) process.kill(-child.pid, "SIGKILL"); // the whole group
        else child.kill("SIGKILL");
      } catch {
        child.kill("SIGKILL");
      }
    };
    const timer = setTimeout(() => {
      timedOut = true;
      killTree();
      // Belt over braces: even if an escapee still holds a pipe open, resolve
      // on the recorded facts — a tool step must never hang the run.
      setTimeout(() => finish({ stdout: out.slice(0, maxOutput), stderr: err.slice(0, maxOutput), exit_code: -1, timed_out: true }), 1000).unref?.();
    }, timeoutMs);
    child.stdout.on("data", (d: Buffer) => {
      if (out.length < maxOutput) out += d.toString("utf8");
    });
    child.stderr.on("data", (d: Buffer) => {
      if (err.length < maxOutput) err += d.toString("utf8");
    });
    child.on("close", (code) => {
      finish({
        stdout: out.slice(0, maxOutput),
        stderr: err.slice(0, maxOutput),
        exit_code: code ?? -1,
        timed_out: timedOut,
      });
    });
    // 'exit' fires when the process dies even while orphans hold the pipes.
    child.on("exit", (code) => {
      setTimeout(() => finish({ stdout: out.slice(0, maxOutput), stderr: err.slice(0, maxOutput), exit_code: code ?? -1, timed_out: timedOut }), 50).unref?.();
    });
    child.on("error", (e) => {
      finish({ stdout: out, stderr: String(e.message), exit_code: -1, timed_out: timedOut });
    });
  });
}

export function execPack(opts: ExecOptions): ToolPack {
  const { root } = opts;
  const timeoutMs = opts.timeoutMs ?? 120_000;
  const maxOutput = opts.maxOutput ?? 100_000;

  const tools: ToolSpec[] = [
    {
      name: "shell.bash",
      version: 1,
      description: "Run a bash command in the workspace root. Returns stdout, stderr, exit_code. Bounded + timed out.",
      effect: "external",
      grants: ["shell.exec"],
      input: { type: "object", properties: { command: { type: "string" } }, required: ["command"] },
      output: {
        type: "object",
        properties: { stdout: { type: "string" }, stderr: { type: "string" }, exit_code: { type: "number" }, timed_out: { type: "boolean" } },
      },
      handler: async (args: Row) => {
        const cmd = String(args.command ?? "").trim();
        if (!cmd) throw new RotorError("E_MISSING_INPUT", "shell.bash requires a `command`");
        return runBash(cmd, root, timeoutMs, maxOutput);
      },
    },
  ];

  return { name: "exec", version: "1.0.0", tools };
}

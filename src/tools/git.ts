/**
 * `git` tool pack — first-class version control: status, diff, log, add, commit,
 * show, branch. These could be bash one-liners, but as first-class tools they are
 * structured (parsed `--porcelain`), grant-scoped (`vcs.read` vs `vcs.write`), and
 * effect-classified (so a `git.commit` is checkpointed and never re-run on replay).
 *
 * Runs `git` via `execFile` (no shell — no injection) in the sandbox `root`. git
 * output carries hashes/timestamps (non-deterministic), which is fine: read/mutate
 * effects are recorded into the tape, so replay returns the recorded output.
 */

import { execFile } from "node:child_process";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

export interface GitOptions {
  root: string;
  timeoutMs?: number;
  maxOutput?: number;
}

function git(args: string[], cwd: string, timeoutMs: number, maxOutput: number): Promise<{ stdout: string; stderr: string; code: number }> {
  return new Promise((resolvePromise) => {
    execFile("git", args, { cwd, timeout: timeoutMs, maxBuffer: maxOutput * 4 }, (err, stdout, stderr) => {
      const code = err && typeof (err as { code?: unknown }).code === "number" ? (err as { code: number }).code : err ? 1 : 0;
      resolvePromise({ stdout: String(stdout).slice(0, maxOutput), stderr: String(stderr).slice(0, maxOutput), code });
    });
  });
}

export function gitPack(opts: GitOptions): ToolPack {
  const { root } = opts;
  const timeoutMs = opts.timeoutMs ?? 30_000;
  const maxOutput = opts.maxOutput ?? 100_000;
  const run = (args: string[]) => git(args, root, timeoutMs, maxOutput);

  const tools: ToolSpec[] = [
    {
      name: "git.status",
      version: 1,
      description: "Porcelain working-tree status: staged/unstaged/untracked files, structured.",
      effect: "reading",
      grants: ["vcs.read"],
      input: { type: "object", properties: {} },
      output: { type: "object", properties: { branch: { type: "string" }, files: { type: "array" }, clean: { type: "boolean" } } },
      handler: async () => {
        const r = await run(["status", "--porcelain=v1", "--branch"]);
        if (r.code !== 0 && r.stderr) throw new RotorError("E_TOOL", `git.status: ${r.stderr.trim()}`);
        const lines = r.stdout.split("\n").filter(Boolean);
        let branch = "";
        const files: Array<{ status: string; path: string }> = [];
        for (const ln of lines) {
          if (ln.startsWith("##")) branch = ln.slice(3).split("...")[0].trim();
          else files.push({ status: ln.slice(0, 2).trim(), path: ln.slice(3) });
        }
        return { branch, files, clean: files.length === 0 };
      },
    },
    {
      name: "git.diff",
      version: 1,
      description: "Unified diff of the working tree (optionally for a path, or --staged).",
      effect: "reading",
      grants: ["vcs.read"],
      input: { type: "object", properties: { path: { type: "string" }, staged: { type: "boolean" } } },
      output: { type: "object", properties: { diff: { type: "string" } } },
      handler: async (args: Row) => {
        const a = ["diff"];
        if (args.staged) a.push("--staged");
        if (args.path) a.push("--", String(args.path));
        const r = await run(a);
        return { diff: r.stdout };
      },
    },
    {
      name: "git.log",
      version: 1,
      description: "Recent commits, one line each (default 20).",
      effect: "reading",
      grants: ["vcs.read"],
      input: { type: "object", properties: { limit: { type: "number" }, path: { type: "string" } } },
      output: { type: "object", properties: { commits: { type: "array" } } },
      handler: async (args: Row) => {
        const n = Math.max(1, Math.min(Number(args.limit ?? 20), 200));
        const a = ["log", `-n${n}`, "--pretty=%h\t%an\t%s"];
        if (args.path) a.push("--", String(args.path));
        const r = await run(a);
        const commits = r.stdout
          .split("\n")
          .filter(Boolean)
          .map((ln) => {
            const [hash, author, ...rest] = ln.split("\t");
            return { hash, author, subject: rest.join("\t") };
          });
        return { commits };
      },
    },
    {
      name: "git.show",
      version: 1,
      description: "Show a commit/object (default HEAD).",
      effect: "reading",
      grants: ["vcs.read"],
      input: { type: "object", properties: { ref: { type: "string" } } },
      output: { type: "object", properties: { content: { type: "string" } } },
      handler: async (args: Row) => {
        const r = await run(["show", String(args.ref ?? "HEAD")]);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.show: ${r.stderr.trim() || "failed"}`);
        return { content: r.stdout };
      },
    },
    {
      name: "git.add",
      version: 1,
      description: "Stage a path (or '.' for all).",
      effect: "mutating",
      grants: ["vcs.write"],
      input: { type: "object", properties: { path: { type: "string" } }, required: ["path"] },
      output: { type: "object", properties: { staged: { type: "string" } } },
      handler: async (args: Row) => {
        const path = String(args.path ?? "");
        if (!path) throw new RotorError("E_MISSING_INPUT", "git.add requires a `path`");
        const r = await run(["add", "--", path]);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.add: ${r.stderr.trim()}`);
        return { staged: path };
      },
    },
    {
      name: "git.commit",
      version: 1,
      description: "Commit the staged changes with a message.",
      effect: "mutating",
      grants: ["vcs.write"],
      input: { type: "object", properties: { message: { type: "string" } }, required: ["message"] },
      output: { type: "object", properties: { committed: { type: "boolean" }, output: { type: "string" } } },
      handler: async (args: Row) => {
        const msg = String(args.message ?? "").trim();
        if (!msg) throw new RotorError("E_MISSING_INPUT", "git.commit requires a `message`");
        const r = await run(["commit", "-m", msg]);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.commit: ${(r.stderr || r.stdout).trim()}`);
        return { committed: true, output: r.stdout.trim() };
      },
    },
    {
      name: "git.branch",
      version: 1,
      description: "The current branch name.",
      effect: "reading",
      grants: ["vcs.read"],
      input: { type: "object", properties: {} },
      output: { type: "object", properties: { branch: { type: "string" } } },
      handler: async () => {
        const r = await run(["rev-parse", "--abbrev-ref", "HEAD"]);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.branch: ${r.stderr.trim() || "not a git repo"}`);
        return { branch: r.stdout.trim() };
      },
    },
  ];

  return { name: "git", version: "1.0.0", tools };
}

/**
 * `gitx` tool pack — the extended version-control workbench on top of the core
 * `git` pack: branch switching, restore/stash (the undo surface), rm/mv, tags,
 * remotes, blame, and the network trio clone/pull/push. Same discipline as git.ts:
 * every command runs via `execFile("git", [...])` in the sandbox `root` — argument
 * ARRAYS only, never a shell string, so no injection surface exists.
 *
 * Two extra guards this pack adds on top of the exemplar:
 *  - user-supplied refs/names/urls are refused if they start with `-` (an argument
 *    that would otherwise be parsed as a git option) — `E_POLICY_DENIED`;
 *  - every path argument resolves under `root` and escapes refuse (`E_POLICY_DENIED`),
 *    including the target dir of `git.clone`.
 *
 * Effects are classified honestly: local, reversible operations are `mutating`
 * (replay returns the recorded output without re-running); `pull`/`push` touch a
 * remote and are `external`. Output is bounded like git.ts; `blame` and `tag list`
 * cap their arrays and report `truncated`.
 */

import { execFile } from "node:child_process";
import { resolve, sep, relative } from "node:path";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

export interface GitxOptions {
  /** The repository / workspace sandbox. Commands run here; paths must stay under it. */
  root: string;
  /** Per-command wall-clock cap (ms). Default 30_000 (clone defaults to 120_000). */
  timeoutMs?: number;
  /** Max captured output bytes (bounds tokens). Default 100_000. */
  maxOutput?: number;
}

const MAX_BLAME_LINES = 500;
const MAX_TAGS = 200;

function git(args: string[], cwd: string, timeoutMs: number, maxOutput: number): Promise<{ stdout: string; stderr: string; code: number }> {
  return new Promise((resolvePromise) => {
    execFile("git", args, { cwd, timeout: timeoutMs, maxBuffer: maxOutput * 4 }, (err, stdout, stderr) => {
      const code = err && typeof (err as { code?: unknown }).code === "number" ? (err as { code: number }).code : err ? 1 : 0;
      resolvePromise({ stdout: String(stdout).slice(0, maxOutput), stderr: String(stderr).slice(0, maxOutput), code });
    });
  });
}

function resolveInRoot(root: string, p: unknown, dflt = "."): string {
  const raw = p == null || p === "" ? dflt : p;
  if (typeof raw !== "string") throw new RotorError("E_MISSING_INPUT", "gitx tool requires a string `path`");
  const base = resolve(root);
  const target = resolve(base, raw);
  const rel = relative(base, target);
  if (rel === ".." || rel.startsWith(".." + sep)) {
    throw new RotorError("E_POLICY_DENIED", `path escapes the workspace: ${raw}`, { context: { path: raw } });
  }
  return target;
}

/** A user-supplied ref/name/url that will sit in argv: non-empty, never option-shaped. */
function safeArg(x: unknown, tool: string, field: string): string {
  const s = typeof x === "string" ? x.trim() : "";
  if (!s) throw new RotorError("E_MISSING_INPUT", `${tool} requires a \`${field}\``);
  if (s.startsWith("-")) {
    throw new RotorError("E_POLICY_DENIED", `${tool}: \`${field}\` may not start with '-' (would be parsed as a git option)`, { context: { [field]: s } });
  }
  return s;
}

/** Validate a paths array: each sandboxed under root, returned workspace-relative. */
function safePaths(root: string, x: unknown, tool: string): string[] {
  if (!Array.isArray(x) || x.length === 0) throw new RotorError("E_MISSING_INPUT", `${tool} requires a non-empty \`paths\` array`);
  const base = resolve(root);
  return x.map((p) => {
    if (typeof p !== "string" || !p) throw new RotorError("E_MISSING_INPUT", `${tool}: every path must be a non-empty string`);
    return relative(base, resolveInRoot(root, p)) || ".";
  });
}

export function gitxPack(opts: GitxOptions): ToolPack {
  const { root } = opts;
  const timeoutMs = opts.timeoutMs ?? 30_000;
  const cloneTimeoutMs = opts.timeoutMs ?? 120_000;
  const maxOutput = opts.maxOutput ?? 100_000;
  const run = (args: string[], t = timeoutMs) => git(args, root, t, maxOutput);

  const tools: ToolSpec[] = [
    {
      name: "git.checkout",
      version: 1,
      description: "Switch to a branch/ref (or create a new branch with create:true).",
      effect: "mutating",
      grants: ["vcs.write"],
      input: { type: "object", properties: { ref: { type: "string" }, create: { type: "boolean" } }, required: ["ref"] },
      output: { type: "object", properties: { ref: { type: "string" }, output: { type: "string" } } },
      handler: async (args: Row) => {
        const ref = safeArg(args.ref, "git.checkout", "ref");
        const a = args.create ? ["checkout", "-b", ref] : ["checkout", ref];
        const r = await run(a);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.checkout: ${(r.stderr || r.stdout).trim()}`, { context: { ref } });
        return { ref, output: (r.stderr || r.stdout).trim() }; // git talks on stderr here
      },
    },
    {
      name: "git.restore",
      version: 1,
      description: "Discard working-tree changes to paths (or unstage them with staged:true).",
      effect: "mutating",
      grants: ["vcs.write"],
      input: {
        type: "object",
        properties: { paths: { type: "array", items: { type: "string" } }, staged: { type: "boolean" } },
        required: ["paths"],
      },
      output: { type: "object", properties: { output: { type: "string" } } },
      handler: async (args: Row) => {
        const paths = safePaths(root, args.paths, "git.restore");
        const a = ["restore", ...(args.staged ? ["--staged"] : []), "--", ...paths];
        const r = await run(a);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.restore: ${(r.stderr || r.stdout).trim()}`, { context: { paths } });
        return { output: r.stdout.trim() };
      },
    },
    {
      name: "git.stash",
      version: 1,
      description: "Stash operations: push (save, optional message), pop (re-apply latest), list.",
      effect: "mutating",
      grants: ["vcs.write"],
      input: { type: "object", properties: { op: { type: "string", enum: ["push", "pop", "list"] }, message: { type: "string" } }, required: ["op"] },
      output: { type: "object", properties: { output: { type: "string" } } },
      handler: async (args: Row) => {
        const op = String(args.op ?? "");
        if (op !== "push" && op !== "pop" && op !== "list") {
          throw new RotorError("E_MISSING_INPUT", "git.stash: `op` must be one of push|pop|list");
        }
        const a = ["stash", op];
        if (op === "push" && args.message) a.push("-m", String(args.message));
        const r = await run(a);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.stash ${op}: ${(r.stderr || r.stdout).trim()}`, { context: { op } });
        return { output: r.stdout.trim() };
      },
    },
    {
      name: "git.rm",
      version: 1,
      description: "Remove paths from the index and working tree (cached:true keeps the file on disk).",
      effect: "mutating",
      grants: ["vcs.write"],
      input: {
        type: "object",
        properties: { paths: { type: "array", items: { type: "string" } }, cached: { type: "boolean" } },
        required: ["paths"],
      },
      output: { type: "object", properties: { output: { type: "string" } } },
      handler: async (args: Row) => {
        const paths = safePaths(root, args.paths, "git.rm");
        const a = ["rm", ...(args.cached ? ["--cached"] : []), "--", ...paths];
        const r = await run(a);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.rm: ${(r.stderr || r.stdout).trim()}`, { context: { paths } });
        return { output: r.stdout.trim() };
      },
    },
    {
      name: "git.mv",
      version: 1,
      description: "Move/rename a tracked file (stages the rename).",
      effect: "mutating",
      grants: ["vcs.write"],
      input: { type: "object", properties: { from: { type: "string" }, to: { type: "string" } }, required: ["from", "to"] },
      output: { type: "object", properties: { output: { type: "string" } } },
      handler: async (args: Row) => {
        const base = resolve(root);
        const from = relative(base, resolveInRoot(root, safeArg(args.from, "git.mv", "from"))) || ".";
        const to = relative(base, resolveInRoot(root, safeArg(args.to, "git.mv", "to"))) || ".";
        const r = await run(["mv", "--", from, to]);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.mv: ${(r.stderr || r.stdout).trim()}`, { context: { from, to } });
        return { output: r.stdout.trim() };
      },
    },
    {
      name: "git.tag",
      version: 1,
      description: "Tag operations — op:'list' reads tags (bounded); op:'create' writes a tag (annotated when `message` is given). Classified mutating because create writes.",
      effect: "mutating",
      grants: ["vcs.write"],
      input: {
        type: "object",
        properties: { op: { type: "string", enum: ["list", "create"] }, name: { type: "string" }, message: { type: "string" } },
        required: ["op"],
      },
      output: {
        type: "object",
        properties: { tags: { type: "array" }, truncated: { type: "boolean" }, output: { type: "string" } },
      },
      handler: async (args: Row) => {
        const op = String(args.op ?? "");
        if (op === "list") {
          const r = await run(["tag", "--list"]);
          if (r.code !== 0) throw new RotorError("E_TOOL", `git.tag list: ${(r.stderr || r.stdout).trim()}`);
          const all = r.stdout.split("\n").filter(Boolean);
          return { tags: all.slice(0, MAX_TAGS), truncated: all.length > MAX_TAGS };
        }
        if (op !== "create") throw new RotorError("E_MISSING_INPUT", "git.tag: `op` must be one of list|create");
        const name = safeArg(args.name, "git.tag", "name");
        const a = args.message ? ["tag", "-a", name, "-m", String(args.message)] : ["tag", name];
        const r = await run(a);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.tag create: ${(r.stderr || r.stdout).trim()}`, { context: { name } });
        return { output: r.stdout.trim() || `tagged ${name}` };
      },
    },
    {
      name: "git.remote",
      version: 1,
      description: "List configured remotes as structured {name, url, kind:fetch|push} entries.",
      effect: "reading",
      grants: ["vcs.read"],
      input: { type: "object", properties: {} },
      output: { type: "object", properties: { remotes: { type: "array" } } },
      handler: async () => {
        const r = await run(["remote", "-v"]);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.remote: ${(r.stderr || r.stdout).trim() || "not a git repo"}`);
        // lines: "<name>\t<url> (fetch|push)"
        const remotes = r.stdout
          .split("\n")
          .filter(Boolean)
          .map((ln) => {
            const [name, rest = ""] = ln.split("\t");
            const m = /^(.*) \((fetch|push)\)$/.exec(rest);
            return { name, url: m ? m[1] : rest, kind: m ? m[2] : "fetch" };
          });
        return { remotes: remotes.slice(0, MAX_TAGS) };
      },
    },
    {
      name: "git.blame",
      version: 1,
      description: "Line-by-line authorship for a file (optional from/to line range), structured, capped at 500 lines.",
      effect: "reading",
      grants: ["vcs.read"],
      input: {
        type: "object",
        properties: { path: { type: "string" }, from: { type: "number" }, to: { type: "number" } },
        required: ["path"],
      },
      output: {
        type: "object",
        properties: { lines: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } },
      },
      handler: async (args: Row) => {
        const base = resolve(root);
        const path = relative(base, resolveInRoot(root, safeArg(args.path, "git.blame", "path"))) || ".";
        const a = ["blame", "--porcelain"];
        if (args.from != null || args.to != null) {
          const from = Math.max(1, Math.floor(Number(args.from ?? 1)));
          const to = Math.max(from, Math.floor(Number(args.to ?? from)));
          a.push("-L", `${from},${to}`);
        }
        a.push("--", path);
        const r = await run(a);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.blame: ${(r.stderr || r.stdout).trim()}`, { context: { path } });

        // Porcelain: a 40-hex header per line, per-commit metadata on first sight,
        // then the content line prefixed with a TAB.
        const meta = new Map<string, { author: string; date: string }>();
        const lines: Array<{ line: number; commit: string; author: string; date: string; text: string }> = [];
        let truncated = false;
        let cur: { sha: string; line: number } | null = null;
        for (const raw of r.stdout.split("\n")) {
          const h = /^([0-9a-f]{40}) \d+ (\d+)/.exec(raw);
          if (h) {
            cur = { sha: h[1], line: Number(h[2]) };
            if (!meta.has(h[1])) meta.set(h[1], { author: "", date: "" });
            continue;
          }
          if (raw.startsWith("\t")) {
            if (!cur) continue;
            if (lines.length >= MAX_BLAME_LINES) {
              truncated = true;
              break;
            }
            const m = meta.get(cur.sha) ?? { author: "", date: "" };
            lines.push({ line: cur.line, commit: cur.sha.slice(0, 12), author: m.author, date: m.date, text: raw.slice(1).slice(0, 400) });
            cur = null;
            continue;
          }
          if (cur) {
            const m = meta.get(cur.sha)!;
            if (raw.startsWith("author ")) m.author = raw.slice(7);
            else if (raw.startsWith("author-time ")) m.date = new Date(Number(raw.slice(12)) * 1000).toISOString();
          }
        }
        return { lines, count: lines.length, truncated };
      },
    },
    {
      name: "git.clone",
      version: 1,
      description: "Clone a repository into a directory under the workspace (shallow by default, depth 1).",
      effect: "mutating",
      grants: ["vcs.write", "net.read"],
      input: {
        type: "object",
        properties: { url: { type: "string" }, dir: { type: "string" }, depth: { type: "number" } },
        required: ["url", "dir"],
      },
      output: { type: "object", properties: { dir: { type: "string" }, output: { type: "string" } } },
      handler: async (args: Row) => {
        const url = safeArg(args.url, "git.clone", "url");
        const target = resolveInRoot(root, safeArg(args.dir, "git.clone", "dir"));
        const depth = Math.max(1, Math.floor(Number(args.depth ?? 1)));
        const r = await run(["clone", "--depth", String(depth), "--", url, target], cloneTimeoutMs);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.clone: ${(r.stderr || r.stdout).trim()}`, { context: { url, dir: args.dir } });
        return { dir: relative(resolve(root), target) || ".", output: (r.stderr || r.stdout).trim() };
      },
    },
    {
      name: "git.pull",
      version: 1,
      description: "Fetch from the tracked remote and merge into the current branch (touches the network).",
      effect: "external",
      grants: ["vcs.write", "net.read"],
      input: { type: "object", properties: {} },
      output: { type: "object", properties: { output: { type: "string" } } },
      handler: async () => {
        const r = await run(["pull"]);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.pull: ${(r.stderr || r.stdout).trim()}`);
        return { output: (r.stdout + (r.stderr ? "\n" + r.stderr : "")).trim() };
      },
    },
    {
      name: "git.push",
      version: 1,
      description: "PUBLISHES local commits to a remote — an outward, effectively irreversible effect. Optional remote/ref; set_upstream:true adds -u.",
      effect: "external",
      grants: ["vcs.write", "net.write"],
      input: {
        type: "object",
        properties: { remote: { type: "string" }, ref: { type: "string" }, set_upstream: { type: "boolean" } },
      },
      output: { type: "object", properties: { output: { type: "string" } } },
      handler: async (args: Row) => {
        const a = ["push", ...(args.set_upstream ? ["-u"] : [])];
        if (args.remote != null) a.push(safeArg(args.remote, "git.push", "remote"));
        if (args.ref != null) a.push(safeArg(args.ref, "git.push", "ref"));
        const r = await run(a);
        if (r.code !== 0) throw new RotorError("E_TOOL", `git.push: ${(r.stderr || r.stdout).trim()}`);
        return { output: (r.stderr || r.stdout).trim() }; // git push reports on stderr
      },
    },
  ];

  return { name: "gitx", version: "1.0.0", tools };
}

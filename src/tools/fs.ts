/**
 * `fs` tool pack — the filesystem workbench a coding agent lives in: read, write,
 * edit, list, glob, grep. All paths resolve under a sandbox `root`; a path that
 * escapes it is refused (`E_POLICY_DENIED`) rather than touching the wider disk.
 *
 * `glob`/`grep` are deliberately BOUNDED (capped results, sorted) — a smart tool
 * that returns 20 ranked hits instead of 10,000 raw paths is where the token savings
 * live (docs/tools.md). POSIX-first; a Windows provider is a later drop-in behind the
 * same names.
 */

import { readFile, writeFile, readdir, mkdir, stat } from "node:fs/promises";
import { resolve, sep, dirname, relative, join } from "node:path";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

export interface FsOptions {
  /** The workspace sandbox. Every path resolves under here; escapes refuse. */
  root: string;
  /** Max files walked by glob/grep (bounds cost). Default 5000. */
  maxWalk?: number;
  /** Max hits glob/grep return (bounds tokens). Default 200. */
  maxHits?: number;
}

function resolveInRoot(root: string, p: unknown, dflt = "."): string {
  const raw = p == null || p === "" ? dflt : p;
  if (typeof raw !== "string") throw new RotorError("E_MISSING_INPUT", "file tool requires a string `path`");
  const base = resolve(root);
  const target = resolve(base, raw);
  const rel = relative(base, target);
  if (rel === ".." || rel.startsWith(".." + sep)) {
    throw new RotorError("E_POLICY_DENIED", `path escapes the workspace: ${raw}`, { context: { path: raw } });
  }
  return target;
}

const rel = (root: string, p: string): string => relative(resolve(root), p) || ".";

/** Walk files under `dir` (bounded), returning workspace-relative paths, sorted. */
async function walk(root: string, dir: string, max: number): Promise<string[]> {
  const out: string[] = [];
  const stack = [dir];
  while (stack.length && out.length < max) {
    const cur = stack.pop()!;
    let entries;
    try {
      entries = await readdir(cur, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const d of entries) {
      if (d.name === ".git" || d.name === "node_modules") continue; // never walk the obvious noise
      const full = join(cur, d.name);
      if (d.isDirectory()) stack.push(full);
      else if (d.isFile()) out.push(rel(root, full));
      if (out.length >= max) break;
    }
  }
  return out.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

/** Translate a glob (`**`, `*`, `?`) to an anchored RegExp over `/`-joined paths. */
function globToRe(glob: string): RegExp {
  let re = "";
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === "*") {
      if (glob[i + 1] === "*") {
        re += ".*";
        i++;
        if (glob[i + 1] === "/") i++;
      } else re += "[^/]*";
    } else if (c === "?") re += "[^/]";
    else if (".+^${}()|[]\\".includes(c)) re += "\\" + c;
    else re += c;
  }
  return new RegExp("^" + re + "$");
}

export function fsPack(opts: FsOptions): ToolPack {
  const { root } = opts;
  const maxWalk = opts.maxWalk ?? 5000;
  const maxHits = opts.maxHits ?? 200;

  const tools: ToolSpec[] = [
    {
      name: "file.read",
      version: 1,
      description: "Read a UTF-8 file under the workspace root. Returns its content.",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { path: { type: "string" } }, required: ["path"] },
      output: { type: "object", properties: { path: { type: "string" }, content: { type: "string" }, bytes: { type: "number" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        try {
          const content = await readFile(p, "utf8");
          return { path: rel(root, p), content, bytes: Buffer.byteLength(content) };
        } catch (e) {
          throw new RotorError("E_TOOL", `file.read failed: ${(e as Error).message}`, { context: { path: args.path } });
        }
      },
    },
    {
      name: "file.write",
      version: 1,
      description: "Write a UTF-8 file under the workspace root (creates parent dirs).",
      effect: "mutating",
      grants: ["fs.write"],
      input: { type: "object", properties: { path: { type: "string" }, content: { type: "string" } }, required: ["path", "content"] },
      output: { type: "object", properties: { path: { type: "string" }, bytes_written: { type: "number" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        const content = String(args.content ?? "");
        try {
          await mkdir(dirname(p), { recursive: true });
          await writeFile(p, content, "utf8");
          return { path: rel(root, p), bytes_written: Buffer.byteLength(content) };
        } catch (e) {
          throw new RotorError("E_TOOL", `file.write failed: ${(e as Error).message}`, { context: { path: args.path } });
        }
      },
    },
    {
      name: "file.edit",
      version: 1,
      description: "Replace an exact string in a file. `old` must occur exactly once unless `all` is true.",
      effect: "mutating",
      grants: ["fs.write"],
      input: {
        type: "object",
        properties: { path: { type: "string" }, old: { type: "string" }, new: { type: "string" }, all: { type: "boolean" } },
        required: ["path", "old", "new"],
      },
      output: { type: "object", properties: { path: { type: "string" }, replaced: { type: "number" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        const oldStr = String(args.old ?? "");
        const newStr = String(args.new ?? "");
        if (oldStr === "") throw new RotorError("E_MISSING_INPUT", "file.edit requires a non-empty `old`");
        let content: string;
        try {
          content = await readFile(p, "utf8");
        } catch (e) {
          throw new RotorError("E_TOOL", `file.edit read failed: ${(e as Error).message}`, { context: { path: args.path } });
        }
        const count = content.split(oldStr).length - 1;
        if (count === 0) throw new RotorError("E_TOOL", "file.edit: `old` string not found", { context: { path: args.path } });
        if (count > 1 && !args.all) {
          throw new RotorError("E_TOOL", `file.edit: \`old\` occurs ${count}× — pass all:true or make it unique`, { context: { path: args.path, count } });
        }
        const next = args.all ? content.split(oldStr).join(newStr) : content.replace(oldStr, newStr);
        await writeFile(p, next, "utf8");
        return { path: rel(root, p), replaced: args.all ? count : 1 };
      },
    },
    {
      name: "file.list",
      version: 1,
      description: "List entries of a directory under the workspace root (sorted).",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { path: { type: "string" } } },
      output: { type: "object", properties: { path: { type: "string" }, entries: { type: "array" }, count: { type: "number" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        try {
          const entries = await readdir(p, { withFileTypes: true });
          const items = await Promise.all(
            entries.map(async (d) => {
              const kind = d.isDirectory() ? "dir" : "file";
              const size = kind === "file" ? (await stat(join(p, d.name))).size : 0;
              return { name: d.name, kind, size };
            }),
          );
          items.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
          return { path: rel(root, p), entries: items, count: items.length };
        } catch (e) {
          throw new RotorError("E_TOOL", `file.list failed: ${(e as Error).message}`, { context: { path: args.path } });
        }
      },
    },
    {
      name: "fs.glob",
      version: 1,
      description: "Find files matching a glob (** * ?) under the workspace, sorted and capped.",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { pattern: { type: "string" }, path: { type: "string" } }, required: ["pattern"] },
      output: { type: "object", properties: { paths: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const dir = resolveInRoot(root, args.path);
        const re = globToRe(String(args.pattern ?? "*"));
        const files = await walk(root, dir, maxWalk);
        const hits = files.filter((f) => re.test(f));
        return { paths: hits.slice(0, maxHits), count: hits.length, truncated: hits.length > maxHits };
      },
    },
    {
      name: "fs.grep",
      version: 1,
      description: "Search file contents by regex under the workspace. Returns bounded, ranked {file,line,text} hits.",
      effect: "reading",
      grants: ["fs.read"],
      input: {
        type: "object",
        properties: { pattern: { type: "string" }, path: { type: "string" }, glob: { type: "string" }, ignore_case: { type: "boolean" } },
        required: ["pattern"],
      },
      output: { type: "object", properties: { matches: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const dir = resolveInRoot(root, args.path);
        let re: RegExp;
        try {
          re = new RegExp(String(args.pattern ?? ""), args.ignore_case ? "i" : "");
        } catch (e) {
          throw new RotorError("E_MISSING_INPUT", `fs.grep: bad regex: ${(e as Error).message}`);
        }
        const nameRe = args.glob ? globToRe(String(args.glob)) : undefined;
        const files = (await walk(root, dir, maxWalk)).filter((f) => !nameRe || nameRe.test(f));
        const matches: Array<{ file: string; line: number; text: string }> = [];
        for (const f of files) {
          if (matches.length >= maxHits) break;
          let content: string;
          try {
            content = await readFile(join(resolve(root), f), "utf8");
          } catch {
            continue; // skip binary/unreadable
          }
          const lines = content.split("\n");
          for (let i = 0; i < lines.length && matches.length < maxHits; i++) {
            if (re.test(lines[i])) matches.push({ file: f, line: i + 1, text: lines[i].slice(0, 400) });
          }
        }
        return { matches, count: matches.length, truncated: matches.length >= maxHits };
      },
    },
  ];

  return { name: "fs", version: "1.0.0", tools };
}

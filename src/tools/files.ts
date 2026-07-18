/**
 * `files` tool pack — the power extension of `fs`: append/move/copy/delete,
 * stat/touch/mkdir, windowed reads (head/tail/lines), checksums, an ascii tree,
 * disk-usage ranking, binary-safe base64 I/O, and tar archives. Every path
 * resolves under the sandbox `root`; a path that escapes it is refused
 * (`E_POLICY_DENIED`) rather than touching the wider disk — including tar
 * members, which are listed and vetted BEFORE extraction.
 *
 * Reads are BOUNDED: head/tail/lines/read_b64 never load more than `maxBytes`
 * (default 1 MB), tree/du cap their walks, and every capped result says so with
 * `truncated: true`. Archive tools shell out via `execFile` argument arrays —
 * never string interpolation — with a timeout and bounded captured output.
 */

import { appendFile, copyFile, lstat, mkdir, open, readdir, rename, rm, rmdir, stat, unlink, utimes, writeFile } from "node:fs/promises";
import { createReadStream } from "node:fs";
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

const execFileP = promisify(execFile);

export interface FilesOptions {
  /** The workspace sandbox. Every path resolves under here; escapes refuse. */
  root: string;
  /** Max bytes any single read-style tool loads (bounds tokens + memory). Default 1_000_000. */
  maxBytes?: number;
}

const TREE_CAP = 500; // max entries fs.tree renders
const DU_TOP = 50; // fs.du returns the N largest files
const DU_WALK = 5000; // max files fs.du walks
const MEMBER_CAP = 200; // max archive member names returned
const EXEC_TIMEOUT_MS = 60_000;
const EXEC_MAX_BUFFER = 8 * 1024 * 1024;
const CHECKSUM_ALGOS = new Set(["sha256", "sha1", "sha512", "md5"]);

function resolveInRoot(root: string, p: unknown, dflt = "."): string {
  const raw = p == null || p === "" ? dflt : p;
  if (typeof raw !== "string") throw new RotorError("E_MISSING_INPUT", "files tool requires a string `path`");
  const base = resolve(root);
  const target = resolve(base, raw);
  const relPath = relative(base, target);
  if (relPath === ".." || relPath.startsWith(".." + sep)) {
    throw new RotorError("E_POLICY_DENIED", `path escapes the workspace: ${raw}`, { context: { path: raw } });
  }
  return target;
}

const rel = (root: string, p: string): string => relative(resolve(root), p) || ".";

function toolFail(op: string, e: unknown, context: Record<string, unknown>): RotorError {
  if (e instanceof RotorError) return e;
  return new RotorError("E_TOOL", `${op} failed: ${(e as Error).message}`, { context, cause: e });
}

function requireString(args: Row, key: string, tool: string): string {
  const v = args[key];
  if (typeof v !== "string") throw new RotorError("E_MISSING_INPUT", `${tool} requires a string \`${key}\``);
  return v;
}

function optionalCount(v: unknown, dflt: number): number {
  if (v == null) return dflt;
  const n = Number(v);
  if (!Number.isInteger(n) || n < 1) throw new RotorError("E_MISSING_INPUT", `expected a positive integer, got ${String(v)}`);
  return n;
}

/** Read at most `cap` bytes from the start of a file (never loads huge files whole). */
async function readCapped(p: string, cap: number, position = 0): Promise<{ buf: Buffer; size: number }> {
  const fh = await open(p, "r");
  try {
    const st = await fh.stat();
    const n = Math.max(0, Math.min(st.size - position, cap));
    const buf = Buffer.alloc(n);
    let off = 0;
    while (off < n) {
      const { bytesRead } = await fh.read(buf, off, n - off, position + off);
      if (bytesRead === 0) break;
      off += bytesRead;
    }
    return { buf: buf.subarray(0, off), size: st.size };
  } finally {
    await fh.close();
  }
}

interface TreeState {
  lines: string[];
  dirs: number;
  files: number;
  entries: number;
  truncated: boolean;
}

async function buildTree(dir: string, depth: number, prefix: string, s: TreeState): Promise<void> {
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return; // unreadable subdir — skip, don't fail the whole tree
  }
  const kept = entries.filter((d) => d.name !== ".git" && d.name !== "node_modules").sort((a, b) => (a.name < b.name ? -1 : 1));
  for (let i = 0; i < kept.length; i++) {
    if (s.entries >= TREE_CAP) {
      s.truncated = true;
      return;
    }
    const d = kept[i];
    const last = i === kept.length - 1;
    s.entries++;
    if (d.isDirectory()) {
      s.dirs++;
      s.lines.push(prefix + (last ? "└── " : "├── ") + d.name + "/");
      if (depth > 1) await buildTree(join(dir, d.name), depth - 1, prefix + (last ? "    " : "│   "), s);
    } else {
      s.files++;
      s.lines.push(prefix + (last ? "└── " : "├── ") + d.name);
    }
  }
}

async function walkSizes(root: string, dir: string): Promise<{ items: Array<{ path: string; bytes: number }>; hitCap: boolean }> {
  const items: Array<{ path: string; bytes: number }> = [];
  const stack = [dir];
  let hitCap = false;
  while (stack.length) {
    if (items.length >= DU_WALK) {
      hitCap = true;
      break;
    }
    const cur = stack.pop()!;
    let entries;
    try {
      entries = await readdir(cur, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const d of entries) {
      if (d.name === ".git" || d.name === "node_modules") continue;
      const full = join(cur, d.name);
      if (d.isDirectory()) stack.push(full);
      else if (d.isFile()) {
        try {
          items.push({ path: rel(root, full), bytes: (await stat(full)).size });
        } catch {
          /* raced away — skip */
        }
        if (items.length >= DU_WALK) {
          hitCap = true;
          break;
        }
      }
    }
  }
  return { items, hitCap };
}

async function runTar(args: string[], cwd: string): Promise<{ stdout: string; stderr: string }> {
  try {
    return await execFileP("tar", args, { cwd, timeout: EXEC_TIMEOUT_MS, maxBuffer: EXEC_MAX_BUFFER });
  } catch (e) {
    const err = e as Error & { stderr?: string };
    throw new RotorError("E_TOOL", `tar failed: ${err.message}`, {
      context: { args, stderr: String(err.stderr ?? "").slice(0, 2000) },
      cause: e,
    });
  }
}

export function filesPack(opts: FilesOptions): ToolPack {
  const { root } = opts;
  const maxBytes = opts.maxBytes ?? 1_000_000;

  const tools: ToolSpec[] = [
    {
      name: "file.append",
      version: 1,
      description: "Append UTF-8 content to a file under the workspace root (creates the file + parent dirs).",
      effect: "mutating",
      grants: ["fs.write"],
      input: { type: "object", properties: { path: { type: "string" }, content: { type: "string" } }, required: ["path", "content"] },
      output: { type: "object", properties: { path: { type: "string" }, bytes_appended: { type: "number" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        const content = requireString(args, "content", "file.append");
        try {
          await mkdir(dirname(p), { recursive: true });
          await appendFile(p, content, "utf8");
          return { path: rel(root, p), bytes_appended: Buffer.byteLength(content) };
        } catch (e) {
          throw toolFail("file.append", e, { path: args.path });
        }
      },
    },
    {
      name: "file.delete",
      version: 1,
      description: "Delete a file or empty directory under the workspace root; pass recursive:true to remove a directory tree.",
      effect: "mutating",
      grants: ["fs.write"],
      input: { type: "object", properties: { path: { type: "string" }, recursive: { type: "boolean" } }, required: ["path"] },
      output: { type: "object", properties: { path: { type: "string" }, deleted: { type: "boolean" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        try {
          const st = await lstat(p);
          if (st.isDirectory()) {
            if (args.recursive === true) await rm(p, { recursive: true, force: true });
            else await rmdir(p); // refuses non-empty — the safe default
          } else await unlink(p);
          return { path: rel(root, p), deleted: true };
        } catch (e) {
          throw toolFail("file.delete", e, { path: args.path });
        }
      },
    },
    {
      name: "file.move",
      version: 1,
      description: "Move/rename a file or directory within the workspace root (creates destination parent dirs).",
      effect: "mutating",
      grants: ["fs.write"],
      input: { type: "object", properties: { from: { type: "string" }, to: { type: "string" } }, required: ["from", "to"] },
      output: { type: "object", properties: { from: { type: "string" }, to: { type: "string" } } },
      handler: async (args: Row) => {
        const src = resolveInRoot(root, requireString(args, "from", "file.move"));
        const dst = resolveInRoot(root, requireString(args, "to", "file.move"));
        try {
          await mkdir(dirname(dst), { recursive: true });
          await rename(src, dst);
          return { from: rel(root, src), to: rel(root, dst) };
        } catch (e) {
          throw toolFail("file.move", e, { from: args.from, to: args.to });
        }
      },
    },
    {
      name: "file.copy",
      version: 1,
      description: "Copy a file within the workspace root (creates destination parent dirs). Returns bytes copied.",
      effect: "mutating",
      grants: ["fs.write"],
      input: { type: "object", properties: { from: { type: "string" }, to: { type: "string" } }, required: ["from", "to"] },
      output: { type: "object", properties: { from: { type: "string" }, to: { type: "string" }, bytes: { type: "number" } } },
      handler: async (args: Row) => {
        const src = resolveInRoot(root, requireString(args, "from", "file.copy"));
        const dst = resolveInRoot(root, requireString(args, "to", "file.copy"));
        try {
          await mkdir(dirname(dst), { recursive: true });
          await copyFile(src, dst);
          return { from: rel(root, src), to: rel(root, dst), bytes: (await stat(dst)).size };
        } catch (e) {
          throw toolFail("file.copy", e, { from: args.from, to: args.to });
        }
      },
    },
    {
      name: "file.mkdir",
      version: 1,
      description: "Create a directory (recursively) under the workspace root. `created` is false if it already existed.",
      effect: "mutating",
      grants: ["fs.write"],
      input: { type: "object", properties: { path: { type: "string" } }, required: ["path"] },
      output: { type: "object", properties: { path: { type: "string" }, created: { type: "boolean" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        try {
          const existed = await lstat(p).then(
            (st) => st.isDirectory(),
            () => false,
          );
          await mkdir(p, { recursive: true });
          return { path: rel(root, p), created: !existed };
        } catch (e) {
          throw toolFail("file.mkdir", e, { path: args.path });
        }
      },
    },
    {
      name: "file.stat",
      version: 1,
      description: "Stat a path under the workspace root: kind (file/dir/symlink), size, mtime, mode.",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { path: { type: "string" } }, required: ["path"] },
      output: {
        type: "object",
        properties: { path: { type: "string" }, kind: { type: "string" }, size: { type: "number" }, mtime_iso: { type: "string" }, mode: { type: "string" } },
      },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        try {
          const st = await lstat(p);
          const kind = st.isDirectory() ? "dir" : st.isSymbolicLink() ? "symlink" : st.isFile() ? "file" : "other";
          return { path: rel(root, p), kind, size: st.size, mtime_iso: st.mtime.toISOString(), mode: (st.mode & 0o7777).toString(8) };
        } catch (e) {
          throw toolFail("file.stat", e, { path: args.path });
        }
      },
    },
    {
      name: "file.touch",
      version: 1,
      description: "Create an empty file if missing, else update its mtime (creates parent dirs).",
      effect: "mutating",
      grants: ["fs.write"],
      input: { type: "object", properties: { path: { type: "string" } }, required: ["path"] },
      output: { type: "object", properties: { path: { type: "string" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        try {
          await mkdir(dirname(p), { recursive: true });
          const fh = await open(p, "a"); // create-if-missing without clobbering
          await fh.close();
          const now = new Date();
          await utimes(p, now, now);
          return { path: rel(root, p) };
        } catch (e) {
          throw toolFail("file.touch", e, { path: args.path });
        }
      },
    },
    {
      name: "file.head",
      version: 1,
      description: "Return the first N lines (default 10) of a file. Streams — never reads more than the byte cap.",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { path: { type: "string" }, lines: { type: "number" } }, required: ["path"] },
      output: { type: "object", properties: { text: { type: "string" }, total_lines_read: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        const n = optionalCount(args.lines, 10);
        try {
          const { buf, size } = await readCapped(p, maxBytes);
          const all = buf.toString("utf8").split("\n");
          const head = all.slice(0, n);
          return { text: head.join("\n"), total_lines_read: head.length, truncated: buf.length < size && all.length <= n };
        } catch (e) {
          throw toolFail("file.head", e, { path: args.path });
        }
      },
    },
    {
      name: "file.tail",
      version: 1,
      description: "Return the last N lines (default 10) of a file, read efficiently from the tail bytes only.",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { path: { type: "string" }, lines: { type: "number" } }, required: ["path"] },
      output: { type: "object", properties: { text: { type: "string" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        const n = optionalCount(args.lines, 10);
        try {
          const st = await stat(p);
          const from = Math.max(0, st.size - maxBytes);
          const { buf } = await readCapped(p, maxBytes, from);
          let text = buf.toString("utf8");
          if (text.endsWith("\n")) text = text.slice(0, -1); // a trailing newline is not an extra line
          let lines = text.split("\n");
          if (from > 0 && lines.length > 0) lines = lines.slice(1); // first line may be a partial — drop it
          return { text: lines.slice(-n).join("\n") };
        } catch (e) {
          throw toolFail("file.tail", e, { path: args.path });
        }
      },
    },
    {
      name: "file.lines",
      version: 1,
      description: "Return an inclusive 1-indexed line range [from, to] of a file (bounded by the byte cap).",
      effect: "reading",
      grants: ["fs.read"],
      input: {
        type: "object",
        properties: { path: { type: "string" }, from: { type: "number" }, to: { type: "number" } },
        required: ["path", "from", "to"],
      },
      output: { type: "object", properties: { text: { type: "string" }, from: { type: "number" }, to: { type: "number" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        if (args.from == null || args.to == null) throw new RotorError("E_MISSING_INPUT", "file.lines requires `from` and `to`");
        const from = optionalCount(args.from, 1);
        const to = optionalCount(args.to, 1);
        if (from > to) throw new RotorError("E_MISSING_INPUT", `file.lines: from (${from}) must be <= to (${to})`);
        try {
          const { buf } = await readCapped(p, maxBytes);
          const all = buf.toString("utf8").split("\n");
          const slice = all.slice(from - 1, to);
          return { text: slice.join("\n"), from, to: from + slice.length - 1 };
        } catch (e) {
          throw toolFail("file.lines", e, { path: args.path });
        }
      },
    },
    {
      name: "file.checksum",
      version: 1,
      description: "Streamed checksum of a file (sha256 default; sha1/sha512/md5 allowed). Returns hex digest + size.",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { path: { type: "string" }, algo: { type: "string" } }, required: ["path"] },
      output: { type: "object", properties: { hex: { type: "string" }, algo: { type: "string" }, bytes: { type: "number" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        const algo = args.algo == null ? "sha256" : String(args.algo);
        if (!CHECKSUM_ALGOS.has(algo)) {
          throw new RotorError("E_MISSING_INPUT", `file.checksum: unsupported algo \`${algo}\` (use sha256|sha1|sha512|md5)`);
        }
        try {
          const hash = createHash(algo);
          let bytes = 0;
          for await (const chunk of createReadStream(p)) {
            hash.update(chunk as Buffer);
            bytes += (chunk as Buffer).length;
          }
          return { hex: hash.digest("hex"), algo, bytes };
        } catch (e) {
          throw toolFail("file.checksum", e, { path: args.path });
        }
      },
    },
    {
      name: "fs.tree",
      version: 1,
      description: "Render an ascii tree of a directory (depth default 3, skips .git/node_modules, capped at 500 entries).",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { path: { type: "string" }, depth: { type: "number" } } },
      output: {
        type: "object",
        properties: { tree: { type: "string" }, dirs: { type: "number" }, files: { type: "number" }, truncated: { type: "boolean" } },
      },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        const depth = optionalCount(args.depth, 3);
        try {
          const st = await stat(p);
          if (!st.isDirectory()) throw new RotorError("E_TOOL", "fs.tree: path is not a directory", { context: { path: args.path } });
          const s: TreeState = { lines: [rel(root, p)], dirs: 0, files: 0, entries: 0, truncated: false };
          await buildTree(p, depth, "", s);
          return { tree: s.lines.join("\n"), dirs: s.dirs, files: s.files, truncated: s.truncated };
        } catch (e) {
          throw toolFail("fs.tree", e, { path: args.path });
        }
      },
    },
    {
      name: "fs.du",
      version: 1,
      description: "Disk usage under a directory: the 50 largest files plus the walked total (bounded walk).",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { path: { type: "string" } } },
      output: { type: "object", properties: { entries: { type: "array" }, total_bytes: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        try {
          const { items, hitCap } = await walkSizes(root, p);
          items.sort((a, b) => b.bytes - a.bytes || (a.path < b.path ? -1 : 1));
          const total = items.reduce((acc, i) => acc + i.bytes, 0);
          return { entries: items.slice(0, DU_TOP), total_bytes: total, truncated: hitCap || items.length > DU_TOP };
        } catch (e) {
          throw toolFail("fs.du", e, { path: args.path });
        }
      },
    },
    {
      name: "file.read_b64",
      version: 1,
      description: "Read a file as base64 (binary-safe), capped at max_bytes (default 1 MB). Sets truncated if capped.",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { path: { type: "string" }, max_bytes: { type: "number" } }, required: ["path"] },
      output: { type: "object", properties: { b64: { type: "string" }, bytes: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        const cap = Math.min(optionalCount(args.max_bytes, maxBytes), maxBytes);
        try {
          const { buf, size } = await readCapped(p, cap);
          return { b64: buf.toString("base64"), bytes: buf.length, truncated: buf.length < size };
        } catch (e) {
          throw toolFail("file.read_b64", e, { path: args.path });
        }
      },
    },
    {
      name: "file.write_b64",
      version: 1,
      description: "Write base64-decoded bytes to a file under the workspace root (binary-safe, creates parent dirs).",
      effect: "mutating",
      grants: ["fs.write"],
      input: { type: "object", properties: { path: { type: "string" }, b64: { type: "string" } }, required: ["path", "b64"] },
      output: { type: "object", properties: { path: { type: "string" }, bytes_written: { type: "number" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.path);
        const b64 = requireString(args, "b64", "file.write_b64");
        if (!/^[A-Za-z0-9+/=\r\n]*$/.test(b64)) throw new RotorError("E_MISSING_INPUT", "file.write_b64: `b64` is not valid base64");
        try {
          const buf = Buffer.from(b64, "base64");
          await mkdir(dirname(p), { recursive: true });
          await writeFile(p, buf);
          return { path: rel(root, p), bytes_written: buf.length };
        } catch (e) {
          throw toolFail("file.write_b64", e, { path: args.path });
        }
      },
    },
    {
      name: "archive.tar",
      version: 1,
      description: "Create a gzipped tar (.tgz) from workspace paths. All inputs and the output stay under the root.",
      effect: "mutating",
      grants: ["fs.write"],
      input: {
        type: "object",
        properties: { paths: { type: "array", items: { type: "string" } }, out: { type: "string" } },
        required: ["paths", "out"],
      },
      output: { type: "object", properties: { path: { type: "string" }, bytes: { type: "number" }, files: { type: "number" } } },
      handler: async (args: Row) => {
        const paths = args.paths;
        if (!Array.isArray(paths) || paths.length === 0 || !paths.every((x) => typeof x === "string")) {
          throw new RotorError("E_MISSING_INPUT", "archive.tar requires a non-empty string[] `paths`");
        }
        const out = resolveInRoot(root, requireString(args, "out", "archive.tar"));
        // Every input must resolve inside the sandbox BEFORE anything is spawned.
        const relPaths = (paths as string[]).map((x) => rel(root, resolveInRoot(root, x)));
        await mkdir(dirname(out), { recursive: true });
        await runTar(["-czf", out, "--", ...relPaths], resolve(root));
        try {
          return { path: rel(root, out), bytes: (await stat(out)).size, files: relPaths.length };
        } catch (e) {
          throw toolFail("archive.tar", e, { out: args.out });
        }
      },
    },
    {
      name: "archive.untar",
      version: 1,
      description: "Extract a gzipped tar under the workspace root. Members are listed first; absolute or `..` paths refuse.",
      effect: "mutating",
      grants: ["fs.write"],
      input: { type: "object", properties: { path: { type: "string" }, into: { type: "string" } }, required: ["path"] },
      output: { type: "object", properties: { files: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const tarPath = resolveInRoot(root, args.path);
        const into = resolveInRoot(root, args.into);
        // SECURITY: vet the member list before a single byte is extracted — a
        // member like `../evil` or `/etc/x` would land outside the sandbox.
        const { stdout } = await runTar(["-tzf", tarPath], resolve(root));
        const members = stdout.split("\n").map((s) => s.trim()).filter(Boolean);
        for (const m of members) {
          if (isAbsolute(m) || m.split("/").includes("..")) {
            throw new RotorError("E_POLICY_DENIED", `archive.untar: member escapes the workspace: ${m}`, { context: { member: m } });
          }
        }
        try {
          await mkdir(into, { recursive: true });
        } catch (e) {
          throw toolFail("archive.untar", e, { into: args.into });
        }
        await runTar(["-xzf", tarPath], into);
        return { files: members.slice(0, MEMBER_CAP), count: members.length, truncated: members.length > MEMBER_CAP };
      },
    },
  ];

  return { name: "files", version: "1.0.0", tools };
}

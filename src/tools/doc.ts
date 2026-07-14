/**
 * `doc` tool pack — token-smart document tools for the co-work / writing surface.
 * `doc.outline` returns just a file's heading structure (a few tokens) instead of
 * the whole document — the "smart tool pre-digests" principle (docs/tools.md).
 * `doc.section` returns one section by heading. `doc.write` authors a document.
 */

import { readFile, writeFile, mkdir } from "node:fs/promises";
import { resolve, sep, relative, dirname } from "node:path";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

export interface DocOptions {
  root: string;
}

function inRoot(root: string, p: unknown): string {
  if (typeof p !== "string" || !p) throw new RotorError("E_MISSING_INPUT", "doc tool requires a `path`");
  const base = resolve(root);
  const target = resolve(base, p);
  const rel = relative(base, target);
  if (rel === ".." || rel.startsWith(".." + sep)) throw new RotorError("E_POLICY_DENIED", `path escapes the workspace: ${p}`);
  return target;
}

/** Parse ATX markdown headings into a structured outline. */
function outline(md: string): Array<{ level: number; title: string; line: number }> {
  const out: Array<{ level: number; title: string; line: number }> = [];
  const lines = md.split("\n");
  let inFence = false;
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    if (/^\s*```/.test(ln)) inFence = !inFence;
    if (inFence) continue;
    const m = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(ln);
    if (m) out.push({ level: m[1].length, title: m[2].trim(), line: i + 1 });
  }
  return out;
}

export function docPack(opts: DocOptions): ToolPack {
  const { root } = opts;

  const tools: ToolSpec[] = [
    {
      name: "doc.outline",
      version: 1,
      description: "Return a markdown document's heading outline (level, title, line) — not its full text.",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { path: { type: "string" } }, required: ["path"] },
      output: { type: "object", properties: { outline: { type: "array" }, count: { type: "number" } } },
      handler: async (args: Row) => {
        const p = inRoot(root, args.path);
        let md: string;
        try {
          md = await readFile(p, "utf8");
        } catch (e) {
          throw new RotorError("E_TOOL", `doc.outline: ${(e as Error).message}`);
        }
        const o = outline(md);
        return { outline: o, count: o.length };
      },
    },
    {
      name: "doc.section",
      version: 1,
      description: "Return the text of one section of a markdown doc, selected by its heading title.",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { path: { type: "string" }, heading: { type: "string" } }, required: ["path", "heading"] },
      output: { type: "object", properties: { heading: { type: "string" }, text: { type: "string" }, found: { type: "boolean" } } },
      handler: async (args: Row) => {
        const p = inRoot(root, args.path);
        const want = String(args.heading ?? "").trim().toLowerCase();
        const md = await readFile(p, "utf8").catch((e) => {
          throw new RotorError("E_TOOL", `doc.section: ${(e as Error).message}`);
        });
        const o = outline(md);
        const lines = md.split("\n");
        const idx = o.findIndex((h) => h.title.toLowerCase() === want);
        if (idx === -1) return { heading: String(args.heading), text: "", found: false };
        // Body = lines after this heading, up to the next heading at the same-or-
        // shallower level. `line` is 1-based; the heading sits at index line-1, so the
        // body starts at index `line`.
        const start = o[idx].line;
        let end = lines.length;
        for (let j = idx + 1; j < o.length; j++) {
          if (o[j].level <= o[idx].level) {
            end = o[j].line - 1;
            break;
          }
        }
        const text = lines.slice(start, end).join("\n").trim();
        return { heading: o[idx].title, text, found: true };
      },
    },
    {
      name: "doc.write",
      version: 1,
      description: "Write a document (markdown/text) under the workspace root.",
      effect: "mutating",
      grants: ["doc.write"],
      input: { type: "object", properties: { path: { type: "string" }, content: { type: "string" } }, required: ["path", "content"] },
      output: { type: "object", properties: { path: { type: "string" }, bytes_written: { type: "number" } } },
      handler: async (args: Row) => {
        const p = inRoot(root, args.path);
        const content = String(args.content ?? "");
        await mkdir(dirname(p), { recursive: true });
        await writeFile(p, content, "utf8");
        return { path: relative(resolve(root), p), bytes_written: Buffer.byteLength(content) };
      },
    },
  ];

  return { name: "doc", version: "1.0.0", tools };
}

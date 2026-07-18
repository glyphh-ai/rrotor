/**
 * `data` tool pack — the pure data workbench: parse, shape, query, diff, patch and
 * transcode the formats an agent meets every turn (JSON, YAML, CSV, XML, JSON
 * Schema, base64/hex/url). Every tool is a deterministic function of its inputs —
 * `effect: "pure"`, `grants: []` — so the whole pack is available in EVERY
 * permission mode, replays for free, and can never touch the disk, the network,
 * or the environment.
 *
 * Outputs are BOUNDED (row/change/error caps with a `truncated` flag) so a
 * pathological payload cannot flood the tape. The XML parser is hand-rolled and
 * refuses DOCTYPE outright (`E_POLICY_DENIED`) — no entity expansion, no XXE
 * surface, by construction.
 */

import { Ajv2020, type ErrorObject, type ValidateFunction } from "ajv/dist/2020.js";
import addFormatsModule, { type FormatsPlugin } from "ajv-formats";
import { parse as parseYamlText, stringify as stringifyYamlValue } from "yaml";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

// ajv-formats ships CommonJS; under NodeNext the interop default can land on the
// module namespace, so unwrap a `.default` if present to recover the callable.
const addFormats: FormatsPlugin =
  (addFormatsModule as unknown as { default?: FormatsPlugin }).default ??
  (addFormatsModule as unknown as FormatsPlugin);

const MAX_TEXT = 1_000_000; // cap on any produced text payload
const MAX_ROWS = 10_000; // csv.parse row cap
const MAX_ITEMS = 1_000; // query results / diff changes cap
const MAX_ERRORS = 20; // jsonschema.validate error cap
const MAX_XML_NODES = 10_000;
const MAX_XML_DEPTH = 256;

// ───────────────────────────────────────────────────────────────────────────
// Shared helpers.
// ───────────────────────────────────────────────────────────────────────────

function requireString(args: Row, key: string, tool: string): string {
  const v = args[key];
  if (typeof v !== "string") throw new RotorError("E_MISSING_INPUT", `${tool} requires a string \`${key}\``);
  return v;
}

function requireValue(args: Row, key: string, tool: string): unknown {
  if (!(key in args) || args[key] === undefined) {
    throw new RotorError("E_MISSING_INPUT", `${tool} requires \`${key}\``);
  }
  return args[key];
}

function capText(text: string): { text: string; truncated: boolean } {
  return text.length > MAX_TEXT ? { text: text.slice(0, MAX_TEXT), truncated: true } : { text, truncated: false };
}

/** Strict JSON.parse, tolerating ONLY a markdown ```json fence around the payload. */
function parseJsonText(text: string, tool: string): unknown {
  let src = text.trim();
  const fence = /^```[a-zA-Z]*\s*\n([\s\S]*?)\n?```$/.exec(src);
  if (fence) src = fence[1];
  try {
    return JSON.parse(src);
  } catch (e) {
    throw new RotorError("E_TOOL", `${tool}: invalid JSON: ${(e as Error).message}`, { cause: e });
  }
}

/** JSON-style deep equality (used by json.diff and the RFC-6902 `test` op). */
function deepEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;
  const aArr = Array.isArray(a);
  if (aArr !== Array.isArray(b)) return false;
  if (aArr) {
    const x = a as unknown[];
    const y = b as unknown[];
    return x.length === y.length && x.every((v, i) => deepEqual(v, y[i]));
  }
  const ka = Object.keys(a as object);
  const kb = Object.keys(b as object);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => k in (b as object) && deepEqual((a as Row)[k], (b as Row)[k]));
}

const isPlainObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

// ── dot/bracket path (`users[*].name`) — json.query ────────────────────────

type PathSeg = { kind: "key"; key: string } | { kind: "index"; i: number } | { kind: "wild" };

function parsePath(path: string, tool: string): PathSeg[] {
  const src = path.replace(/^\$\.?/, ""); // tolerate a `$.` prefix
  const bad = (why: string): never => {
    throw new RotorError("E_MISSING_INPUT", `${tool}: ${why} in path \`${path}\``);
  };
  const segs: PathSeg[] = [];
  if (src === "") return segs;
  if (src.endsWith(".")) bad("trailing dot");
  let i = 0;
  while (i < src.length) {
    if (src[i] === "[") {
      const end = src.indexOf("]", i);
      const inner = end < 0 ? "" : src.slice(i + 1, end);
      if (end < 0 || !(inner === "*" || /^\d+$/.test(inner))) bad(`bad bracket segment at offset ${i}`);
      segs.push(inner === "*" ? { kind: "wild" } : { kind: "index", i: Number(inner) });
      i = end + 1;
    } else {
      let j = i;
      while (j < src.length && src[j] !== "." && src[j] !== "[") j++;
      const key = src.slice(i, j);
      if (key === "") bad(`empty segment at offset ${i}`);
      if (key.includes("]")) bad(`stray ']' at offset ${i}`);
      segs.push({ kind: "key", key });
      i = j;
    }
    if (src[i] === ".") i++; // `a[0].b` and `a.b` both step over one dot
  }
  return segs;
}

// ── json.diff ──────────────────────────────────────────────────────────────

interface Change {
  path: string;
  op: "add" | "remove" | "replace";
  from?: unknown;
  to?: unknown;
}

function diffInto(a: unknown, b: unknown, path: string, out: Change[]): void {
  if (out.length > MAX_ITEMS) return; // bounded — the flag is set by the caller
  if (Object.is(a, b)) return;
  if (isPlainObject(a) && isPlainObject(b)) {
    for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) {
      const p = path === "" ? k : `${path}.${k}`;
      if (!(k in b)) out.push({ path: p, op: "remove", from: a[k] });
      else if (!(k in a)) out.push({ path: p, op: "add", to: b[k] });
      else diffInto(a[k], b[k], p, out);
      if (out.length > MAX_ITEMS) return;
    }
    return;
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    const shared = Math.min(a.length, b.length);
    for (let i = 0; i < shared && out.length <= MAX_ITEMS; i++) diffInto(a[i], b[i], `${path}[${i}]`, out);
    for (let i = shared; i < a.length && out.length <= MAX_ITEMS; i++) out.push({ path: `${path}[${i}]`, op: "remove", from: a[i] });
    for (let i = shared; i < b.length && out.length <= MAX_ITEMS; i++) out.push({ path: `${path}[${i}]`, op: "add", to: b[i] });
    return;
  }
  if (!deepEqual(a, b)) out.push({ path, op: "replace", from: a, to: b });
}

// ── RFC-6902 subset — json.patch ───────────────────────────────────────────

function pointerTokens(pointer: string, tool: string): string[] {
  if (pointer === "") return [];
  if (!pointer.startsWith("/")) throw new RotorError("E_MISSING_INPUT", `${tool}: JSON Pointer must start with '/': \`${pointer}\``);
  return pointer
    .slice(1)
    .split("/")
    .map((t) => t.replace(/~1/g, "/").replace(/~0/g, "~"));
}

/** Navigate to the parent of the pointer target. Throws E_TOOL on a missing path. */
function pointerParent(root: { doc: unknown }, tokens: string[], pointer: string): { parent: unknown; key: string } {
  let cur: unknown = root.doc;
  for (let i = 0; i < tokens.length - 1; i++) {
    const t = tokens[i];
    if (Array.isArray(cur)) cur = cur[Number(t)];
    else if (isPlainObject(cur)) cur = cur[t];
    else cur = undefined;
    if (cur === undefined) throw new RotorError("E_TOOL", `json.patch: path does not exist: \`${pointer}\``, { context: { pointer } });
  }
  return { parent: cur, key: tokens[tokens.length - 1] };
}

function pointerGet(root: { doc: unknown }, pointer: string): unknown {
  const tokens = pointerTokens(pointer, "json.patch");
  if (tokens.length === 0) return root.doc;
  const { parent, key } = pointerParent(root, tokens, pointer);
  const v = Array.isArray(parent) ? parent[Number(key)] : isPlainObject(parent) ? parent[key] : undefined;
  if (v === undefined) throw new RotorError("E_TOOL", `json.patch: path does not exist: \`${pointer}\``, { context: { pointer } });
  return v;
}

function pointerAdd(root: { doc: unknown }, pointer: string, value: unknown): void {
  const tokens = pointerTokens(pointer, "json.patch");
  if (tokens.length === 0) {
    root.doc = value;
    return;
  }
  const { parent, key } = pointerParent(root, tokens, pointer);
  if (Array.isArray(parent)) {
    const i = key === "-" ? parent.length : Number(key);
    if (!Number.isInteger(i) || i < 0 || i > parent.length) {
      throw new RotorError("E_TOOL", `json.patch: bad array index \`${key}\` at \`${pointer}\``, { context: { pointer } });
    }
    parent.splice(i, 0, value);
  } else if (isPlainObject(parent)) parent[key] = value;
  else throw new RotorError("E_TOOL", `json.patch: cannot add into a non-container at \`${pointer}\``, { context: { pointer } });
}

function pointerRemove(root: { doc: unknown }, pointer: string): unknown {
  const tokens = pointerTokens(pointer, "json.patch");
  if (tokens.length === 0) throw new RotorError("E_TOOL", "json.patch: cannot remove the document root");
  const { parent, key } = pointerParent(root, tokens, pointer);
  if (Array.isArray(parent)) {
    const i = Number(key);
    if (!Number.isInteger(i) || i < 0 || i >= parent.length) {
      throw new RotorError("E_TOOL", `json.patch: path does not exist: \`${pointer}\``, { context: { pointer } });
    }
    return parent.splice(i, 1)[0];
  }
  if (isPlainObject(parent) && key in parent) {
    const v = parent[key];
    delete parent[key];
    return v;
  }
  throw new RotorError("E_TOOL", `json.patch: path does not exist: \`${pointer}\``, { context: { pointer } });
}

// ── RFC-4180 CSV (hand-rolled: quotes, escaped quotes, embedded newlines) ──

function parseCsvText(text: string, delim: string): { rows: string[][]; truncated: boolean } {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  let fieldQuoted = false;
  let truncated = false;
  const pushField = (): void => {
    row.push(field);
    field = "";
    fieldQuoted = false;
  };
  const pushRow = (): boolean => {
    pushField();
    rows.push(row);
    row = [];
    return rows.length >= MAX_ROWS;
  };
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else inQuotes = false;
      } else field += c;
    } else if (c === '"' && field === "" && !fieldQuoted) {
      inQuotes = true;
      fieldQuoted = true;
    } else if (c === delim) pushField();
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      if (pushRow()) {
        truncated = i + 1 < text.length;
        return { rows, truncated };
      }
    } else field += c;
  }
  if (inQuotes) throw new RotorError("E_TOOL", "csv.parse: unterminated quoted field");
  if (field !== "" || fieldQuoted || row.length > 0) pushRow(); // no trailing newline
  return { rows, truncated };
}

function csvField(v: unknown, delim: string): string {
  const s = v == null ? "" : typeof v === "string" ? v : JSON.stringify(v);
  return s.includes(delim) || s.includes('"') || s.includes("\n") || s.includes("\r") ? `"${s.replace(/"/g, '""')}"` : s;
}

// ── minimal XML (no DOCTYPE, no external entities — XXE-proof by refusal) ──

export interface XmlNode {
  tag: string;
  attrs: Record<string, string>;
  children: XmlNode[];
  text: string;
}

function decodeEntities(s: string): string {
  return s.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (whole, ent: string) => {
    if (ent[0] === "#") {
      const code = ent[1] === "x" || ent[1] === "X" ? parseInt(ent.slice(2), 16) : parseInt(ent.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : whole;
    }
    return ({ lt: "<", gt: ">", amp: "&", quot: '"', apos: "'" } as Record<string, string>)[ent] ?? whole;
  });
}

function parseXmlText(src: string): XmlNode {
  if (/<!DOCTYPE/i.test(src)) {
    throw new RotorError("E_POLICY_DENIED", "xml.parse refuses DOCTYPE declarations (XXE guard) — strip the prolog and retry");
  }
  let i = 0;
  let nodes = 0;
  const fail = (msg: string): never => {
    throw new RotorError("E_TOOL", `xml.parse: ${msg} (offset ${i})`);
  };
  const skipMisc = (): void => {
    for (;;) {
      while (i < src.length && /\s/.test(src[i])) i++;
      if (src.startsWith("<!--", i)) {
        const end = src.indexOf("-->", i + 4);
        if (end < 0) fail("unterminated comment");
        i = end + 3;
      } else if (src.startsWith("<?", i)) {
        const end = src.indexOf("?>", i + 2);
        if (end < 0) fail("unterminated processing instruction");
        i = end + 2;
      } else return;
    }
  };
  const readName = (): string => {
    const m = /^[A-Za-z_][\w.:-]*/.exec(src.slice(i));
    if (!m) fail("expected a name");
    i += m![0].length;
    return m![0];
  };
  const parseElement = (depth: number): XmlNode => {
    if (depth > MAX_XML_DEPTH) fail("document nests too deep");
    if (++nodes > MAX_XML_NODES) fail(`document exceeds ${MAX_XML_NODES} nodes`);
    if (src[i] !== "<") fail("expected '<'");
    i++;
    const tag = readName();
    const node: XmlNode = { tag, attrs: {}, children: [], text: "" };
    for (;;) {
      while (i < src.length && /\s/.test(src[i])) i++;
      if (src.startsWith("/>", i)) {
        i += 2;
        return node;
      }
      if (src[i] === ">") {
        i++;
        break;
      }
      const name = readName();
      while (i < src.length && /\s/.test(src[i])) i++;
      if (src[i] !== "=") fail(`attribute \`${name}\` missing '='`);
      i++;
      while (i < src.length && /\s/.test(src[i])) i++;
      const q = src[i];
      if (q !== '"' && q !== "'") fail(`attribute \`${name}\` value must be quoted`);
      const end = src.indexOf(q, i + 1);
      if (end < 0) fail(`unterminated attribute \`${name}\``);
      node.attrs[name] = decodeEntities(src.slice(i + 1, end));
      i = end + 1;
    }
    // Content until the matching close tag.
    for (;;) {
      if (i >= src.length) fail(`unclosed element <${tag}>`);
      if (src.startsWith("<!--", i)) {
        const end = src.indexOf("-->", i + 4);
        if (end < 0) fail("unterminated comment");
        i = end + 3;
      } else if (src.startsWith("<![CDATA[", i)) {
        const end = src.indexOf("]]>", i + 9);
        if (end < 0) fail("unterminated CDATA");
        node.text += src.slice(i + 9, end);
        i = end + 3;
      } else if (src.startsWith("</", i)) {
        i += 2;
        const close = readName();
        if (close !== tag) fail(`mismatched close tag </${close}> for <${tag}>`);
        while (i < src.length && /\s/.test(src[i])) i++;
        if (src[i] !== ">") fail("malformed close tag");
        i++;
        node.text = node.text.trim();
        return node;
      } else if (src[i] === "<") {
        node.children.push(parseElement(depth + 1));
      } else {
        const next = src.indexOf("<", i);
        const end = next < 0 ? src.length : next;
        node.text += decodeEntities(src.slice(i, end));
        i = end;
      }
    }
  };
  skipMisc();
  const rootNode = parseElement(0);
  skipMisc();
  if (i < src.length) fail("trailing content after the root element");
  return rootNode;
}

// ───────────────────────────────────────────────────────────────────────────
// The pack.
// ───────────────────────────────────────────────────────────────────────────

export function dataPack(): ToolPack {
  const tools: ToolSpec[] = [
    {
      name: "json.parse",
      version: 1,
      description: "Parse strict JSON text to a value (a surrounding markdown ```json fence is stripped first).",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
      output: { type: "object", properties: { value: {} } },
      handler: async (args: Row) => ({ value: parseJsonText(requireString(args, "text", "json.parse"), "json.parse") }),
    },
    {
      name: "json.stringify",
      version: 1,
      description: "Serialize a value to JSON text; `pretty: true` indents with 2 spaces. Output capped.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { value: {}, pretty: { type: "boolean" } }, required: ["value"] },
      output: { type: "object", properties: { text: { type: "string" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const value = requireValue(args, "value", "json.stringify");
        try {
          return capText(JSON.stringify(value, null, args.pretty ? 2 : 0) ?? "null");
        } catch (e) {
          throw new RotorError("E_TOOL", `json.stringify failed: ${(e as Error).message}`, { cause: e });
        }
      },
    },
    {
      name: "json.query",
      version: 1,
      description: "Extract from a JSON value (or text) by dot/bracket path — `users[*].name` ([*] wildcard, [n] index). Returns {result, found}.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { value: {}, text: { type: "string" }, path: { type: "string" } },
        required: ["path"],
      },
      output: { type: "object", properties: { result: {}, found: { type: "boolean" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const doc = "value" in args && args.value !== undefined ? args.value : typeof args.text === "string" ? parseJsonText(args.text, "json.query") : undefined;
        if (doc === undefined && !("value" in args)) throw new RotorError("E_MISSING_INPUT", "json.query requires `value` or `text`");
        const segs = parsePath(requireString(args, "path", "json.query"), "json.query");
        let nodes: unknown[] = [doc];
        let fanned = false;
        for (const s of segs) {
          const next: unknown[] = [];
          for (const n of nodes) {
            if (s.kind === "wild") {
              if (Array.isArray(n)) next.push(...n);
              else if (isPlainObject(n)) next.push(...Object.values(n));
            } else if (s.kind === "index") {
              if (Array.isArray(n) && s.i < n.length) next.push(n[s.i]);
            } else if (isPlainObject(n) && s.key in n) next.push(n[s.key]);
          }
          if (s.kind === "wild") fanned = true;
          nodes = next;
          if (nodes.length === 0) break;
        }
        if (fanned) {
          return { result: nodes.slice(0, MAX_ITEMS), found: nodes.length > 0, truncated: nodes.length > MAX_ITEMS };
        }
        return { result: nodes.length ? nodes[0] : null, found: nodes.length > 0, truncated: false };
      },
    },
    {
      name: "json.diff",
      version: 1,
      description: "Deep-compare two JSON values. Returns {changes: [{path, op, from, to}], count} — capped.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { a: {}, b: {} }, required: ["a", "b"] },
      output: { type: "object", properties: { changes: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const a = requireValue(args, "a", "json.diff");
        const b = requireValue(args, "b", "json.diff");
        const changes: Change[] = [];
        diffInto(a, b, "", changes);
        const truncated = changes.length > MAX_ITEMS;
        const out = truncated ? changes.slice(0, MAX_ITEMS) : changes;
        return { changes: out, count: out.length, truncated };
      },
    },
    {
      name: "json.patch",
      version: 1,
      description: "Apply an RFC-6902 patch (add/remove/replace/copy/move/test, JSON Pointer paths) to a value. Returns the patched value.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { value: {}, ops: { type: "array", items: { type: "object" } } },
        required: ["value", "ops"],
      },
      output: { type: "object", properties: { value: {}, applied: { type: "number" } } },
      handler: async (args: Row) => {
        const value = requireValue(args, "value", "json.patch");
        if (!Array.isArray(args.ops)) throw new RotorError("E_MISSING_INPUT", "json.patch requires an `ops` array");
        const root = { doc: structuredClone(value) };
        for (const [idx, raw] of args.ops.entries()) {
          const op = raw as { op?: string; path?: string; from?: string; value?: unknown };
          if (typeof op?.op !== "string" || typeof op?.path !== "string") {
            throw new RotorError("E_MISSING_INPUT", `json.patch: op[${idx}] needs string \`op\` and \`path\``);
          }
          switch (op.op) {
            case "add":
              pointerAdd(root, op.path, structuredClone(op.value));
              break;
            case "remove":
              pointerRemove(root, op.path);
              break;
            case "replace":
              pointerGet(root, op.path); // must exist (RFC 6902 §4.3)
              if (op.path === "") root.doc = structuredClone(op.value);
              else {
                pointerRemove(root, op.path);
                pointerAdd(root, op.path, structuredClone(op.value));
              }
              break;
            case "move": {
              if (typeof op.from !== "string") throw new RotorError("E_MISSING_INPUT", `json.patch: op[${idx}] move needs \`from\``);
              const moved = pointerRemove(root, op.from);
              pointerAdd(root, op.path, moved);
              break;
            }
            case "copy": {
              if (typeof op.from !== "string") throw new RotorError("E_MISSING_INPUT", `json.patch: op[${idx}] copy needs \`from\``);
              pointerAdd(root, op.path, structuredClone(pointerGet(root, op.from)));
              break;
            }
            case "test":
              if (!deepEqual(pointerGet(root, op.path), op.value)) {
                throw new RotorError("E_TOOL", `json.patch: test failed at \`${op.path}\``, { context: { index: idx, path: op.path } });
              }
              break;
            default:
              throw new RotorError("E_MISSING_INPUT", `json.patch: unknown op \`${op.op}\` at op[${idx}]`);
          }
        }
        return { value: root.doc, applied: args.ops.length };
      },
    },
    {
      name: "yaml.parse",
      version: 1,
      description: "Parse YAML text to a value.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
      output: { type: "object", properties: { value: {} } },
      handler: async (args: Row) => {
        const text = requireString(args, "text", "yaml.parse");
        try {
          return { value: parseYamlText(text) ?? null };
        } catch (e) {
          throw new RotorError("E_TOOL", `yaml.parse: invalid YAML: ${(e as Error).message}`, { cause: e });
        }
      },
    },
    {
      name: "yaml.stringify",
      version: 1,
      description: "Serialize a value to YAML text. Output capped.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { value: {} }, required: ["value"] },
      output: { type: "object", properties: { text: { type: "string" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const value = requireValue(args, "value", "yaml.stringify");
        try {
          return capText(stringifyYamlValue(value));
        } catch (e) {
          throw new RotorError("E_TOOL", `yaml.stringify failed: ${(e as Error).message}`, { cause: e });
        }
      },
    },
    {
      name: "csv.parse",
      version: 1,
      description: "Parse RFC-4180 CSV (quotes, embedded newlines). `headers: true` returns objects keyed by the first row. Capped at 10000 rows.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: { text: { type: "string" }, delimiter: { type: "string", maxLength: 1 }, headers: { type: "boolean" } },
        required: ["text"],
      },
      output: {
        type: "object",
        properties: { rows: { type: "array" }, columns: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } },
      },
      handler: async (args: Row) => {
        const text = requireString(args, "text", "csv.parse");
        const delim = args.delimiter == null ? "," : String(args.delimiter);
        if (delim.length !== 1 || delim === '"' || delim === "\n") {
          throw new RotorError("E_MISSING_INPUT", "csv.parse `delimiter` must be a single character (not a quote or newline)");
        }
        const { rows: raw, truncated } = parseCsvText(text, delim);
        if (args.headers) {
          const columns = raw[0] ?? [];
          const rows = raw.slice(1).map((r) => Object.fromEntries(columns.map((c, ci) => [c, r[ci] ?? ""])));
          return { rows, columns, count: rows.length, truncated };
        }
        return { rows: raw, columns: [], count: raw.length, truncated };
      },
    },
    {
      name: "csv.stringify",
      version: 1,
      description: "Serialize rows (arrays or objects) to RFC-4180 CSV with proper quoting; objects emit a header row.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: {
          rows: { type: "array" },
          columns: { type: "array", items: { type: "string" } },
          delimiter: { type: "string", maxLength: 1 },
        },
        required: ["rows"],
      },
      output: { type: "object", properties: { text: { type: "string" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        if (!Array.isArray(args.rows)) throw new RotorError("E_MISSING_INPUT", "csv.stringify requires a `rows` array");
        const delim = args.delimiter == null ? "," : String(args.delimiter);
        if (delim.length !== 1) throw new RotorError("E_MISSING_INPUT", "csv.stringify `delimiter` must be a single character");
        const rows = args.rows as unknown[];
        const objects = rows.length > 0 && isPlainObject(rows[0]);
        const columns = Array.isArray(args.columns) && args.columns.length > 0 ? args.columns.map(String) : objects ? Object.keys(rows[0] as object) : [];
        const lines: string[] = [];
        if (columns.length > 0) lines.push(columns.map((c) => csvField(c, delim)).join(delim));
        for (const r of rows) {
          if (Array.isArray(r)) lines.push(r.map((v) => csvField(v, delim)).join(delim));
          else if (isPlainObject(r)) {
            const cols = columns.length > 0 ? columns : Object.keys(r);
            lines.push(cols.map((c) => csvField(r[c], delim)).join(delim));
          } else lines.push(csvField(r, delim));
        }
        return capText(lines.join("\n") + (lines.length ? "\n" : ""));
      },
    },
    {
      name: "xml.parse",
      version: 1,
      description: "Parse well-formed XML to nested {tag, attrs, children, text}. DOCTYPE is refused (XXE guard); no external entities.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
      output: { type: "object", properties: { value: { type: "object" } } },
      handler: async (args: Row) => ({ value: parseXmlText(requireString(args, "text", "xml.parse")) }),
    },
    {
      name: "jsonschema.validate",
      version: 1,
      description: "Validate a value against a JSON Schema (2020-12 dialect, formats supported). Returns {valid, errors} — max 20 errors.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { value: {}, schema: { type: "object" } }, required: ["value", "schema"] },
      output: {
        type: "object",
        properties: { valid: { type: "boolean" }, errors: { type: "array" }, error_count: { type: "number" }, truncated: { type: "boolean" } },
      },
      handler: async (args: Row) => {
        const value = requireValue(args, "value", "jsonschema.validate");
        if (!isPlainObject(args.schema) && typeof args.schema !== "boolean") {
          throw new RotorError("E_MISSING_INPUT", "jsonschema.validate requires an object `schema`");
        }
        // strict:false so common draft-07 style schemas validate without churn.
        const ajv = new Ajv2020({ allErrors: true, strict: false, validateFormats: true });
        addFormats(ajv);
        let validate: ValidateFunction;
        try {
          validate = ajv.compile(args.schema as object);
        } catch (e) {
          throw new RotorError("E_MISSING_INPUT", `jsonschema.validate: schema does not compile: ${(e as Error).message}`, { cause: e });
        }
        const valid = validate(value) as boolean;
        const all = (validate.errors ?? []) as ErrorObject[];
        const errors = all.slice(0, MAX_ERRORS).map((e) => ({ path: e.instancePath || "/", keyword: e.keyword, message: e.message ?? "" }));
        return { valid, errors, error_count: all.length, truncated: all.length > MAX_ERRORS };
      },
    },
    {
      name: "base64.encode",
      version: 1,
      description: "Base64-encode UTF-8 `text` (or validate + normalize an already-encoded `bytes_b64`). Returns {b64}.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { text: { type: "string" }, bytes_b64: { type: "string" } } },
      output: { type: "object", properties: { b64: { type: "string" }, bytes: { type: "number" } } },
      handler: async (args: Row) => {
        if (typeof args.bytes_b64 === "string") {
          const buf = decodeB64(args.bytes_b64, "base64.encode");
          return { b64: buf.toString("base64"), bytes: buf.length };
        }
        const text = requireString(args, "text", "base64.encode");
        const buf = Buffer.from(text, "utf8");
        return { b64: buf.toString("base64"), bytes: buf.length };
      },
    },
    {
      name: "base64.decode",
      version: 1,
      description: "Decode base64 to UTF-8 text. Refuses invalid base64. Returns {text, bytes}.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { b64: { type: "string" } }, required: ["b64"] },
      output: { type: "object", properties: { text: { type: "string" }, bytes: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const buf = decodeB64(requireString(args, "b64", "base64.decode"), "base64.decode");
        const { text, truncated } = capText(buf.toString("utf8"));
        return { text, bytes: buf.length, truncated };
      },
    },
    {
      name: "hex.encode",
      version: 1,
      description: "Hex-encode UTF-8 text. Returns {hex}.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
      output: { type: "object", properties: { hex: { type: "string" }, bytes: { type: "number" } } },
      handler: async (args: Row) => {
        const buf = Buffer.from(requireString(args, "text", "hex.encode"), "utf8");
        return { hex: buf.toString("hex"), bytes: buf.length };
      },
    },
    {
      name: "hex.decode",
      version: 1,
      description: "Decode a hex string to UTF-8 text. Refuses non-hex or odd-length input. Returns {text}.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { hex: { type: "string" } }, required: ["hex"] },
      output: { type: "object", properties: { text: { type: "string" }, bytes: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const hex = requireString(args, "hex", "hex.decode").trim();
        if (!/^[0-9a-fA-F]*$/.test(hex) || hex.length % 2 !== 0) {
          throw new RotorError("E_TOOL", "hex.decode: input is not even-length hex");
        }
        const buf = Buffer.from(hex, "hex");
        const { text, truncated } = capText(buf.toString("utf8"));
        return { text, bytes: buf.length, truncated };
      },
    },
    {
      name: "url.encode",
      version: 1,
      description: "Percent-encode text: `component: true` uses encodeURIComponent (a single param value); default encodeURI (a whole URL).",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { text: { type: "string" }, component: { type: "boolean" } }, required: ["text"] },
      output: { type: "object", properties: { text: { type: "string" } } },
      handler: async (args: Row) => {
        const text = requireString(args, "text", "url.encode");
        try {
          return { text: args.component ? encodeURIComponent(text) : encodeURI(text) };
        } catch (e) {
          // lone surrogates throw URIError
          throw new RotorError("E_TOOL", `url.encode failed: ${(e as Error).message}`, { cause: e });
        }
      },
    },
    {
      name: "url.decode",
      version: 1,
      description: "Decode percent-encoded text (decodeURIComponent). Refuses malformed escapes. Returns {text}.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
      output: { type: "object", properties: { text: { type: "string" } } },
      handler: async (args: Row) => {
        const text = requireString(args, "text", "url.decode");
        try {
          return { text: decodeURIComponent(text) };
        } catch (e) {
          throw new RotorError("E_TOOL", `url.decode: malformed percent-encoding: ${(e as Error).message}`, { cause: e });
        }
      },
    },
  ];

  return { name: "data", version: "1.0.0", tools };
}

function decodeB64(input: string, tool: string): Buffer {
  const s = input.replace(/\s+/g, "");
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(s) || s.length % 4 !== 0) {
    throw new RotorError("E_TOOL", `${tool}: invalid base64 input`);
  }
  return Buffer.from(s, "base64");
}

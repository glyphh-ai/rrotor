/**
 * `db` tool pack — structured SQLite over workspace-sandboxed database files:
 * query, exec, tables, schema. A rotor that can *ask a database* instead of
 * grepping CSVs is the difference between token-burning text munging and one
 * precise, bounded answer.
 *
 * Design rules mirror `fs`: every db path resolves under the sandbox `root`
 * (escapes refuse with `E_POLICY_DENIED`), results are BOUNDED (row cap +
 * per-cell string cap, `truncated: true` when clipped), and reads are honest —
 * `sqlite.query` refuses any statement SQLite itself reports as non-readonly,
 * so a sneaky `WITH … DELETE` or a value-setting `PRAGMA` cannot ride in on the
 * `reading` effect class. Connections are opened per call and closed in
 * `finally`; no handle outlives a step.
 */

import Database from "better-sqlite3";
import { resolve, sep, relative, dirname } from "node:path";
import { mkdirSync } from "node:fs";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

export interface DbOptions {
  /** The workspace sandbox. Every db path resolves under here; escapes refuse. */
  root: string;
  /** Max rows any query/listing returns (bounds tokens). Default 500. */
  maxRows?: number;
}

/** Per-cell string cap — a single 10MB TEXT column must not flood the tape. */
const MAX_CELL = 10_000;

function resolveInRoot(root: string, p: unknown): string {
  if (typeof p !== "string" || p === "") throw new RotorError("E_MISSING_INPUT", "sqlite tool requires a string `db` path");
  const base = resolve(root);
  const target = resolve(base, p);
  const rel = relative(base, target);
  if (rel === ".." || rel.startsWith(".." + sep)) {
    throw new RotorError("E_POLICY_DENIED", `db path escapes the workspace: ${p}`, { context: { db: p } });
  }
  return target;
}

/** Normalize `params` to something better-sqlite3 can bind (array | object | none). */
function bindParams(params: unknown): unknown[] | Record<string, unknown> | undefined {
  if (params == null) return undefined;
  if (Array.isArray(params)) return params;
  if (typeof params === "object") return params as Record<string, unknown>;
  throw new RotorError("E_MISSING_INPUT", "`params` must be a positional array or a named object");
}

/** Make a raw SQLite cell tape-safe: bound strings, stringify BLOBs, number BigInts. */
function cell(v: unknown): unknown {
  if (typeof v === "bigint") return Number.isSafeInteger(Number(v)) ? Number(v) : v.toString();
  if (Buffer.isBuffer(v)) return `<blob ${v.length} bytes>`;
  if (typeof v === "string" && v.length > MAX_CELL) return v.slice(0, MAX_CELL);
  return v;
}

function boundRow(r: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(r)) out[k] = cell(v);
  return out;
}

/** Quote an identifier for interpolation into count/pragma statements. */
const quoteIdent = (name: string): string => `"${name.replace(/"/g, '""')}"`;

/** The leading SQL keyword, skipping whitespace and comments. */
function firstKeyword(sql: string): string {
  const stripped = sql.replace(/^(\s|--[^\n]*\n?|\/\*[\s\S]*?\*\/)+/, "");
  return (stripped.match(/^[a-zA-Z]+/) ?? [""])[0].toLowerCase();
}

/** ATTACH/DETACH name a database FILE that SQLite opens directly — a path the
 *  runtime never sandboxes, escaping the workspace root. Refuse them across
 *  every statement of a (possibly multi-statement) script. */
function assertNoAttach(sql: string, db: unknown): void {
  for (const stmt of sql.split(";")) {
    const kw = firstKeyword(stmt);
    if (kw === "attach" || kw === "detach") {
      throw new RotorError("E_POLICY_DENIED", "sqlite refuses ATTACH/DETACH — database paths must stay within the workspace sandbox", { context: { db } });
    }
  }
}

interface ColumnInfo {
  name: string;
  type: string;
  pk: boolean;
  notnull: boolean;
}

function tableColumns(db: InstanceType<typeof Database>, table: string): ColumnInfo[] {
  const rows = db.prepare(`PRAGMA table_info(${quoteIdent(table)})`).all() as Array<{ name: string; type: string; pk: number; notnull: number }>;
  return rows.map((c) => ({ name: c.name, type: c.type, pk: c.pk > 0, notnull: c.notnull !== 0 }));
}

function userTables(db: InstanceType<typeof Database>): Array<{ name: string; sql: string }> {
  return db
    .prepare("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    .all() as Array<{ name: string; sql: string }>;
}

export function dbPack(opts: { root: string; maxRows?: number }): ToolPack {
  const { root } = opts;
  const maxRows = opts.maxRows ?? 500;

  /** Open an existing db read-only; never creates a file on a read path. */
  const openRead = (p: string, dbArg: unknown): InstanceType<typeof Database> => {
    try {
      return new Database(p, { readonly: true, fileMustExist: true });
    } catch (e) {
      throw new RotorError("E_TOOL", `sqlite open failed: ${(e as Error).message}`, { context: { db: dbArg }, cause: e });
    }
  };

  const tools: ToolSpec[] = [
    {
      name: "sqlite.query",
      version: 1,
      description: "Run a read-only SELECT/PRAGMA against a SQLite db under the workspace. Returns bounded rows.",
      effect: "reading",
      grants: ["fs.read"],
      input: {
        type: "object",
        properties: {
          db: { type: "string", description: "Workspace-relative path to the .db file" },
          sql: { type: "string" },
          params: { description: "Positional array or named object of bind parameters" },
        },
        required: ["db", "sql"],
      },
      output: { type: "object", properties: { rows: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.db);
        const sql = String(args.sql ?? "").trim();
        if (!sql) throw new RotorError("E_MISSING_INPUT", "sqlite.query requires `sql`");
        const kw = firstKeyword(sql);
        if (!["select", "with", "pragma", "explain"].includes(kw)) {
          throw new RotorError("E_POLICY_DENIED", `sqlite.query is read-only; \`${kw || sql.slice(0, 30)}\` is not — use sqlite.exec`, { context: { db: args.db } });
        }
        const bound = bindParams(args.params);
        const db = openRead(p, args.db);
        try {
          const stmt = bound === undefined ? db.prepare(sql) : db.prepare(sql).bind(bound);
          // SQLite's own verdict, not just the keyword: catches WITH…DELETE, writing PRAGMAs.
          if (!stmt.readonly) {
            throw new RotorError("E_POLICY_DENIED", "sqlite.query refuses statements that modify the database — use sqlite.exec", { context: { db: args.db } });
          }
          if (!stmt.reader) return { rows: [], count: 0, truncated: false }; // readonly but returns no data
          const rows: Array<Record<string, unknown>> = [];
          let truncated = false;
          for (const r of stmt.iterate()) {
            if (rows.length >= maxRows) {
              truncated = true;
              break;
            }
            rows.push(boundRow(r as Record<string, unknown>));
          }
          return { rows, count: rows.length, truncated };
        } catch (e) {
          if (e instanceof RotorError) throw e;
          throw new RotorError("E_TOOL", `sqlite.query failed: ${(e as Error).message}`, { context: { db: args.db }, cause: e });
        } finally {
          db.close();
        }
      },
    },
    {
      name: "sqlite.exec",
      version: 1,
      description: "Execute a SQL statement (DDL/DML) against a SQLite db under the workspace; creates the db file if missing.",
      effect: "mutating",
      grants: ["fs.write"],
      input: {
        type: "object",
        properties: {
          db: { type: "string", description: "Workspace-relative path to the .db file" },
          sql: { type: "string" },
          params: { description: "Positional array or named object of bind parameters" },
        },
        required: ["db", "sql"],
      },
      output: { type: "object", properties: { changes: { type: "number" }, last_insert_rowid: { type: "number" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.db);
        const sql = String(args.sql ?? "").trim();
        if (!sql) throw new RotorError("E_MISSING_INPUT", "sqlite.exec requires `sql`");
        assertNoAttach(sql, args.db);
        const bound = bindParams(args.params);
        let db: InstanceType<typeof Database>;
        try {
          mkdirSync(dirname(p), { recursive: true });
          db = new Database(p);
        } catch (e) {
          throw new RotorError("E_TOOL", `sqlite open failed: ${(e as Error).message}`, { context: { db: args.db }, cause: e });
        }
        try {
          try {
            const info = bound === undefined ? db.prepare(sql).run() : db.prepare(sql).run(bound);
            return { changes: info.changes, last_insert_rowid: Number(info.lastInsertRowid) };
          } catch (e) {
            // A parameterless multi-statement script (schema setup) goes through exec().
            if (bound === undefined && /more than one statement/i.test((e as Error).message)) {
              db.exec(sql);
              const r = db.prepare("SELECT changes() AS c, last_insert_rowid() AS l").get() as { c: number; l: number | bigint };
              return { changes: r.c, last_insert_rowid: Number(r.l) };
            }
            throw new RotorError("E_TOOL", `sqlite.exec failed: ${(e as Error).message}`, { context: { db: args.db }, cause: e });
          }
        } catch (e) {
          if (e instanceof RotorError) throw e;
          throw new RotorError("E_TOOL", `sqlite.exec failed: ${(e as Error).message}`, { context: { db: args.db }, cause: e });
        } finally {
          db.close();
        }
      },
    },
    {
      name: "sqlite.tables",
      version: 1,
      description: "List user tables in a SQLite db under the workspace, with row counts.",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { db: { type: "string" } }, required: ["db"] },
      output: { type: "object", properties: { tables: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.db);
        const db = openRead(p, args.db);
        try {
          const all = userTables(db);
          const clipped = all.slice(0, maxRows);
          const tables = clipped.map((t) => ({
            name: t.name,
            rows: (db.prepare(`SELECT count(*) AS n FROM ${quoteIdent(t.name)}`).get() as { n: number }).n,
          }));
          return { tables, count: tables.length, truncated: all.length > maxRows };
        } catch (e) {
          if (e instanceof RotorError) throw e;
          throw new RotorError("E_TOOL", `sqlite.tables failed: ${(e as Error).message}`, { context: { db: args.db }, cause: e });
        } finally {
          db.close();
        }
      },
    },
    {
      name: "sqlite.schema",
      version: 1,
      description: "Describe table schemas (DDL + columns with type/pk/notnull) of a SQLite db; optionally one table.",
      effect: "reading",
      grants: ["fs.read"],
      input: { type: "object", properties: { db: { type: "string" }, table: { type: "string" } }, required: ["db"] },
      output: { type: "object", properties: { schema: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const p = resolveInRoot(root, args.db);
        const db = openRead(p, args.db);
        try {
          let picked = userTables(db);
          if (args.table != null && args.table !== "") {
            picked = picked.filter((t) => t.name === String(args.table));
            if (picked.length === 0) {
              throw new RotorError("E_TOOL", `sqlite.schema: no such table \`${String(args.table)}\``, { context: { db: args.db, table: args.table } });
            }
          }
          const clipped = picked.slice(0, maxRows);
          const schema = clipped.map((t) => ({
            table: t.name,
            sql: String(t.sql ?? "").slice(0, MAX_CELL),
            columns: tableColumns(db, t.name),
          }));
          return { schema, count: schema.length, truncated: picked.length > maxRows };
        } catch (e) {
          if (e instanceof RotorError) throw e;
          throw new RotorError("E_TOOL", `sqlite.schema failed: ${(e as Error).message}`, { context: { db: args.db }, cause: e });
        } finally {
          db.close();
        }
      },
    },
  ];

  return { name: "db", version: "1.0.0", tools };
}

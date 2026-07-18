/**
 * `db` pack — SQLite over sandboxed workspace files. Exercises the full
 * create → insert → query → introspect loop against a real mkdtemp workspace,
 * plus the guardrails: read-only enforcement on sqlite.query (keyword AND
 * SQLite's own readonly verdict), the row cap with `truncated`, and sandbox
 * escapes refusing with E_POLICY_DENIED.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { BasicConnections } from "../../src/plugins/connections.js";
import { dbPack } from "../../src/tools/db.js";

let root: string;
beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "glyphh-db-"));
});
afterAll(() => rmSync(root, { recursive: true, force: true }));

function withPack(pack: { tools: { name: string; handler: (a: Record<string, unknown>) => unknown }[] }) {
  const c = new BasicConnections();
  for (const t of pack.tools) c.register(t.name, t.handler);
  return c;
}
const call = async (c: BasicConnections, name: string, args: Record<string, unknown>) => {
  const r = await c.dispatch(name, args);
  if (!r.ok) throw new Error(r.error);
  return r.result as Record<string, unknown>;
};

describe("db pack — sandbox", () => {
  it("refuses ATTACH/DETACH so a db file cannot escape the workspace root", async () => {
    const c = withPack(dbPack({ root }));
    const escape = join(tmpdir(), `rrotor-escape-${Date.now()}.db`);
    await expect(
      call(c, "sqlite.exec", { db: "app.db", sql: `ATTACH DATABASE '${escape}' AS e; CREATE TABLE e.t(x);` }),
    ).rejects.toThrow(/ATTACH|sandbox/i);
    expect(existsSync(escape)).toBe(false);
    // Also blocked mid-script (multi-statement).
    await expect(
      call(c, "sqlite.exec", { db: "app.db", sql: `CREATE TABLE a(x); ATTACH DATABASE '${escape}' AS e;` }),
    ).rejects.toThrow(/ATTACH|sandbox/i);
    expect(existsSync(escape)).toBe(false);
  });
});

describe("db pack — sqlite.exec", () => {
  it("creates the db file, runs DDL/DML, reports changes + last_insert_rowid", async () => {
    const c = withPack(dbPack({ root }));
    expect(existsSync(join(root, "app.db"))).toBe(false);
    await call(c, "sqlite.exec", { db: "app.db", sql: "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT)" });
    expect(existsSync(join(root, "app.db"))).toBe(true);

    const ins = await call(c, "sqlite.exec", { db: "app.db", sql: "INSERT INTO users (name, email) VALUES (?, ?)", params: ["ada", "ada@x.dev"] });
    expect(ins.changes).toBe(1);
    expect(ins.last_insert_rowid).toBe(1);

    // Named params.
    const ins2 = await call(c, "sqlite.exec", { db: "app.db", sql: "INSERT INTO users (name, email) VALUES (@name, @email)", params: { name: "bob", email: "b@x.dev" } });
    expect(ins2.last_insert_rowid).toBe(2);
  });

  it("runs a parameterless multi-statement script via exec fallback", async () => {
    const c = withPack(dbPack({ root }));
    const r = await call(c, "sqlite.exec", {
      db: "script.db",
      sql: "CREATE TABLE t (v TEXT); INSERT INTO t VALUES ('a'); INSERT INTO t VALUES ('b');",
    });
    expect(r.changes).toBe(1); // changes() reflects the last statement
    const q = await call(c, "sqlite.query", { db: "script.db", sql: "SELECT count(*) AS n FROM t" });
    expect((q.rows as Array<{ n: number }>)[0].n).toBe(2);
  });

  it("refuses missing sql, bad params type, and creates parent dirs", async () => {
    const c = withPack(dbPack({ root }));
    await expect(c.dispatch("sqlite.exec", { db: "app.db", sql: "  " })).resolves.toMatchObject({ ok: false });
    await expect(c.dispatch("sqlite.exec", { db: "app.db", sql: "SELECT 1", params: "nope" })).resolves.toMatchObject({ ok: false });
    await call(c, "sqlite.exec", { db: "nested/deep/x.db", sql: "CREATE TABLE z (a)" });
    expect(existsSync(join(root, "nested/deep/x.db"))).toBe(true);
  });

  it("surfaces SQL errors as structured failures", async () => {
    const c = withPack(dbPack({ root }));
    const r = await c.dispatch("sqlite.exec", { db: "app.db", sql: "INSERT INTO no_such_table VALUES (1)" });
    expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/no_such_table/) });
  });
});

describe("db pack — sqlite.query", () => {
  beforeAll(async () => {
    const c = withPack(dbPack({ root }));
    await call(c, "sqlite.exec", { db: "q.db", sql: "CREATE TABLE nums (n INTEGER, label TEXT)" });
    for (let i = 1; i <= 5; i++) {
      await call(c, "sqlite.exec", { db: "q.db", sql: "INSERT INTO nums VALUES (?, ?)", params: [i, `row-${i}`] });
    }
  });

  it("selects rows with positional and named params", async () => {
    const c = withPack(dbPack({ root }));
    const r = await call(c, "sqlite.query", { db: "q.db", sql: "SELECT n, label FROM nums WHERE n > ? ORDER BY n", params: [3] });
    expect(r.count).toBe(2);
    expect(r.truncated).toBe(false);
    expect(r.rows).toEqual([
      { n: 4, label: "row-4" },
      { n: 5, label: "row-5" },
    ]);
    const named = await call(c, "sqlite.query", { db: "q.db", sql: "SELECT label FROM nums WHERE n = @n", params: { n: 2 } });
    expect((named.rows as Array<{ label: string }>)[0].label).toBe("row-2");
  });

  it("caps rows at maxRows and flags truncated", async () => {
    const c = withPack(dbPack({ root, maxRows: 3 }));
    const r = await call(c, "sqlite.query", { db: "q.db", sql: "SELECT * FROM nums ORDER BY n" });
    expect(r.count).toBe(3);
    expect((r.rows as unknown[]).length).toBe(3);
    expect(r.truncated).toBe(true);
  });

  it("allows read PRAGMAs", async () => {
    const c = withPack(dbPack({ root }));
    const r = await call(c, "sqlite.query", { db: "q.db", sql: "PRAGMA user_version" });
    expect((r.rows as Array<{ user_version: number }>)[0].user_version).toBe(0);
  });

  it("refuses writes with E_POLICY_DENIED — by keyword and by SQLite's readonly verdict", async () => {
    const c = withPack(dbPack({ root }));
    // Keyword gate: plain DML.
    const ins = await c.dispatch("sqlite.query", { db: "q.db", sql: "INSERT INTO nums VALUES (9, 'x')" });
    expect(ins).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    // Readonly verdict: a write smuggled behind an allowed keyword (WITH … DELETE).
    const smuggled = await c.dispatch("sqlite.query", { db: "q.db", sql: "WITH x AS (SELECT 1) DELETE FROM nums" });
    expect(smuggled).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    // Nothing actually changed.
    const r = await call(c, "sqlite.query", { db: "q.db", sql: "SELECT count(*) AS n FROM nums" });
    expect((r.rows as Array<{ n: number }>)[0].n).toBe(5);
  });

  it("does not create a db file on the read path; missing db is a structured error", async () => {
    const c = withPack(dbPack({ root }));
    const r = await c.dispatch("sqlite.query", { db: "ghost.db", sql: "SELECT 1" });
    expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/E_TOOL/) });
    expect(existsSync(join(root, "ghost.db"))).toBe(false);
  });
});

describe("db pack — sqlite.tables + sqlite.schema", () => {
  beforeAll(async () => {
    const c = withPack(dbPack({ root }));
    await call(c, "sqlite.exec", {
      db: "meta.db",
      sql: "CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL); CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER);",
    });
    await call(c, "sqlite.exec", { db: "meta.db", sql: "INSERT INTO authors (name) VALUES ('le guin')" });
  });

  it("lists user tables with row counts", async () => {
    const c = withPack(dbPack({ root }));
    const r = await call(c, "sqlite.tables", { db: "meta.db" });
    expect(r.tables).toEqual([
      { name: "authors", rows: 1 },
      { name: "books", rows: 0 },
    ]);
    expect(r.truncated).toBe(false);
  });

  it("describes all tables, and a single table on request", async () => {
    const c = withPack(dbPack({ root }));
    const all = await call(c, "sqlite.schema", { db: "meta.db" });
    expect((all.schema as Array<{ table: string }>).map((s) => s.table)).toEqual(["authors", "books"]);

    const one = await call(c, "sqlite.schema", { db: "meta.db", table: "authors" });
    const s = (one.schema as Array<{ table: string; sql: string; columns: Array<{ name: string; type: string; pk: boolean; notnull: boolean }> }>)[0];
    expect(s.table).toBe("authors");
    expect(s.sql).toMatch(/CREATE TABLE authors/);
    expect(s.columns).toEqual([
      { name: "id", type: "INTEGER", pk: true, notnull: false },
      { name: "name", type: "TEXT", pk: false, notnull: true },
    ]);
  });

  it("errors on an unknown table", async () => {
    const c = withPack(dbPack({ root }));
    const r = await c.dispatch("sqlite.schema", { db: "meta.db", table: "nope" });
    expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/no such table/) });
  });
});

describe("db pack — sandbox", () => {
  it("refuses db paths that escape the workspace with E_POLICY_DENIED", async () => {
    const c = withPack(dbPack({ root }));
    for (const tool of ["sqlite.query", "sqlite.exec", "sqlite.tables", "sqlite.schema"]) {
      const r = await c.dispatch(tool, { db: "../evil.db", sql: "SELECT 1" });
      expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    }
    await expect(c.dispatch("sqlite.query", { db: "", sql: "SELECT 1" })).resolves.toMatchObject({ ok: false });
  });
});

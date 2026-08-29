import { describe, it, expect } from "vitest";
import { ThreadStore } from "../../src/harness/threads.js";
import type { PgLike } from "../../src/exec/pgvector-store.js";
import type { Principal } from "../../src/auth/introspect.js";

/** A client that records every statement instead of running one. */
function recorder() {
  const sql: string[] = [];
  const db = {
    query: async (text: string) => { sql.push(String(text).trim().split("\n")[0]!.trim()); return { rows: [] }; },
    exec: async (text: string) => { sql.push("DDL:" + String(text).trim().slice(0, 12)); },
  } as unknown as PgLike;
  return { db, sql };
}

const p = { orgId: "3b028fb8-b6e1-45e8-9895-e3f80670bb98", userId: "u1" } as unknown as Principal;

describe("ThreadStore — the tenant pin", () => {
  // The 2026-08-29 leak: the stator is reached through pgbouncer in TRANSACTION
  // mode, so a bare `SET search_path` is its own transaction and the queries after
  // it get a different backend — one still carrying another org's schema. A whole
  // thread landed in a stranger's tenant. pool max 1 + the op mutex cannot prevent
  // that; only running the op inside one transaction can.
  it("pins with SET LOCAL inside a transaction, not a bare session SET", async () => {
    const { db, sql } = recorder();
    const store = await ThreadStore.create({ client: db });
    await store.list(p);
    expect(sql[0]).toBe("BEGIN");
    const sets = sql.filter((s) => s.includes("search_path"));
    expect(sets).toHaveLength(1);
    expect(sets[0]).toMatch(/^SET LOCAL /);
    expect(sql).toContain("COMMIT");
    // The pin must come before any query it is meant to scope.
    expect(sql.indexOf("BEGIN")).toBeLessThan(sql.indexOf(sets[0]!));
  });

  it("does not mark an org ensured until its DDL has committed", async () => {
    const sql: string[] = [];
    let calls = 0;
    const db = {
      query: async (text: string) => {
        const t = String(text).trim();
        sql.push(t.split("\n")[0]!.trim());
        // Fail the first operation AFTER the DDL, so the transaction rolls back.
        if (t.startsWith("SELECT") && ++calls === 1) throw new Error("boom");
        return { rows: [] };
      },
      exec: async () => { sql.push("DDL"); },
    } as unknown as PgLike;
    const store = await ThreadStore.create({ client: db });
    await expect(store.list(p)).rejects.toThrow("boom");
    expect(sql).toContain("ROLLBACK");
    // Second attempt must re-run the DDL — the rolled-back schema is not "ensured".
    sql.length = 0;
    await store.list(p);
    expect(sql).toContain("DDL");
  });
});

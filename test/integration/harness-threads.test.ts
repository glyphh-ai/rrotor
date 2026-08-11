/**
 * Session-thread persistence on the stator (harness/threads.ts), exercised
 * against an in-process PGlite instance so the real SQL runs in CI: the
 * ensure-DDL (byte-matching the server's canonical migration) applies
 * idempotently, a run writes its transcript (user turn at start, tool
 * breadcrumbs, streamed assistant text) under the introspected principal in
 * the desktop's CodeMsg grammar, the /threads routes serve list/full/LWW-push/
 * tombstone owner-scoped behind the same auth gate as the run endpoints,
 * stale writes lose, and one user can never see or touch another's threads.
 */

import { describe, it, expect, afterEach } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PGlite } from "@electric-sql/pglite";

import { startHarnessServer } from "../../src/harness/server.js";
import { ThreadStore, schemaForOrg, statorConfigured, warnIfUnrecordable } from "../../src/harness/threads.js";
import type { ThreadMsg } from "../../src/harness/threads.js";
import type { QueryFn } from "../../src/harness/engine.js";
import type { PgLike } from "../../src/exec/pgvector-store.js";
import type { Introspector, Principal } from "../../src/auth/introspect.js";
import { createLogger } from "../../src/obs/logger.js";

const ORG = "0f0e0d0c-0b0a-4908-8706-050403020100";
const ORG_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const ALICE: Principal = { orgId: ORG, userId: "11111111-1111-4111-8111-111111111111" };
const BOB: Principal = { orgId: ORG, userId: "22222222-2222-4222-8222-222222222222" };
/** Carol lives in ANOTHER ORG — her rows must land in another schema. */
const CAROL: Principal = { orgId: ORG_B, userId: "33333333-3333-4333-8333-333333333333" };

/** An introspector that admits everyone AS Alice, or per-bearer when the
 *  caller sends `Bearer alice|bob|carol` — the principal seam under test. */
const asUsers: Introspector = {
  enabled: true,
  authorize: (bearer) =>
    Promise.resolve({ ok: true, status: 200, principal: bearer === "bob" ? BOB : bearer === "carol" ? CAROL : ALICE }),
};
const as = (who: "alice" | "bob" | "carol"): { authorization: string } => ({ authorization: `Bearer ${who}` });

const open: PGlite[] = [];
async function pglite(): Promise<PgLike> {
  const db = await PGlite.create();
  open.push(db);
  return db as unknown as PgLike;
}
const servers: Server[] = [];
afterEach(async () => {
  for (const s of servers.splice(0)) await new Promise<void>((r) => s.close(() => r()));
  while (open.length) await open.pop()!.close();
});

function podEnv(): NodeJS.ProcessEnv {
  return {
    GLYPHH_GATEWAY_URL: "https://gw.test",
    GLYPHH_RUNTIME_TOKEN: "gy_rt_pod_secret",
    HARNESS_HOME: mkdtempSync(join(tmpdir(), "pod-")),
  } as NodeJS.ProcessEnv;
}

async function boot(queryFn: QueryFn, threads: ThreadStore | null, auth: Introspector = asUsers): Promise<string> {
  const server = startHarnessServer(0, { env: podEnv(), engine: { queryFn }, threads, auth });
  servers.push(server);
  await new Promise<void>((r) => server.once("listening", () => r()));
  return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
}

/** A run that streams text, uses one tool, then answers — the full grammar. */
const toolingQuery: QueryFn = () =>
  (async function* () {
    yield { type: "assistant", message: { content: [{ type: "tool_use", id: "tu-1", name: "Read", input: { file_path: "/w/a.ts" } }] } };
    yield { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "tu-1", content: "export const a = 1;" }] } };
    yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "the answer" } } };
    yield { type: "result", subtype: "success", result: "the answer" };
  })();

/** Poll until the thread's persisted message count reaches `n` (recorder
 *  writes drain asynchronously after the terminal frame). */
async function untilMessages(base: string, id: string, n: number, timeoutMs = 5000): Promise<{ messages: ThreadMsg[] }> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const res = await fetch(`${base}/threads/${id}`, { headers: as("alice") });
    if (res.status === 200) {
      const t = (await res.json()) as { messages: ThreadMsg[] };
      if (t.messages.length >= n) return t;
    }
    if (Date.now() > deadline) throw new Error(`thread ${id} never reached ${n} messages`);
    await new Promise((r) => setTimeout(r, 20));
  }
}

describe("thread store — per-org schemas", () => {
  it("first touch of an org creates its schema + tables (server convention), idempotently", async () => {
    const db = await pglite();
    const first = await ThreadStore.create({ client: db });
    await first.touch(ALICE, "c1", 100, { mode: "chat" });
    // The schema exists under the server's deterministic name.
    const schema = schemaForOrg(ORG);
    expect(schema).toBe("org_0f0e0d0c0b0a49088706050403020100");
    const found = await db.query("SELECT schema_name FROM information_schema.schemata WHERE schema_name = $1", [schema]);
    expect(found.rows).toHaveLength(1);
    // A second pod over the SAME database re-ensures harmlessly, data intact.
    const second = await ThreadStore.create({ client: db });
    expect((await second.list(ALICE)).map((t) => t.id)).toEqual(["c1"]);
  });

  it("two orgs through ONE pod land in separate schemas — same thread id, no bleed", async () => {
    const db = await pglite();
    const store = await ThreadStore.create({ client: db });
    const base = await boot(toolingQuery, store);
    const putAs = (who: "alice" | "carol", title: string): Promise<Response> =>
      fetch(`${base}/threads/cshared`, {
        method: "PUT",
        headers: as(who),
        body: JSON.stringify({ title, messages: [{ role: "user", text: title, at: 1 }], updatedAt: 100 }),
      });
    expect((await putAs("alice", "A's thread")).status).toBe(200);
    expect((await putAs("carol", "B's thread")).status).toBe(200); // same id, different org — no conflict

    // Each org reads its OWN row through the API…
    expect(((await (await fetch(`${base}/threads/cshared`, { headers: as("alice") })).json()) as { title: string }).title).toBe("A's thread");
    expect(((await (await fetch(`${base}/threads/cshared`, { headers: as("carol") })).json()) as { title: string }).title).toBe("B's thread");

    // …because the rows are PHYSICALLY separate, in the per-org schemas the
    // server's tenantDb routing reads.
    const inA = await db.query(`SELECT title FROM "${schemaForOrg(ORG)}".threads`);
    const inB = await db.query(`SELECT title FROM "${schemaForOrg(ORG_B)}".threads`);
    expect(inA.rows.map((r) => r.title)).toEqual(["A's thread"]);
    expect(inB.rows.map((r) => r.title)).toEqual(["B's thread"]);
  });

  it("rejects an orgId that derives an unsafe schema name", () => {
    expect(() => schemaForOrg("bad; DROP SCHEMA public")).toThrow(/unsafe schema name/);
    expect(() => schemaForOrg("")).toThrow(/unsafe schema name/);
  });
});

describe("thread persistence — a run writes its transcript", () => {
  it("persists the user turn, tool breadcrumb, and streamed answer under the caller's principal", async () => {
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(toolingQuery, store);
    const res = await fetch(`${base}/run`, {
      method: "POST",
      headers: as("alice"),
      body: JSON.stringify({ prompt: "read a.ts", sessionId: "c1a2b3", mode: "code" }),
    });
    expect(res.status).toBe(200);

    // user turn → tool row (⚙ rewritten to ✓ in place) → assistant text. The
    // breadcrumb carries the call's KEY INPUT (engine toolTitle) — a bare
    // "✓ Read" is exactly the poverty the title exists to remove.
    const t = await untilMessages(base, "c1a2b3", 3);
    expect(t.messages.map((m) => [m.role, m.kind ?? "", m.text])).toEqual([
      ["user", "", "read a.ts"],
      ["assistant", "tool", "✓ Read /w/a.ts"],
      ["assistant", "", "the answer"],
    ]);

    // The list shows it to its OWNER, metadata only — and to nobody else.
    const list = (await (await fetch(`${base}/threads`, { headers: as("alice") })).json()) as { threads: Array<Record<string, unknown>> };
    expect(list.threads).toHaveLength(1);
    expect(list.threads[0]).toMatchObject({ id: "c1a2b3", mode: "code", messageCount: 3 });
    expect(list.threads[0]).not.toHaveProperty("messages");
    const other = (await (await fetch(`${base}/threads`, { headers: as("bob") })).json()) as { threads: unknown[] };
    expect(other.threads).toHaveLength(0);
  });

  it("persists the user turn even when the run fails, and appends the error", async () => {
    const failing: QueryFn = () =>
      (async function* () {
        yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "partial" } } };
        throw new Error("gateway unreachable");
      })();
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(failing, store);
    await fetch(`${base}/run`, { method: "POST", headers: as("alice"), body: JSON.stringify({ prompt: "doomed", sessionId: "cfail" }) });
    const t = await untilMessages(base, "cfail", 2);
    expect(t.messages[0]).toMatchObject({ role: "user", text: "doomed" });
    expect(t.messages[1].text).toBe("partial\n\n⚠ gateway unreachable");
  });
});

describe("thread routes — LWW push and tombstone", () => {
  const put = (base: string, id: string, body: Record<string, unknown>, who: "alice" | "bob" = "alice"): Promise<Response> =>
    fetch(`${base}/threads/${id}`, { method: "PUT", headers: as(who), body: JSON.stringify(body) });

  it("a newer PUT applies (creating the thread), a stale PUT 409s with the winning stamp", async () => {
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(toolingQuery, store);
    const msgs: ThreadMsg[] = [{ role: "user", text: "hi", at: 100 }, { role: "assistant", text: "hello", at: 150 }];
    const ok = await put(base, "cpush", { title: "Greetings", mode: "chat", messages: msgs, updatedAt: 200 });
    expect(ok.status).toBe(200);
    expect(await ok.json()).toEqual({ ok: true, updatedAt: 200 });

    const stale = await put(base, "cpush", { title: "Old title", updatedAt: 100 });
    expect(stale.status).toBe(409);
    expect(((await stale.json()) as { updatedAt: number }).updatedAt).toBe(200);

    // The stale write changed nothing; the newer state stands.
    const t = (await (await fetch(`${base}/threads/cpush`, { headers: as("alice") })).json()) as Record<string, unknown>;
    expect(t).toMatchObject({ title: "Greetings", mode: "chat", updatedAt: 200 });
    expect((t.messages as ThreadMsg[]).map((m) => m.text)).toEqual(["hi", "hello"]);

    // A PUT without the LWW stamp is malformed.
    expect((await put(base, "cpush", { title: "x" })).status).toBe(400);
  });

  it("DELETE tombstones: hidden from the list, 404 on read, later PUTs lose", async () => {
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(toolingQuery, store);
    await put(base, "cgone", { title: "Doomed", messages: [{ role: "user", text: "x", at: 1 }], updatedAt: 100 });
    expect((await fetch(`${base}/threads/cgone`, { method: "DELETE", headers: as("alice") })).status).toBe(200);

    const list = (await (await fetch(`${base}/threads`, { headers: as("alice") })).json()) as { threads: unknown[] };
    expect(list.threads).toHaveLength(0);
    expect((await fetch(`${base}/threads/cgone`, { headers: as("alice") })).status).toBe(404);
    // The tombstone is terminal — even a far-future stamp does not resurrect.
    expect((await put(base, "cgone", { title: "Back?", updatedAt: Date.now() + 1e9 })).status).toBe(409);
    // Deleting the unknown 404s.
    expect((await fetch(`${base}/threads/cnever`, { method: "DELETE", headers: as("alice") })).status).toBe(404);
  });

  it("owner-only: another user's thread reads as absent and cannot be pushed or deleted", async () => {
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(toolingQuery, store);
    await put(base, "calice", { title: "Mine", messages: [{ role: "user", text: "hi", at: 1 }], updatedAt: 100 }, "alice");

    // Bob sees nothing — not in the list, not by id, not via PUT (even with a
    // winning stamp — no hijack), not via DELETE.
    expect(((await (await fetch(`${base}/threads`, { headers: as("bob") })).json()) as { threads: unknown[] }).threads).toHaveLength(0);
    expect((await fetch(`${base}/threads/calice`, { headers: as("bob") })).status).toBe(404);
    expect((await put(base, "calice", { title: "Hijack", updatedAt: Date.now() + 1e9 }, "bob")).status).toBe(404);
    expect((await fetch(`${base}/threads/calice`, { method: "DELETE", headers: as("bob") })).status).toBe(404);

    // Alice's thread is untouched.
    const t = (await (await fetch(`${base}/threads/calice`, { headers: as("alice") })).json()) as { title: string };
    expect(t.title).toBe("Mine");
  });
});

describe("thread routes — availability and auth", () => {
  it("503s when no stator is configured (persistence off), runs still serve", async () => {
    const base = await boot(toolingQuery, null);
    expect((await fetch(`${base}/threads`, { headers: as("alice") })).status).toBe(503);
    expect((await fetch(`${base}/run`, { method: "POST", headers: as("alice"), body: JSON.stringify({ prompt: "x" }) })).status).toBe(200);
  });

  it("503s without a principal (auth off): per-user threads cannot be scoped", async () => {
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(toolingQuery, store, { enabled: false, authorize: () => Promise.resolve({ ok: true, status: 200 }) });
    expect((await fetch(`${base}/threads`)).status).toBe(503);
    // And an unattributed run records nothing.
    await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "x", sessionId: "cnoone" }) });
    await new Promise((r) => setTimeout(r, 100));
    expect((await store.list(ALICE)).length).toBe(0);
  });

  it("sits behind the same introspection gate as the run endpoints", async () => {
    const denyAll: Introspector = { enabled: true, authorize: () => Promise.resolve({ ok: false, status: 401, reason: "no token" }) };
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(toolingQuery, store, denyAll);
    expect((await fetch(`${base}/threads`)).status).toBe(401);
    expect((await fetch(`${base}/threads/c1`, { method: "PUT", body: "{}" })).status).toBe(401);
  });

  it("warns LOUDLY at boot when the stator is configured but introspection is off", () => {
    const statorEnv = { ROTOR_STATOR_BACKEND: "pgvector", ROTOR_STATOR_URL: "postgres://stator" } as NodeJS.ProcessEnv;
    expect(statorConfigured(statorEnv)).toBe(true);
    expect(statorConfigured({} as NodeJS.ProcessEnv)).toBe(false);

    const lines: string[] = [];
    const logger = createLogger({ level: "warn", write: (l) => lines.push(l) });
    // Stator on + auth off → the warn fires and says threads will not persist.
    expect(warnIfUnrecordable(statorEnv, false, logger)).toBe(true);
    expect(lines.join("\n")).toMatch(/threads will NOT persist/);
    // Auth on, or no stator → silent.
    expect(warnIfUnrecordable(statorEnv, true, logger)).toBe(false);
    expect(warnIfUnrecordable({} as NodeJS.ProcessEnv, false, logger)).toBe(false);
    expect(lines).toHaveLength(1);
  });
});

describe("POST /run — runtime token from the Authorization bearer", () => {
  it("defaults `runtimeToken` from the header so callers don't send the token twice", async () => {
    // No GLYPHH_RUNTIME_TOKEN in the env and none in the body: the bearer
    // (which IS the session's runtime token) carries the run.
    const env = { GLYPHH_GATEWAY_URL: "https://gw.test", HARNESS_HOME: mkdtempSync(join(tmpdir(), "pod-")) } as NodeJS.ProcessEnv;
    const server = startHarnessServer(0, { env, engine: { queryFn: toolingQuery }, threads: null, auth: asUsers });
    servers.push(server);
    await new Promise<void>((r) => server.once("listening", () => r()));
    const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

    const withBearer = await fetch(`${base}/run`, { method: "POST", headers: as("alice"), body: JSON.stringify({ prompt: "x" }) });
    expect(withBearer.status).toBe(200);
    // Without any token anywhere the run is still rejected.
    const bare = await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "x" }) });
    expect(bare.status).toBe(400);
    expect(((await bare.json()) as { detail: string }).detail).toMatch(/runtime token/);
  });
});

describe("POST /run — the client's threadId", () => {
  it("records the transcript under `threadId` while sessionId stays the token's session", async () => {
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(toolingQuery, store);
    const res = await fetch(`${base}/run`, {
      method: "POST",
      headers: as("alice"),
      body: JSON.stringify({ prompt: "read a.ts", sessionId: "sess_prov1", threadId: "csmoke5", mode: "code" }),
    });
    expect(res.status).toBe(200);
    expect(((await res.json()) as { sessionId: string }).sessionId).toBe("sess_prov1");

    // The transcript lives under the CLIENT's id, not the provisioned session's.
    const t = await untilMessages(base, "csmoke5", 3);
    expect(t.messages[0]).toMatchObject({ role: "user", text: "read a.ts" });
    const list = (await (await fetch(`${base}/threads`, { headers: as("alice") })).json()) as { threads: Array<{ id: string }> };
    expect(list.threads.map((x) => x.id)).toEqual(["csmoke5"]);
    expect((await fetch(`${base}/threads/sess_prov1`, { headers: as("alice") })).status).toBe(404);
  });

  it("400s an invalid threadId", async () => {
    const base = await boot(toolingQuery, null);
    for (const threadId of ["bad id!", "a".repeat(65), ""]) {
      const res = await fetch(`${base}/run`, { method: "POST", headers: as("alice"), body: JSON.stringify({ prompt: "x", threadId }) });
      expect(res.status).toBe(400);
      expect(((await res.json()) as { detail: string }).detail).toMatch(/threadId/);
    }
  });

  it("threadId does not loosen auth — a token bound to another session still 401s", async () => {
    // The real gate binds the bearer to the pod's provisioned session; a
    // mismatched token is denied no matter what threadId rides the body.
    const sessionBound: Introspector = {
      enabled: true,
      authorize: (bearer) =>
        bearer === "sess-token"
          ? Promise.resolve({ ok: true, status: 200, principal: ALICE })
          : Promise.resolve({ ok: false, status: 401, reason: "session mismatch" }),
    };
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(toolingQuery, store, sessionBound);
    const denied = await fetch(`${base}/run`, {
      method: "POST",
      headers: { authorization: "Bearer other-session" },
      body: JSON.stringify({ prompt: "x", threadId: "csmoke5" }),
    });
    expect(denied.status).toBe(401);
    expect(((await denied.json()) as { detail: string }).detail).toMatch(/session mismatch/);
    const admitted = await fetch(`${base}/run`, {
      method: "POST",
      headers: { authorization: "Bearer sess-token" },
      body: JSON.stringify({ prompt: "x", threadId: "csmoke5" }),
    });
    expect(admitted.status).toBe(200);
  });
});

describe("POST /run — shared-pod per-run session binding", () => {
  // A shared pod's introspector admits any ACTIVE token and surfaces the
  // session the token is bound to; the harness enforces the binding per run.
  const shared: Introspector = {
    enabled: true,
    authorize: () => Promise.resolve({ ok: true, status: 200, principal: ALICE, sessionId: "sess_tok" }),
  };

  it("rejects a body sessionId that isn't the token's; adopts the token's when absent", async () => {
    const base = await boot(toolingQuery, null, shared);
    const mismatch = await fetch(`${base}/run`, {
      method: "POST",
      headers: as("alice"),
      body: JSON.stringify({ prompt: "x", sessionId: "sess_other" }),
    });
    expect(mismatch.status).toBe(401);
    expect(((await mismatch.json()) as { detail: string }).detail).toBe("session mismatch");

    // No body sessionId → the run binds to (and reports) the token's session.
    const adopted = await fetch(`${base}/run`, { method: "POST", headers: as("alice"), body: JSON.stringify({ prompt: "x" }) });
    expect(adopted.status).toBe(200);
    const { runId, sessionId } = (await adopted.json()) as { runId: string; sessionId: string };
    expect(sessionId).toBe("sess_tok");
    // Let it finish — the pod caps at one live run.
    for (;;) {
      const st = (await (await fetch(`${base}/runs/${runId}`, { headers: as("alice") })).json()) as { status: string; sessionId: string };
      if (st.status !== "running") {
        expect(st.sessionId).toBe("sess_tok");
        break;
      }
      await new Promise((r) => setTimeout(r, 20));
    }

    // A matching body sessionId is redundant but welcome.
    const matching = await fetch(`${base}/run`, {
      method: "POST",
      headers: as("alice"),
      body: JSON.stringify({ prompt: "x", sessionId: "sess_tok" }),
    });
    expect(matching.status).toBe(200);
  });
});

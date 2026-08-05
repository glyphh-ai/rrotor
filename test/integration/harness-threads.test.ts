/**
 * Session-thread persistence on the stator (harness/threads.ts), exercised
 * against an in-process PGlite instance so the real SQL runs in CI: the
 * migration applies idempotently, a run writes its transcript (user turn at
 * start, tool breadcrumbs, streamed assistant text) in the desktop's CodeMsg
 * grammar, the /threads routes serve list/full/LWW-push/tombstone behind the
 * same auth gate as the run endpoints, and stale writes lose.
 */

import { describe, it, expect, afterEach } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PGlite } from "@electric-sql/pglite";

import { startHarnessServer } from "../../src/harness/server.js";
import { ThreadStore } from "../../src/harness/threads.js";
import type { ThreadMsg } from "../../src/harness/threads.js";
import type { QueryFn } from "../../src/harness/engine.js";
import type { PgLike } from "../../src/exec/pgvector-store.js";
import type { Introspector } from "../../src/auth/introspect.js";

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

async function boot(queryFn: QueryFn, threads: ThreadStore | null, auth?: Introspector): Promise<string> {
  const server = startHarnessServer(0, { env: podEnv(), engine: { queryFn }, threads, ...(auth ? { auth } : {}) });
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
    const res = await fetch(`${base}/threads/${id}`);
    if (res.status === 200) {
      const t = (await res.json()) as { messages: ThreadMsg[] };
      if (t.messages.length >= n) return t;
    }
    if (Date.now() > deadline) throw new Error(`thread ${id} never reached ${n} messages`);
    await new Promise((r) => setTimeout(r, 20));
  }
}

describe("thread store — migration", () => {
  it("applies idempotently over one database (a second create is a no-op)", async () => {
    const db = await pglite();
    const first = await ThreadStore.create({ client: db });
    await first.touch("c1", 100, { mode: "chat" });
    // A second pod over the SAME database: schema already there, data intact.
    const second = await ThreadStore.create({ client: db });
    expect((await second.list()).map((t) => t.id)).toEqual(["c1"]);
  });
});

describe("thread persistence — a run writes its transcript", () => {
  it("persists the user turn, tool breadcrumb, and streamed answer under the client's session id", async () => {
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(toolingQuery, store);
    const res = await fetch(`${base}/run`, {
      method: "POST",
      body: JSON.stringify({ prompt: "read a.ts", sessionId: "c1a2b3", mode: "code" }),
    });
    expect(res.status).toBe(200);

    // user turn → tool row (⚙ rewritten to ✓ in place) → assistant text.
    const t = await untilMessages(base, "c1a2b3", 3);
    expect(t.messages.map((m) => [m.role, m.kind ?? "", m.text])).toEqual([
      ["user", "", "read a.ts"],
      ["assistant", "tool", "✓ Read"],
      ["assistant", "", "the answer"],
    ]);

    // The list shows it, newest first, metadata only.
    const list = (await (await fetch(`${base}/threads`)).json()) as { threads: Array<Record<string, unknown>> };
    expect(list.threads).toHaveLength(1);
    expect(list.threads[0]).toMatchObject({ id: "c1a2b3", mode: "code", messageCount: 3 });
    expect(list.threads[0]).not.toHaveProperty("messages");
  });

  it("persists the user turn even when the run fails, and appends the error", async () => {
    const failing: QueryFn = () =>
      (async function* () {
        yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "partial" } } };
        throw new Error("gateway unreachable");
      })();
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(failing, store);
    await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "doomed", sessionId: "cfail" }) });
    const t = await untilMessages(base, "cfail", 2);
    expect(t.messages[0]).toMatchObject({ role: "user", text: "doomed" });
    expect(t.messages[1].text).toBe("partial\n\n⚠ gateway unreachable");
  });
});

describe("thread routes — LWW push and tombstone", () => {
  const put = (base: string, id: string, body: Record<string, unknown>): Promise<Response> =>
    fetch(`${base}/threads/${id}`, { method: "PUT", body: JSON.stringify(body) });

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
    const t = (await (await fetch(`${base}/threads/cpush`)).json()) as Record<string, unknown>;
    expect(t).toMatchObject({ title: "Greetings", mode: "chat", updatedAt: 200 });
    expect((t.messages as ThreadMsg[]).map((m) => m.text)).toEqual(["hi", "hello"]);

    // A PUT without the LWW stamp is malformed.
    expect((await put(base, "cpush", { title: "x" })).status).toBe(400);
  });

  it("DELETE tombstones: hidden from the list, 404 on read, later PUTs lose", async () => {
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(toolingQuery, store);
    await put(base, "cgone", { title: "Doomed", messages: [{ role: "user", text: "x", at: 1 }], updatedAt: 100 });
    expect((await fetch(`${base}/threads/cgone`, { method: "DELETE" })).status).toBe(200);

    const list = (await (await fetch(`${base}/threads`)).json()) as { threads: unknown[] };
    expect(list.threads).toHaveLength(0);
    expect((await fetch(`${base}/threads/cgone`)).status).toBe(404);
    // The tombstone is terminal — even a far-future stamp does not resurrect.
    expect((await put(base, "cgone", { title: "Back?", updatedAt: Date.now() + 1e9 })).status).toBe(409);
    // Deleting the unknown 404s.
    expect((await fetch(`${base}/threads/cnever`, { method: "DELETE" })).status).toBe(404);
  });
});

describe("thread routes — availability and auth", () => {
  it("503s when no stator is configured (persistence off), runs still serve", async () => {
    const base = await boot(toolingQuery, null);
    expect((await fetch(`${base}/threads`)).status).toBe(503);
    expect((await fetch(`${base}/run`, { method: "POST", body: JSON.stringify({ prompt: "x" }) })).status).toBe(200);
  });

  it("sits behind the same introspection gate as the run endpoints", async () => {
    const denyAll: Introspector = { enabled: true, authorize: () => Promise.resolve({ ok: false, status: 401, reason: "no token" }) };
    const store = await ThreadStore.create({ client: await pglite() });
    const base = await boot(toolingQuery, store, denyAll);
    expect((await fetch(`${base}/threads`)).status).toBe(401);
    expect((await fetch(`${base}/threads/c1`, { method: "PUT", body: "{}" })).status).toBe(401);
  });
});

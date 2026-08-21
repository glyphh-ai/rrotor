/**
 * app-worker-service.test.ts — the app-worker COMPOSITION ROOT
 * (src/app-worker/service.ts) against a MOCK control plane (a real node:http
 * server, the capability-bridge.test.ts pattern).
 *
 * What the assertions guard:
 *   - the MINT FLOW: the pod's `gy_rk_` runtime token mints the app's `gy_wk_`
 *     worker token ONCE per slug (coalesced + cached), and every data-plane
 *     call the worker script makes rides the MINTED token — never the pod's;
 *   - the INVOKE ROUNDTRIP: handler in → result out through the sandboxed
 *     executor, with `glyphh.call` reaching the app's data planes and the
 *     durable cron service wired by the same root;
 *   - the ORG BINDING: a caller org that is not the minted token's org is
 *     refused — this pod is not that org's execution home;
 *   - the TYPED ERROR SHAPES: not-installed (the server's install gate at
 *     mint), capability-denied (the bridge's grant wall), timeout (a wedged
 *     handler), no-handler — each with kind + HTTP status;
 *   - env wiring: `ROTOR_APP_WORKERS` gates the service, and missing
 *     control-plane coordinates keep it off rather than half-armed.
 */

import { describe, it, expect, beforeAll, afterAll, beforeEach } from "vitest";
import { createServer, type Server } from "node:http";
import { createHash } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  AppWorkerService,
  AppWorkerError,
  classifyWorkerError,
  appWorkersEnabled,
  appWorkerServiceFromEnv,
} from "../../src/app-worker/service.js";
import { MemoryCronStore } from "../../src/app-worker/cron.js";
import { buildAppBundle } from "../harness/app-fixtures.js";

const RUNTIME_TOKEN = "gy_rk_pod_credential";
const WORKER_TOKEN = "gy_wk_minted_for_demo";
const SLUG = "demo";
const ORG = "org-1";

/** The demo app's worker script: one handler per behaviour under test. */
const WORKER_SOURCE = `
glyphh.handle("echo", (args) => ({ echoed: args }));
glyphh.handle("note", (args) => glyphh.call("data.query", { op: "insert", table: "notes", values: args }));
glyphh.handle("denied", () => glyphh.call("vector.upsert", { items: [] }));
glyphh.handle("sleep", () => new Promise(() => {}));
glyphh.handle("sched", (args) => glyphh.call("cron.schedule", args));
glyphh.handle("crons", () => glyphh.call("cron.list", null));
`;

const BUNDLE = buildAppBundle(
  {
    slug: SLUG,
    worker: "worker.js",
    capabilities: ["data.query", "cron.schedule", "cron.list"],
  },
  { "worker.js": WORKER_SOURCE },
);
const BUNDLE_SHA = createHash("sha256").update(BUNDLE).digest("hex");

interface Seen {
  path: string;
  method: string;
  bearer: string | undefined;
}

let server: Server;
let base = "";
let seen: Seen[] = [];
let mintCount = 0;

beforeAll(async () => {
  server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const path = (req.url ?? "").split("?", 1)[0];
      const bearer = req.headers.authorization?.replace(/^Bearer /, "");
      seen.push({ path, method: req.method ?? "", bearer });
      const json = (status: number, payload: unknown) =>
        res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(payload));

      // ── mint: requires the POD's runtime token ──────────────────────────
      if (/^\/api\/apps\/[^/]+\/worker-token$/.test(path) && req.method === "POST") {
        if (bearer !== RUNTIME_TOKEN) {
          json(401, { error: { code: "E_UNAUTHENTICATED", message: "missing or invalid credentials" } });
          return;
        }
        const slug = path.split("/")[3]!;
        if (slug === "ghost") {
          json(403, { error: { code: "E_FORBIDDEN", message: "'ghost' is not installed in your org" } });
          return;
        }
        mintCount++;
        json(201, {
          data: {
            token: WORKER_TOKEN, appSlug: slug, orgId: ORG,
            prefix: WORKER_TOKEN.slice(0, 10), createdAt: new Date().toISOString(), rotatedAt: null,
          },
        });
        return;
      }
      // ── bundle rails: download-url (no presign) + buffered /source ──────
      if (path === `/api/apps/${SLUG}/bundle/download-url`) {
        if (bearer !== RUNTIME_TOKEN) { json(401, { error: { code: "E_UNAUTHENTICATED", message: "no" } }); return; }
        json(200, { data: { url: null, releaseId: "rel-1", version: "1.0.0", sha256: BUNDLE_SHA, sizeBytes: BUNDLE.length } });
        return;
      }
      if (path === `/api/apps/${SLUG}/source`) {
        res.writeHead(200, { "content-type": "application/zip" }).end(BUNDLE);
        return;
      }
      // ── the app's data plane: requires the MINTED worker token ──────────
      if (path === `/api/apps/${SLUG}/data/query`) {
        if (bearer !== WORKER_TOKEN) { json(401, { error: { code: "E_UNAUTHENTICATED", message: "invalid or revoked worker token" } }); return; }
        json(200, { data: { rows: [{ inserted: true }], rowCount: 1 } });
        return;
      }
      json(404, { error: { code: "E_NOT_FOUND", message: `no route ${path}` } });
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const addr = server.address() as { port: number };
  base = `http://127.0.0.1:${addr.port}`;
});

afterAll(async () => {
  await new Promise<void>((r) => server.close(() => r()));
});

beforeEach(() => {
  seen = [];
  mintCount = 0;
});

function makeService(over: Partial<ConstructorParameters<typeof AppWorkerService>[0]> = {}): AppWorkerService {
  return new AppWorkerService({
    controlPlaneUrl: base,
    runtimeToken: RUNTIME_TOKEN,
    cacheDir: mkdtempSync(join(tmpdir(), "app-worker-svc-")),
    cronStore: new MemoryCronStore(),
    ...over,
  });
}

describe("AppWorkerService — mint flow + invoke roundtrip", () => {
  it("mints once with the pod's runtime token, then invokes through the sandbox", async () => {
    const svc = makeService();
    try {
      const result = await svc.invoke(ORG, SLUG, "echo", { x: 1 });
      expect(result).toEqual({ echoed: { x: 1 } });

      // The mint carried the POD's credential, exactly once.
      const mints = seen.filter((s) => s.path.endsWith("/worker-token"));
      expect(mints).toHaveLength(1);
      expect(mints[0]!.bearer).toBe(RUNTIME_TOKEN);

      // A second invoke reuses the armed entry — no second mint.
      expect(await svc.invoke(ORG, SLUG, "echo", { y: 2 })).toEqual({ echoed: { y: 2 } });
      expect(mintCount).toBe(1);
    } finally {
      await svc.close();
    }
  });

  it("coalesces CONCURRENT first invokes into one mint", async () => {
    const svc = makeService();
    try {
      const [a, b] = await Promise.all([
        svc.invoke(ORG, SLUG, "echo", { n: 1 }),
        svc.invoke(ORG, SLUG, "echo", { n: 2 }),
      ]);
      expect(a).toEqual({ echoed: { n: 1 } });
      expect(b).toEqual({ echoed: { n: 2 } });
      expect(mintCount).toBe(1);
    } finally {
      await svc.close();
    }
  });

  it("the worker's glyphh.call rides the MINTED worker token, never the pod's", async () => {
    const svc = makeService();
    try {
      const result = await svc.invoke(ORG, SLUG, "note", { body: "hi" });
      expect(result).toEqual({ rows: [{ inserted: true }], rowCount: 1 });
      const dataCalls = seen.filter((s) => s.path.endsWith("/data/query"));
      expect(dataCalls).toHaveLength(1);
      expect(dataCalls[0]!.bearer).toBe(WORKER_TOKEN);
    } finally {
      await svc.close();
    }
  });

  it("wires the durable cron service into the bridge (schedule → list)", async () => {
    const svc = makeService();
    try {
      const scheduled = await svc.invoke(ORG, SLUG, "sched", { id: "daily", cron: "0 9 * * *", handler: "echo" });
      expect(scheduled).toEqual({ scheduled: true, id: "daily" });
      const listed = await svc.invoke(ORG, SLUG, "crons", {});
      expect(listed).toEqual([{ id: "daily", cron: "0 9 * * *", handler: "echo" }]);
    } finally {
      await svc.close();
    }
  });

  it("start() re-arms persisted schedules and a firing goes through the ensure path", async () => {
    // A pre-seeded store (what a pod restart finds): the firing must mint +
    // materialize an app this process has never invoked.
    const store = new MemoryCronStore();
    await store.upsert({ orgId: ORG, slug: SLUG, id: "boot", cron: "0 0 1 1 *", handler: "echo", args: null });
    const svc = makeService({ cronStore: store });
    try {
      await svc.start();
      // The schedule is armed (listable via the bridge) without ever firing.
      const listed = await svc.invoke(ORG, SLUG, "crons", {});
      expect(listed).toEqual([{ id: "boot", cron: "0 0 1 1 *", handler: "echo" }]);
    } finally {
      await svc.close();
    }
  });
});

describe("AppWorkerService — org binding + typed error shapes", () => {
  it("refuses a caller org that is not the minted token's org (forbidden)", async () => {
    const svc = makeService();
    try {
      const err = await svc.invoke("org-OTHER", SLUG, "echo", {}).catch((e: unknown) => e);
      expect(err).toBeInstanceOf(AppWorkerError);
      expect((err as AppWorkerError).kind).toBe("forbidden");
      expect((err as AppWorkerError).httpStatus).toBe(403);
    } finally {
      await svc.close();
    }
  });

  it("surfaces the server's install gate as not-installed (404)", async () => {
    const svc = makeService();
    try {
      const err = await svc.invoke(ORG, "ghost", "echo", {}).catch((e: unknown) => e);
      expect(err).toBeInstanceOf(AppWorkerError);
      expect((err as AppWorkerError).kind).toBe("not-installed");
      expect((err as AppWorkerError).httpStatus).toBe(404);
      // A failed arm does not wedge the slug — the next invoke retries (and
      // fails the same way, proving it re-reached the control plane).
      const again = await svc.invoke(ORG, "ghost", "echo", {}).catch((e: unknown) => e);
      expect((again as AppWorkerError).kind).toBe("not-installed");
      expect(seen.filter((s) => s.path === "/api/apps/ghost/worker-token")).toHaveLength(2);
    } finally {
      await svc.close();
    }
  });

  it("classifies the bridge's grant wall as capability-denied (403)", async () => {
    const svc = makeService();
    try {
      const err = await svc.invoke(ORG, SLUG, "denied", {}).catch((e: unknown) => e);
      expect(err).toBeInstanceOf(AppWorkerError);
      expect((err as AppWorkerError).kind).toBe("capability-denied");
      expect((err as AppWorkerError).httpStatus).toBe(403);
      expect((err as AppWorkerError).message).toMatch(/capability not granted: vector\.upsert/);
    } finally {
      await svc.close();
    }
  });

  it("classifies a wedged handler as timeout (504) — and the worker restarts fresh", async () => {
    const svc = makeService();
    try {
      const err = await svc.invoke(ORG, SLUG, "sleep", {}, 300).catch((e: unknown) => e);
      expect(err).toBeInstanceOf(AppWorkerError);
      expect((err as AppWorkerError).kind).toBe("timeout");
      expect((err as AppWorkerError).httpStatus).toBe(504);
      // The timed-out worker was disposed; the next invoke gets a fresh one.
      expect(await svc.invoke(ORG, SLUG, "echo", { back: true })).toEqual({ echoed: { back: true } });
    } finally {
      await svc.close();
    }
  }, 15_000);

  it("classifies an unregistered handler as no-handler (404)", async () => {
    const svc = makeService();
    try {
      const err = await svc.invoke(ORG, SLUG, "nope", {}).catch((e: unknown) => e);
      expect(err).toBeInstanceOf(AppWorkerError);
      expect((err as AppWorkerError).kind).toBe("no-handler");
      expect((err as AppWorkerError).httpStatus).toBe(404);
    } finally {
      await svc.close();
    }
  });

  it("classifyWorkerError falls back to invoke-error (500) for anything unrecognized", () => {
    const e = classifyWorkerError(new Error("something exploded"));
    expect(e.kind).toBe("invoke-error");
    expect(e.httpStatus).toBe(500);
    // And passes an already-typed error through unchanged.
    expect(classifyWorkerError(e)).toBe(e);
  });
});

describe("appWorkerServiceFromEnv — the mode switch", () => {
  it("is OFF by default and on for '1'/'true'", () => {
    expect(appWorkersEnabled({} as NodeJS.ProcessEnv)).toBe(false);
    expect(appWorkersEnabled({ ROTOR_APP_WORKERS: "0" } as NodeJS.ProcessEnv)).toBe(false);
    expect(appWorkersEnabled({ ROTOR_APP_WORKERS: "1" } as NodeJS.ProcessEnv)).toBe(true);
    expect(appWorkersEnabled({ ROTOR_APP_WORKERS: "true" } as NodeJS.ProcessEnv)).toBe(true);
  });

  it("returns null when the mode is off, and when the coordinates are incomplete", async () => {
    expect(await appWorkerServiceFromEnv({} as NodeJS.ProcessEnv)).toBeNull();
    // Enabled but unmintable — stays off rather than half-armed.
    expect(await appWorkerServiceFromEnv({ ROTOR_APP_WORKERS: "1" } as NodeJS.ProcessEnv)).toBeNull();
    expect(
      await appWorkerServiceFromEnv({ ROTOR_APP_WORKERS: "1", GLYPHH_CONTROL_URL: "http://cp" } as NodeJS.ProcessEnv),
    ).toBeNull();
  });

  it("builds a live service (memory cron) from complete coordinates", async () => {
    const svc = await appWorkerServiceFromEnv({
      ROTOR_APP_WORKERS: "1",
      GLYPHH_CONTROL_URL: base,
      GLYPHH_RUNTIME_TOKEN: RUNTIME_TOKEN,
      ROTOR_APP_CACHE_DIR: mkdtempSync(join(tmpdir(), "app-worker-env-")),
    } as NodeJS.ProcessEnv);
    expect(svc).not.toBeNull();
    try {
      expect(svc!.status()).toMatchObject({ ready: true, tier: "basic" });
      expect(await svc!.invoke(ORG, SLUG, "echo", { via: "env" })).toEqual({ echoed: { via: "env" } });
    } finally {
      await svc!.close();
    }
  });
});

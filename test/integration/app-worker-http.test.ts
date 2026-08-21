/**
 * app-worker-http.test.ts — the pod's app-worker HTTP surface (slice 5a),
 * driven over the wire the server.test.ts way: a real `startServer` on an
 * ephemeral port, plus a MOCK control plane serving introspection, worker-token
 * mint, and bundle download.
 *
 * What the assertions guard:
 *   - the surface is GATED: a pod without the service (every existing
 *     deployment) 404s POST /app-worker/invoke exactly like any unknown route;
 *   - /readyz advertises the `app-worker` capability when the mode is on —
 *     the same per-seam readiness line every other capability reports;
 *   - the route sits behind the SAME introspection gate as /run (no bearer →
 *     401), and the ORG comes from the INTROSPECTED principal — a body `orgId`
 *     cannot override it;
 *   - an auth-off pod (dev/self-host) may pass `orgId` in the body, and a
 *     mismatched one is refused;
 *   - the typed error contract over HTTP: 400 missing-slug/handler,
 *     404 not-installed, 403 capability-denied, 200 with the handler result.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { createServer } from "node:http";
import { createHash } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { startServer } from "../../src/server.js";
import { introspectorFromEnv } from "../../src/auth/introspect.js";
import { AppWorkerService } from "../../src/app-worker/service.js";
import { MemoryCronStore } from "../../src/app-worker/cron.js";
import { buildAppBundle } from "../harness/app-fixtures.js";

const RUNTIME_TOKEN = "gy_rk_pod_credential";
const WORKER_TOKEN = "gy_wk_minted_for_demo";
const CALLER_BEARER = "gy_rk_caller_token";
const SLUG = "demo";
const ORG = "org-1";

const BUNDLE = buildAppBundle(
  { slug: SLUG, worker: "worker.js", capabilities: ["data.query"] },
  {
    "worker.js": `
glyphh.handle("echo", (args) => ({ echoed: args }));
glyphh.handle("denied", () => glyphh.call("vector.upsert", { items: [] }));
`,
  },
);
const BUNDLE_SHA = createHash("sha256").update(BUNDLE).digest("hex");

// ── the mock control plane: introspect + mint + bundle rails ────────────────
let controlPlane: Server;
let cpBase = "";

// ── the pod under test (auth ON, app workers ON) ────────────────────────────
let pod: Server;
let podBase = "";
let service: AppWorkerService;

// ── a second pod with auth OFF (the dev/self-host shape) ────────────────────
let openPod: Server;
let openBase = "";
let openService: AppWorkerService;

// ── a default pod: no app-worker service (every existing deployment) ────────
let plainPod: Server;
let plainBase = "";

beforeAll(async () => {
  controlPlane = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const path = (req.url ?? "").split("?", 1)[0];
      const bearer = req.headers.authorization?.replace(/^Bearer /, "");
      const json = (status: number, payload: unknown) =>
        res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(payload));

      if (path === "/introspect") {
        const { token } = JSON.parse(Buffer.concat(chunks).toString("utf8")) as { token?: string };
        if (token === CALLER_BEARER) {
          json(200, { data: { active: true, sessionId: "sess-1", orgId: ORG, userId: "user-1" } });
        } else {
          json(200, { data: { active: false } });
        }
        return;
      }
      if (/^\/api\/apps\/[^/]+\/worker-token$/.test(path) && req.method === "POST") {
        if (bearer !== RUNTIME_TOKEN) { json(401, { error: { code: "E_UNAUTHENTICATED", message: "no" } }); return; }
        const slug = path.split("/")[3]!;
        if (slug === "ghost") {
          json(403, { error: { code: "E_FORBIDDEN", message: "'ghost' is not installed in your org" } });
          return;
        }
        json(201, {
          data: {
            token: WORKER_TOKEN, appSlug: slug, orgId: ORG,
            prefix: WORKER_TOKEN.slice(0, 10), createdAt: new Date().toISOString(), rotatedAt: null,
          },
        });
        return;
      }
      if (path === `/api/apps/${SLUG}/bundle/download-url`) {
        json(200, { data: { url: null, releaseId: "rel-1", version: "1.0.0", sha256: BUNDLE_SHA, sizeBytes: BUNDLE.length } });
        return;
      }
      if (path === `/api/apps/${SLUG}/source`) {
        res.writeHead(200, { "content-type": "application/zip" }).end(BUNDLE);
        return;
      }
      json(404, { error: { code: "E_NOT_FOUND", message: `no route ${path}` } });
    });
  });
  await new Promise<void>((r) => controlPlane.listen(0, "127.0.0.1", r));
  cpBase = `http://127.0.0.1:${(controlPlane.address() as AddressInfo).port}`;

  const makeService = () =>
    new AppWorkerService({
      controlPlaneUrl: cpBase,
      runtimeToken: RUNTIME_TOKEN,
      cacheDir: mkdtempSync(join(tmpdir(), "app-worker-http-")),
      cronStore: new MemoryCronStore(),
    });

  // Shared pod (no ROTOR_SESSION_ID): any ACTIVE token is admitted, and the
  // principal rides the decision — exactly the introspection shape /run uses.
  const auth = introspectorFromEnv({
    ROTOR_AUTH_INTROSPECT_URL: `${cpBase}/introspect`,
    ROTOR_AUTH_SERVICE_TOKEN: "svc",
  } as NodeJS.ProcessEnv);
  service = makeService();
  pod = startServer(0, undefined, undefined, undefined, auth, service);
  await new Promise<void>((r) => pod.once("listening", () => r()));
  podBase = `http://127.0.0.1:${(pod.address() as AddressInfo).port}`;

  openService = makeService();
  openPod = startServer(0, undefined, undefined, undefined, undefined, openService);
  await new Promise<void>((r) => openPod.once("listening", () => r()));
  openBase = `http://127.0.0.1:${(openPod.address() as AddressInfo).port}`;

  plainPod = startServer(0);
  await new Promise<void>((r) => plainPod.once("listening", () => r()));
  plainBase = `http://127.0.0.1:${(plainPod.address() as AddressInfo).port}`;
});

afterAll(async () => {
  await service.close();
  await openService.close();
  for (const s of [pod, openPod, plainPod, controlPlane]) {
    await new Promise<void>((r) => s.close(() => r()));
  }
});

const invoke = (base: string, body: unknown, bearer?: string) =>
  fetch(`${base}/app-worker/invoke`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
    },
    body: JSON.stringify(body),
  });

describe("the gate", () => {
  it("404s on a pod without the service — existing deployments untouched", async () => {
    const res = await invoke(plainBase, { slug: SLUG, handler: "echo" });
    expect(res.status).toBe(404);
    expect(((await res.json()) as { error: string }).error).toBe("not-found");
  });

  it("/readyz advertises the app-worker capability when the mode is on", async () => {
    const res = await fetch(`${openBase}/readyz`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as {
      status: string;
      capabilities: Record<string, { ready: boolean; tier: string }>;
    };
    expect(body.status).toBe("ready");
    expect(body.capabilities["app-worker"]).toEqual({ ready: true, tier: "basic" });
    // …and a default pod does NOT advertise it.
    const plain = (await (await fetch(`${plainBase}/readyz`)).json()) as {
      capabilities: Record<string, unknown>;
    };
    expect(plain.capabilities["app-worker"]).toBeUndefined();
  });
});

describe("auth (the same introspection gate as /run)", () => {
  it("401s with no bearer", async () => {
    const res = await invoke(podBase, { slug: SLUG, handler: "echo" });
    expect(res.status).toBe(401);
  });

  it("invokes with a live bearer; the org comes from the introspected principal", async () => {
    const res = await invoke(podBase, { slug: SLUG, handler: "echo", args: { a: 1 } }, CALLER_BEARER);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, slug: SLUG, handler: "echo", result: { echoed: { a: 1 } } });
  });

  it("a body orgId cannot override the principal's org", async () => {
    // The principal is org-1 (the minted token's org) — a hostile body claiming
    // another org changes nothing, because the principal wins.
    const res = await invoke(podBase, { slug: SLUG, handler: "echo", args: {}, orgId: "org-EVIL" }, CALLER_BEARER);
    expect(res.status).toBe(200);
  });

  it("an auth-off pod honours a body orgId — and refuses a mismatched one", async () => {
    const good = await invoke(openBase, { slug: SLUG, handler: "echo", args: { open: true } });
    expect(good.status).toBe(200);
    expect(((await good.json()) as { result: unknown }).result).toEqual({ echoed: { open: true } });

    const bad = await invoke(openBase, { slug: SLUG, handler: "echo", orgId: "org-OTHER" });
    expect(bad.status).toBe(403);
    expect(((await bad.json()) as { error: string }).error).toBe("forbidden");
  });
});

describe("the typed error contract over HTTP", () => {
  it("400s a missing slug or handler", async () => {
    const noSlug = await invoke(podBase, { handler: "echo" }, CALLER_BEARER);
    expect(noSlug.status).toBe(400);
    expect(((await noSlug.json()) as { error: string }).error).toBe("missing-slug");

    const noHandler = await invoke(podBase, { slug: SLUG }, CALLER_BEARER);
    expect(noHandler.status).toBe(400);
    expect(((await noHandler.json()) as { error: string }).error).toBe("missing-handler");
  });

  it("surfaces not-installed as 404", async () => {
    const res = await invoke(podBase, { slug: "ghost", handler: "echo" }, CALLER_BEARER);
    expect(res.status).toBe(404);
    const body = (await res.json()) as { error: string; detail: string };
    expect(body.error).toBe("not-installed");
    expect(body.detail).toMatch(/not installed/);
  });

  it("surfaces the bridge's grant wall as 403 capability-denied", async () => {
    const res = await invoke(podBase, { slug: SLUG, handler: "denied" }, CALLER_BEARER);
    expect(res.status).toBe(403);
    const body = (await res.json()) as { error: string; detail: string };
    expect(body.error).toBe("capability-denied");
    expect(body.detail).toMatch(/capability not granted: vector\.upsert/);
  });

  it("surfaces an unknown handler as 404 no-handler", async () => {
    const res = await invoke(podBase, { slug: SLUG, handler: "nope" }, CALLER_BEARER);
    expect(res.status).toBe(404);
    expect(((await res.json()) as { error: string }).error).toBe("no-handler");
  });
});

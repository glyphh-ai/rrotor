/**
 * app-bundle.test.ts — the workpanel-app bundle rail: fetch, verify,
 * materialize, cache.
 *
 * The contract under test is docs/app-runtime-spec.md "Distribution and
 * execution home": bundle bytes move by presigned URL (with the buffered
 * /source fallback in dev), are verified against the control plane's sha256,
 * and land in a content-hash-keyed cache so an unchanged release never
 * re-downloads. Manifest validation must mirror the desktop host — the same
 * bundle must pass or fail identically on both.
 */

import { describe, it, expect, beforeAll, afterAll, beforeEach } from "vitest";
import * as http from "node:http";
import { existsSync, mkdtempSync, readFileSync, readdirSync } from "node:fs";
import { createHash } from "node:crypto";
import { writeFile, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  readAppBundleManifest,
  fetchBundle,
  materialize,
  AppBundleCache,
  MAX_BUNDLE_BYTES,
} from "../../src/app-worker/bundle.js";
import { readZipEntries } from "../../src/app-worker/zip.js";
import { buildZip, buildAppBundle, appManifest } from "../harness/app-fixtures.js";

const sha256 = (b: Buffer) => createHash("sha256").update(b).digest("hex");

// ── manifest validation (desktop-mirrored rules) ───────────────────────────

describe("readAppBundleManifest", () => {
  it("accepts a minimal valid manifest and normalizes fields", () => {
    const m = readAppBundleManifest(JSON.stringify(appManifest({ worker: " worker.js " })));
    expect(m).not.toBeNull();
    expect(m!.slug).toBe("fixture-app");
    expect(m!.worker).toBe("worker.js");
    expect(m!.capabilities).toEqual([]);
    expect(m!.tools).toEqual([]);
  });

  it("rejects non-JSON, wrong kind, and missing name/slug/entry", () => {
    expect(readAppBundleManifest("not json")).toBeNull();
    expect(readAppBundleManifest(JSON.stringify({ ...appManifest(), kind: "other" }))).toBeNull();
    expect(readAppBundleManifest(JSON.stringify({ ...appManifest(), name: "" }))).toBeNull();
    expect(readAppBundleManifest(JSON.stringify({ ...appManifest(), slug: "" }))).toBeNull();
    expect(readAppBundleManifest(JSON.stringify({ ...appManifest(), entry: "" }))).toBeNull();
  });

  it("sanitizes the slug and refuses the reserved underscore prefix", () => {
    const m = readAppBundleManifest(JSON.stringify(appManifest({ slug: "My App!" })));
    expect(m!.slug).toBe("my-app-");
    expect(readAppBundleManifest(JSON.stringify(appManifest({ slug: "_libs" })))).toBeNull();
  });

  it("keeps only tools with exactly one backing (capability XOR handler)", () => {
    const m = readAppBundleManifest(
      JSON.stringify(
        appManifest({
          worker: "worker.js",
          tools: [
            { name: "ok-cap", capability: "db.exec" },
            { name: "ok-handler", handler: "sync" },
            { name: "both", capability: "db.exec", handler: "sync" },
            { name: "neither" },
          ],
        })
      )
    );
    expect(m!.tools!.map((t) => t.name)).toEqual(["ok-cap", "ok-handler"]);
  });

  it("refuses a handler-backed tool when no worker ships", () => {
    const json = JSON.stringify(appManifest({ tools: [{ name: "t", handler: "h" }] }));
    expect(readAppBundleManifest(json)).toBeNull();
  });

  it("dedupes and lowercases connectors, dropping malformed slugs", () => {
    const m = readAppBundleManifest(
      JSON.stringify(appManifest({ connectors: ["Salesforce", "salesforce", "bad slug!", "netsuite"] }))
    );
    expect(m!.connectors).toEqual(["salesforce", "netsuite"]);
  });
});

// ── zip reader ─────────────────────────────────────────────────────────────

describe("readZipEntries", () => {
  it("round-trips names and bytes through a stored archive", () => {
    const zip = buildZip({ "a.txt": "alpha", "nested/b.txt": "beta" });
    const entries = readZipEntries(zip);
    expect(entries.map((e) => e.name)).toEqual(["a.txt", "nested/b.txt"]);
    expect(entries[1].data().toString()).toBe("beta");
  });

  it("refuses a buffer that is not a zip", () => {
    expect(() => readZipEntries(Buffer.from("definitely not a zip archive at all"))).toThrow(/not a zip/);
  });
});

// ── materialize ────────────────────────────────────────────────────────────

describe("materialize", () => {
  let workDir: string;
  beforeEach(() => {
    workDir = mkdtempSync(join(tmpdir(), "app-mat-"));
  });

  async function writeBundle(zip: Buffer): Promise<string> {
    const p = join(workDir, "bundle.glyphh");
    await writeFile(p, zip);
    return p;
  }

  it("extracts a good bundle and returns its manifest", async () => {
    const zip = buildAppBundle({ worker: "worker.js" }, { "assets/app.css": "body{}" });
    const dest = join(workDir, "out");
    const manifest = await materialize(await writeBundle(zip), dest);
    expect(manifest.slug).toBe("fixture-app");
    expect(existsSync(join(dest, "index.html"))).toBe(true);
    expect(existsSync(join(dest, "worker.js"))).toBe(true);
    expect(readFileSync(join(dest, "assets/app.css"), "utf8")).toBe("body{}");
  });

  it("rejects path traversal without writing anything", async () => {
    const zip = buildAppBundle({}, { "../evil.txt": "escaped" });
    const dest = join(workDir, "out");
    await expect(materialize(await writeBundle(zip), dest)).rejects.toThrow(/unsafe path/);
    expect(existsSync(join(workDir, "evil.txt"))).toBe(false);
    expect(existsSync(dest)).toBe(false); // best-effort cleanup on refusal
  });

  it("rejects absolute and backslash entry names", async () => {
    for (const name of ["/etc/passwd", "a\\b.txt", "c:/win.txt"]) {
      const dest = join(workDir, `out-${name.length}`);
      const zip = buildAppBundle({}, { [name]: "x" });
      await expect(materialize(await writeBundle(zip), dest)).rejects.toThrow(/unsafe path/);
    }
  });

  it("rejects a bundle without manifest.json", async () => {
    const zip = buildZip({ "index.html": "<html>" });
    await expect(materialize(await writeBundle(zip), join(workDir, "out"))).rejects.toThrow(/no manifest.json/);
  });

  it("rejects an invalid manifest", async () => {
    const zip = buildZip({ "manifest.json": JSON.stringify({ kind: "nope" }), "index.html": "x" });
    await expect(materialize(await writeBundle(zip), join(workDir, "out"))).rejects.toThrow(/not a valid glyphh-app/);
  });

  it("rejects a bundle whose declared entry or worker is missing", async () => {
    const noEntry = buildZip({ "manifest.json": JSON.stringify(appManifest()) });
    await expect(materialize(await writeBundle(noEntry), join(workDir, "o1"))).rejects.toThrow(/entry not found/);
    const noWorker = buildZip({
      "manifest.json": JSON.stringify(appManifest({ worker: "worker.js" })),
      "index.html": "x",
    });
    await expect(materialize(await writeBundle(noWorker), join(workDir, "o2"))).rejects.toThrow(/worker not found/);
  });
});

// ── fetchBundle + AppBundleCache against a stub control plane ──────────────

describe("fetchBundle / AppBundleCache", () => {
  let server: http.Server;
  let base: string;
  const zip = buildAppBundle({ worker: "worker.js" });
  const zipSha = sha256(zip);

  // Mutable per-test knobs for the stub control plane.
  let presign: boolean;
  let reportedSha: string | null;
  let counts: { mint: number; presigned: number; source: number };

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      const url = req.url ?? "";
      if (url.startsWith("/api/apps/demo/bundle/download-url")) {
        if (req.headers.authorization !== "Bearer tok") {
          res.writeHead(401).end(JSON.stringify({ error: "no auth" }));
          return;
        }
        counts.mint++;
        res.writeHead(200, { "content-type": "application/json" });
        res.end(
          JSON.stringify({
            data: {
              url: presign ? `${base}/presigned/bundle.glyphh` : null,
              releaseId: "rel-1",
              version: "1.0.0",
              sha256: reportedSha,
              sizeBytes: zip.length,
            },
          })
        );
      } else if (url.startsWith("/presigned/")) {
        counts.presigned++;
        res.writeHead(200, { "content-type": "application/zip" }).end(zip);
      } else if (url.startsWith("/api/apps/demo/source")) {
        if (req.headers.authorization !== "Bearer tok") {
          res.writeHead(401).end();
          return;
        }
        counts.source++;
        res.writeHead(200, { "content-type": "application/zip" }).end(zip);
      } else {
        res.writeHead(404).end(JSON.stringify({ error: "not found" }));
      }
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    const addr = server.address() as { port: number };
    base = `http://127.0.0.1:${addr.port}`;
  });

  afterAll(async () => {
    await new Promise<void>((r) => server.close(() => r()));
  });

  beforeEach(() => {
    presign = true;
    reportedSha = zipSha;
    counts = { mint: 0, presigned: 0, source: 0 };
  });

  it("downloads via the presigned URL and verifies the digest", async () => {
    const tmp = await mkdtemp(join(tmpdir(), "app-fetch-"));
    const got = await fetchBundle(base, "tok", "demo", { tmpDir: tmp });
    expect(got.sha256).toBe(zipSha);
    expect(got.sizeBytes).toBe(zip.length);
    expect(got.version).toBe("1.0.0");
    expect(got.releaseId).toBe("rel-1");
    expect(readFileSync(got.zipPath).equals(zip)).toBe(true);
    expect(counts).toMatchObject({ presigned: 1, source: 0 });
  });

  it("falls back to the buffered /source route when url is null", async () => {
    presign = false;
    const tmp = await mkdtemp(join(tmpdir(), "app-fetch-"));
    const got = await fetchBundle(base, "tok", "demo", { tmpDir: tmp });
    expect(got.sha256).toBe(zipSha);
    expect(counts).toMatchObject({ presigned: 0, source: 1 });
  });

  it("deletes the file and throws on a sha256 mismatch", async () => {
    reportedSha = "0".repeat(64);
    const tmp = await mkdtemp(join(tmpdir(), "app-fetch-"));
    await expect(fetchBundle(base, "tok", "demo", { tmpDir: tmp })).rejects.toThrow(/sha256 verification/);
    expect(readdirSync(tmp)).toEqual([]); // nothing left behind
  });

  it("rejects a reported size over the cap before downloading a byte", async () => {
    await expect(fetchBundle(base, "tok", "demo", { maxBytes: 10 })).rejects.toThrow(/cap/);
    expect(counts.presigned).toBe(0);
  });

  it("rejects a stream that overruns the cap mid-download", async () => {
    // When the control plane reports no size, the early check cannot fire —
    // the stream guard must still stop an overrun mid-download.
    reportedSha = null;
    const tmp = await mkdtemp(join(tmpdir(), "app-fetch-"));
    const patched: typeof fetch = async (input, init) => {
      const res = await fetch(input, init);
      if (String(input).includes("download-url")) {
        const body = (await res.json()) as { data: Record<string, unknown> };
        body.data.sizeBytes = null;
        return new Response(JSON.stringify(body), { status: 200 });
      }
      return res;
    };
    await expect(
      fetchBundle(base, "tok", "demo", { tmpDir: tmp, maxBytes: 16, fetchImpl: patched })
    ).rejects.toThrow(/cap/);
  });

  it("surfaces control-plane refusals with status", async () => {
    await expect(fetchBundle(base, "wrong-token", "demo")).rejects.toThrow(/HTTP 401/);
    await expect(fetchBundle(base, "tok", "missing")).rejects.toThrow(/HTTP 404/);
  });

  it("caches by content hash: a re-fetch with the same hash is a no-op", async () => {
    const cacheDir = await mkdtemp(join(tmpdir(), "app-cache-"));
    const cache = new AppBundleCache({ cacheDir, controlPlaneUrl: base, token: "tok" });

    const first = await cache.resolveApp("demo");
    expect(first.sha256).toBe(zipSha);
    expect(first.manifest.slug).toBe("fixture-app");
    expect(existsSync(join(first.dir, "index.html"))).toBe(true);
    expect(counts.presigned).toBe(1);

    const second = await cache.resolveApp("demo");
    expect(second.dir).toBe(first.dir);
    expect(counts.presigned).toBe(1); // bundle bytes moved exactly once
    expect(counts.mint).toBe(2); // the cheap control-plane check still ran

    // A fresh cache instance over the same dir also reuses the extraction.
    const rehydrated = new AppBundleCache({ cacheDir, controlPlaneUrl: base, token: "tok" });
    const third = await rehydrated.resolveApp("demo");
    expect(third.dir).toBe(first.dir);
    expect(counts.presigned).toBe(1);
  });

  it("caches under the computed hash when the control plane reports none", async () => {
    reportedSha = null;
    const cacheDir = await mkdtemp(join(tmpdir(), "app-cache-"));
    const cache = new AppBundleCache({ cacheDir, controlPlaneUrl: base, token: "tok" });
    const first = await cache.resolveApp("demo");
    expect(first.sha256).toBe(zipSha);
    // No reported digest → the mint can't prove freshness, but the second
    // resolve still reuses the extraction keyed by the computed hash.
    const second = await cache.resolveApp("demo");
    expect(second.dir).toBe(first.dir);
  });

  it("collapses concurrent resolves into one download", async () => {
    const cacheDir = await mkdtemp(join(tmpdir(), "app-cache-"));
    const cache = new AppBundleCache({ cacheDir, controlPlaneUrl: base, token: "tok" });
    const [a, b] = await Promise.all([cache.resolveApp("demo"), cache.resolveApp("demo")]);
    expect(a.dir).toBe(b.dir);
    expect(counts.presigned).toBe(1);
  });

  it("exports the spec's 250MB cap", () => {
    expect(MAX_BUNDLE_BYTES).toBe(250 * 1024 * 1024);
  });
});

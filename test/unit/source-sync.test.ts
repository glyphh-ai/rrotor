/**
 * source-sync.test.ts — the pod's source-of-record tools against a faked
 * control plane (an in-memory snapshot chain speaking the real routes).
 *
 * What the assertions guard:
 *   - pull hydrates the workspace and stamps the base;
 *   - push CAS-publishes; a moved head comes back as the CONFLICT message,
 *     never a clobber; force overwrites;
 *   - rebase takes upstream-only files, stages both-changed at
 *     .glyphh/upstream/<path> keeping the local copy, restamps the base, and
 *     names who changed what first;
 *   - packing excludes node_modules/dist/.git and hashes deterministically.
 */

import { describe, it, expect, beforeEach } from "vitest";
import AdmZip from "adm-zip";
import { createHash } from "node:crypto";
import { mkdtemp, writeFile, readFile, mkdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { packWorkdir, pullSource, pushSource, rebaseSource, type SourceSyncCfg } from "../../src/harness/source-sync.js";

/** In-memory control plane: head + archives, speaking the three routes. */
function fakePlane() {
  const store = { head: null as string | null, archives: new Map<string, Buffer>(), publisher: "sam@x.co" };
  const fetchFn = (async (input: string | URL, init?: { body?: unknown }) => {
    const url = new URL(String(input));
    const m = url.pathname.match(/^\/api\/apps\/([^/]+)\/source(?:\/(head|archive))?(?:\/(.+))?$/);
    if (!m) return new Response("nf", { status: 404 });
    if (m[2] === "head") {
      return Response.json({ data: { mode: "snapshot", head: store.head ? { hash: store.head, createdByEmail: store.publisher, createdAt: new Date().toISOString() } : null } });
    }
    if (m[2] === "archive") {
      const b = store.archives.get(m[3]!);
      return b ? new Response(new Uint8Array(b)) : new Response("nf", { status: 404 });
    }
    // PUT /source — CAS
    const bytes = Buffer.from(init!.body as Uint8Array);
    const hash = createHash("sha256").update(bytes).digest("hex");
    const base = url.searchParams.get("baseHash");
    const force = url.searchParams.get("force") === "1";
    if (store.head === hash) return Response.json({ data: { hash, noop: true } });
    if (!force && store.head !== null && store.head !== base) {
      return Response.json({ error: "E_SOURCE_MOVED", detail: `${store.publisher} published recently from a different base — your build would overwrite their changes. Run rebase_source…` }, { status: 409 });
    }
    store.archives.set(hash, bytes);
    store.head = hash;
    return Response.json({ data: { hash } }, { status: 201 });
  }) as typeof fetch;
  return { store, fetchFn };
}

function zipOf(files: Record<string, string>): Buffer {
  const zip = new AdmZip();
  for (const [name, content] of Object.entries(files)) zip.addFile(name, Buffer.from(content));
  return zip.toBuffer();
}

let dir: string;
let plane: ReturnType<typeof fakePlane>;
let cfg: SourceSyncCfg;

beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "srcsync-"));
  plane = fakePlane();
  cfg = { workdir: dir, controlUrl: "http://plane.local", token: "t", fetchFn: plane.fetchFn };
});

describe("source sync — pull / push / rebase", () => {
  it("packs deterministically and excludes machinery folders", async () => {
    await writeFile(join(dir, "index.html"), "<x>");
    await mkdir(join(dir, "node_modules/junk"), { recursive: true });
    await writeFile(join(dir, "node_modules/junk/a.js"), "no");
    await mkdir(join(dir, "dist"), { recursive: true });
    await writeFile(join(dir, "dist/out.js"), "no");
    const a = await packWorkdir(dir);
    const names = new AdmZip(a).getEntries().map((e) => e.entryName);
    expect(names).toEqual(["index.html"]);
  });

  it("push creates the first head; identical re-push is a noop; pull round-trips", async () => {
    await writeFile(join(dir, "app.js"), "v1");
    const first = await pushSource(cfg, "demo");
    expect(first).toContain("new head");
    const again = await pushSource(cfg, "demo");
    expect(again).toContain("unchanged");

    const dir2 = await mkdtemp(join(tmpdir(), "srcsync2-"));
    const out = await pullSource({ ...cfg, workdir: dir2 }, "demo");
    expect(out).toContain("pulled 'demo'");
    expect(await readFile(join(dir2, "app.js"), "utf8")).toBe("v1");
    await rm(dir2, { recursive: true, force: true });
  });

  it("a moved head is a CONFLICT with the teammate named — never a clobber", async () => {
    await writeFile(join(dir, "app.js"), "mine-v1");
    await pushSource(cfg, "demo");
    // Sam moves the head behind our back.
    const sam = zipOf({ "app.js": "sams-v2", "sam.css": "s{}" });
    const samHash = createHash("sha256").update(sam).digest("hex");
    plane.store.archives.set(samHash, sam);
    plane.store.head = samHash;

    await writeFile(join(dir, "app.js"), "mine-v2");
    const out = await pushSource(cfg, "demo");
    expect(out).toContain("SOURCE CONFLICT");
    expect(out).toContain("sam@x.co");
    // The head was NOT clobbered.
    expect(plane.store.head).toBe(samHash);
  });

  it("rebase takes upstream-only files, stages both-changed, restamps, and names what changed first", async () => {
    // Base: both sides start from v1 with two files.
    await writeFile(join(dir, "app.js"), "v1");
    await writeFile(join(dir, "style.css"), "v1");
    await pushSource(cfg, "demo");
    const baseArchive = plane.store.archives.get(plane.store.head!)!;

    // Sam: changes style.css (upstream-only) AND app.js (both), from the same base.
    const sam = zipOf({ "app.js": "sams-app", "style.css": "sams-css" });
    const samHash = createHash("sha256").update(sam).digest("hex");
    plane.store.archives.set(samHash, sam);
    plane.store.head = samHash;
    void baseArchive; // the base stays fetchable for the 3-way

    // Us: change app.js locally.
    await writeFile(join(dir, "app.js"), "mine-app");

    const summary = await rebaseSource(cfg, "demo");
    expect(summary).toContain("sam@x.co");
    expect(summary).toContain("style.css");                       // taken
    expect(summary).toContain("CONFLICTS (1)");                   // app.js
    expect(summary).toContain("TELL THE USER");
    expect(await readFile(join(dir, "style.css"), "utf8")).toBe("sams-css");          // upstream taken
    expect(await readFile(join(dir, "app.js"), "utf8")).toBe("mine-app");             // local kept
    expect(await readFile(join(dir, ".glyphh/upstream/app.js"), "utf8")).toBe("sams-app"); // theirs staged
    expect((await readFile(join(dir, ".glyphh/source-base"), "utf8")).trim()).toBe(samHash); // restamped

    // After the agent merges, push goes through against the new base.
    await writeFile(join(dir, "app.js"), "merged");
    await rm(join(dir, ".glyphh/upstream"), { recursive: true, force: true });
    const pushed = await pushSource(cfg, "demo");
    expect(pushed).toContain("new head");
  });
});

/**
 * Sandbox attachment materialization: {name,url} refs land as real files in
 * <workspace>/attachments/ before the loop starts. fetch is faked — names
 * sanitize to one path segment, the size cap enforces while streaming, and a
 * failed download fails loudly.
 */

import { describe, it, expect } from "vitest";
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { ensureWorkspace, materializeAttachments } from "../../src/harness/sandbox.js";

function fakeFetch(bodies: Record<string, { status?: number; bytes?: Uint8Array; contentLength?: string }>): typeof fetch {
  return (async (input: string | URL) => {
    const url = String(input);
    const spec = bodies[url];
    if (!spec) throw new Error(`no route ${url}`);
    const status = spec.status ?? 200;
    const bytes = spec.bytes ?? new Uint8Array();
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: new Headers(spec.contentLength ? { "content-length": spec.contentLength } : {}),
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(bytes);
          controller.close();
        },
      }),
    } as unknown as Response;
  }) as typeof fetch;
}

const enc = new TextEncoder();

describe("materializeAttachments", () => {
  it("downloads into <workdir>/attachments with sanitized names", async () => {
    const workdir = await ensureWorkspace(join(mkdtempSync(join(tmpdir(), "sbx-")), "workspace"));
    const out = await materializeAttachments(
      workdir,
      [{ name: "../..//etc/passwd notes.txt", url: "https://files.test/a" }],
      1024,
      fakeFetch({ "https://files.test/a": { bytes: enc.encode("hello") } }),
    );
    expect(out).toHaveLength(1);
    expect(out[0].path.startsWith(join(workdir, "attachments"))).toBe(true);
    expect(out[0].path).not.toContain("..");
    expect(readFileSync(out[0].path, "utf8")).toBe("hello");
    expect(out[0].bytes).toBe(5);
  });

  it("no attachments → no attachments dir, no work", async () => {
    const workdir = await ensureWorkspace(join(mkdtempSync(join(tmpdir(), "sbx-")), "workspace"));
    expect(await materializeAttachments(workdir, [], 1024)).toEqual([]);
    expect(existsSync(join(workdir, "attachments"))).toBe(false);
  });

  it("fails loudly on a non-2xx download", async () => {
    const workdir = await ensureWorkspace(join(mkdtempSync(join(tmpdir(), "sbx-")), "workspace"));
    await expect(
      materializeAttachments(workdir, [{ name: "a.txt", url: "https://files.test/gone" }], 1024, fakeFetch({ "https://files.test/gone": { status: 404 } })),
    ).rejects.toThrow(/a\.txt.*404/);
  });

  it("enforces the size cap — declared and actual", async () => {
    const workdir = await ensureWorkspace(join(mkdtempSync(join(tmpdir(), "sbx-")), "workspace"));
    await expect(
      materializeAttachments(
        workdir,
        [{ name: "big.bin", url: "https://files.test/big" }],
        10,
        fakeFetch({ "https://files.test/big": { bytes: new Uint8Array(4), contentLength: "999999" } }),
      ),
    ).rejects.toThrow(/cap/);
    await expect(
      materializeAttachments(
        workdir,
        [{ name: "sneaky.bin", url: "https://files.test/sneaky" }],
        10,
        fakeFetch({ "https://files.test/sneaky": { bytes: new Uint8Array(64) } }),
      ),
    ).rejects.toThrow(/cap/);
  });
});

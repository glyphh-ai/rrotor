/**
 * app-fixtures.ts — build tiny `.glyphh` bundles in-test.
 *
 * A `.glyphh` bundle is a plain ZIP; this writes one (STORED entries, no
 * compression — the reader handles both) so bundle/executor tests exercise the
 * real archive path without a binary fixture checked into the repo. Entry
 * names are written verbatim, which is exactly what the traversal tests need
 * (`../evil.txt` must reach the extractor to be refused there).
 */

import type { AppBundleManifest } from "../../src/app-worker/bundle.js";

// ── crc32 (the one zip field a well-formed writer must compute) ────────────
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(buf: Buffer): number {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** Write a ZIP archive (stored, no compression) from name → content. */
export function buildZip(files: Record<string, string | Buffer>): Buffer {
  const locals: Buffer[] = [];
  const centrals: Buffer[] = [];
  let offset = 0;
  for (const [name, content] of Object.entries(files)) {
    const nameBuf = Buffer.from(name, "utf8");
    const data = Buffer.isBuffer(content) ? content : Buffer.from(content, "utf8");
    const crc = crc32(data);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); // local file header signature
    local.writeUInt16LE(20, 4); // version needed
    local.writeUInt16LE(0, 6); // flags
    local.writeUInt16LE(0, 8); // method: stored
    local.writeUInt32LE(0, 10); // mtime/mdate
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18); // compressed size
    local.writeUInt32LE(data.length, 22); // uncompressed size
    local.writeUInt16LE(nameBuf.length, 26);
    local.writeUInt16LE(0, 28); // extra len
    locals.push(local, nameBuf, data);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0); // central directory signature
    central.writeUInt16LE(20, 4); // version made by
    central.writeUInt16LE(20, 6); // version needed
    central.writeUInt16LE(0, 8); // flags
    central.writeUInt16LE(0, 10); // method: stored
    central.writeUInt32LE(0, 12); // mtime/mdate
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBuf.length, 28);
    // extra/comment/disk/attrs stay zero
    central.writeUInt32LE(offset, 42); // local header offset
    centrals.push(central, nameBuf);

    offset += local.length + nameBuf.length + data.length;
  }

  const cdSize = centrals.reduce((n, b) => n + b.length, 0);
  const count = Object.keys(files).length;
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(count, 8);
  eocd.writeUInt16LE(count, 10);
  eocd.writeUInt32LE(cdSize, 12);
  eocd.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, ...centrals, eocd]);
}

/** A minimal valid glyphh-app manifest, overridable per test. */
export function appManifest(over: Partial<AppBundleManifest> = {}): AppBundleManifest {
  return {
    manifestVersion: 2,
    kind: "glyphh-app",
    name: "Fixture App",
    slug: "fixture-app",
    entry: "index.html",
    capabilities: [],
    ...over,
  };
}

/** A complete `.glyphh` bundle: manifest + panel html (+ worker), plus extras. */
export function buildAppBundle(
  manifestOver: Partial<AppBundleManifest> = {},
  extraFiles: Record<string, string | Buffer> = {}
): Buffer {
  const manifest = appManifest(manifestOver);
  const files: Record<string, string | Buffer> = {
    "manifest.json": JSON.stringify(manifest),
    "index.html": "<!doctype html><title>fixture</title>",
    ...extraFiles,
  };
  if (manifest.worker && !(manifest.worker in files)) {
    files[manifest.worker] = `glyphh.handle("echo", (args) => args);`;
  }
  return buildZip(files);
}

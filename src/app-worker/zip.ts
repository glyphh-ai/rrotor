/**
 * zip.ts — a minimal, dependency-free ZIP reader for `.glyphh` app bundles.
 *
 * A `.glyphh` bundle is a plain ZIP (manifest.json + index.html + optional
 * worker.js) produced by the control plane's build path. The pod only needs to
 * READ these archives, and the runtime is deliberately dependency-light
 * (src/server.ts), so this parses the two structures a reader needs — the
 * end-of-central-directory record and the central directory — and inflates
 * entry data with `node:zlib`. Zip64 archives are refused: the bundle rail
 * hard-caps at 250MB (docs/app-runtime-spec.md, "Distribution and execution
 * home"), far below the 4GB where zip64 starts.
 *
 * Entry NAMES are surfaced verbatim; path-safety (traversal, absolute paths)
 * is the extractor's job — see `materialize` in bundle.ts — because "is this
 * name safe" depends on where it is being written.
 */

import { inflateRawSync } from "node:zlib";

const EOCD_SIG = 0x06054b50; // end of central directory
const CEN_SIG = 0x02014b50; // central directory file header
const LOC_SIG = 0x04034b50; // local file header
const EOCD_MIN = 22; // EOCD size with an empty comment
const ZIP64_MARKER = 0xffffffff;

/** One file (or directory marker) inside the archive. `data()` decompresses
 *  lazily so listing an archive for validation costs no inflation. */
export interface ZipEntry {
  name: string;
  isDirectory: boolean;
  /** Uncompressed size as declared by the central directory. */
  size: number;
  data(): Buffer;
}

/** Read a ZIP archive's entries from an in-memory buffer. Throws on anything
 *  that is not a well-formed, non-zip64 archive. */
export function readZipEntries(buf: Buffer): ZipEntry[] {
  // The EOCD sits at the very end, pushed forward only by an archive comment
  // (max 65535 bytes) — scan backwards for its signature.
  let eocd = -1;
  const floor = Math.max(0, buf.length - EOCD_MIN - 65535);
  for (let i = buf.length - EOCD_MIN; i >= floor; i--) {
    if (buf.readUInt32LE(i) === EOCD_SIG) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new Error("not a zip archive (no end-of-central-directory record)");

  const count = buf.readUInt16LE(eocd + 10);
  const cenOffset = buf.readUInt32LE(eocd + 16);
  if (count === 0xffff || cenOffset === ZIP64_MARKER) {
    throw new Error("zip64 archives are not supported (bundles are capped far below 4GB)");
  }
  if (cenOffset + 4 > buf.length) throw new Error("corrupt zip: central directory offset out of range");

  const entries: ZipEntry[] = [];
  let p = cenOffset;
  for (let i = 0; i < count; i++) {
    if (p + 46 > buf.length || buf.readUInt32LE(p) !== CEN_SIG) {
      throw new Error("corrupt zip: bad central directory entry");
    }
    const method = buf.readUInt16LE(p + 10);
    const compressedSize = buf.readUInt32LE(p + 20);
    const size = buf.readUInt32LE(p + 24);
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    const localOffset = buf.readUInt32LE(p + 42);
    if (size === ZIP64_MARKER || compressedSize === ZIP64_MARKER || localOffset === ZIP64_MARKER) {
      throw new Error("zip64 archives are not supported (bundles are capped far below 4GB)");
    }
    const name = buf.toString("utf8", p + 46, p + 46 + nameLen);
    entries.push({
      name,
      isDirectory: name.endsWith("/"),
      size,
      data: () => entryData(buf, name, localOffset, method, compressedSize),
    });
    p += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

/** Locate an entry's bytes via its LOCAL header (whose name/extra lengths can
 *  differ from the central directory's) and decompress. */
function entryData(buf: Buffer, name: string, localOffset: number, method: number, compressedSize: number): Buffer {
  if (localOffset + 30 > buf.length || buf.readUInt32LE(localOffset) !== LOC_SIG) {
    throw new Error(`corrupt zip: bad local header for "${name}"`);
  }
  const nameLen = buf.readUInt16LE(localOffset + 26);
  const extraLen = buf.readUInt16LE(localOffset + 28);
  const start = localOffset + 30 + nameLen + extraLen;
  const end = start + compressedSize;
  if (end > buf.length) throw new Error(`corrupt zip: data for "${name}" out of range`);
  const raw = buf.subarray(start, end);
  if (method === 0) return Buffer.from(raw); // stored
  if (method === 8) return inflateRawSync(raw); // deflate
  throw new Error(`unsupported zip compression method ${method} for "${name}"`);
}

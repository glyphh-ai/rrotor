/**
 * harness/sandbox.ts — the per-session sandbox INSIDE the pod.
 *
 * The pod container is the hard isolation boundary (one pod ⇔ one session,
 * provisioned by the control plane); the sandbox dir is the run's working
 * surface within it: `HARNESS_HOME/sessions/<session>/workspace`. All fs/bash
 * tool activity is anchored there via the SDK's `cwd`. Nothing here reaches a
 * user machine — there is no relay, no desktop bridge, no native dialog.
 *
 * Attachments arrive as `{name, url}` refs (server-signed URLs, treated as
 * opaque) and are materialized into `<workspace>/attachments/` BEFORE the
 * agent loop starts, so the model can read them as ordinary files. Names are
 * sanitized to a single path segment; each file is capped; a failed download
 * fails the run loudly rather than starting a run with silently missing
 * context.
 */

import { createWriteStream } from "node:fs";
import { mkdir, stat } from "node:fs/promises";
import { join } from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";

import { sanitizeSegment } from "./config.js";
import type { AttachmentRef } from "./config.js";
import { log } from "../obs/logger.js";

export interface MaterializedAttachment {
  name: string;
  /** Absolute path inside the sandbox. */
  path: string;
  bytes: number;
}

/** Ensure the sandbox workspace exists; returns its absolute path. */
export async function ensureWorkspace(workdir: string): Promise<string> {
  await mkdir(workdir, { recursive: true });
  return workdir;
}

/**
 * Download every attachment into `<workdir>/attachments/`. Sequential — run
 * start is not a throughput path, and sequencing keeps the size accounting
 * simple. Throws on the first failure (bad URL, over cap, transport error).
 */
export async function materializeAttachments(
  workdir: string,
  refs: AttachmentRef[],
  maxBytes: number,
  fetchFn: typeof fetch = fetch,
): Promise<MaterializedAttachment[]> {
  if (!refs.length) return [];
  const dir = join(workdir, "attachments");
  await mkdir(dir, { recursive: true });
  const out: MaterializedAttachment[] = [];
  for (const ref of refs) {
    const name = sanitizeSegment(ref.name);
    const path = join(dir, name);
    let res: Response;
    try {
      res = await fetchFn(ref.url);
    } catch (err) {
      throw new Error(`attachment "${ref.name}": download failed — ${(err as Error).message}`);
    }
    if (!res.ok || !res.body) {
      throw new Error(`attachment "${ref.name}": download failed (${res.status})`);
    }
    // Enforce the cap while streaming — a Content-Length header is advisory.
    const declared = Number(res.headers.get("content-length") ?? 0);
    if (declared > maxBytes) throw new Error(`attachment "${ref.name}": ${declared} bytes exceeds the ${maxBytes}-byte cap`);
    let seen = 0;
    const counter = async function* (src: AsyncIterable<Uint8Array>): AsyncIterable<Uint8Array> {
      for await (const chunk of src) {
        seen += chunk.byteLength;
        if (seen > maxBytes) throw new Error(`attachment "${ref.name}": exceeds the ${maxBytes}-byte cap`);
        yield chunk;
      }
    };
    try {
      await pipeline(counter(Readable.fromWeb(res.body as import("node:stream/web").ReadableStream)), createWriteStream(path));
    } catch (err) {
      throw new Error(`attachment "${ref.name}": ${(err as Error).message}`);
    }
    const info = await stat(path);
    out.push({ name, path, bytes: info.size });
    // The signed URL is opaque and may embed credentials — log the name only.
    log.info("attachment materialized", { name, bytes: info.size });
  }
  return out;
}

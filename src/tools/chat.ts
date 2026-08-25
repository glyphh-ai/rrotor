/**
 * `chat` tool pack — the lightweight tools the conversational surface needs:
 * `recall` (semantic memory over recorded turns) and `web.fetch`. Both are `reading`
 * effect (recorded, so replay is stable) and read-only grants, so they are safe in
 * chat mode where nothing mutates.
 */

import { RotorError } from "../errors.js";
import type { MemoryPlugin, Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

export interface ChatOptions {
  memory: MemoryPlugin;
  /** Max fetched body bytes (bounds tokens). Default 100_000. */
  maxFetch?: number;
}

export function chatPack(opts: ChatOptions): ToolPack {
  const maxFetch = opts.maxFetch ?? 100_000;

  const tools: ToolSpec[] = [
    {
      name: "web.fetch",
      version: 1,
      description: "Fetch a URL and return its text body (bounded).",
      effect: "reading",
      grants: ["net.read"],
      input: { type: "object", properties: { url: { type: "string" } }, required: ["url"] },
      output: { type: "object", properties: { url: { type: "string" }, status: { type: "number" }, body: { type: "string" } } },
      handler: async (args: Row) => {
        const url = String(args.url ?? "");
        if (!/^https?:\/\//.test(url)) throw new RotorError("E_MISSING_INPUT", "web.fetch requires an http(s) `url`");
        try {
          const res = await fetch(url);
          const text = await res.text();
          return { url, status: res.status, body: text.slice(0, maxFetch), truncated: text.length > maxFetch };
        } catch (e) {
          throw new RotorError("E_TRANSPORT", `web.fetch failed: ${(e as Error).message}`, { context: { url } });
        }
      },
    },
  ];

  return { name: "chat", version: "1.0.0", tools };
}

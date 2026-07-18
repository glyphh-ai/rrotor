/**
 * `web` tool pack — the network workbench: raw HTTP (get/request/download), web
 * search, HTML digestion (text/links/meta), URL algebra, RSS/Atom, and DNS.
 *
 * The design splits along the effect axis: `http.get`/`web.search`/`rss.read`/
 * `dns.resolve` are `reading` (recorded, replay-stable), `http.download` is
 * `mutating` (a reversible file under the workspace sandbox), and `http.request`
 * is `external` — an arbitrary verb is an outward act, so it sits behind
 * `net.write` and never appears in chat/co-work modes.
 *
 * Everything is BOUNDED: bodies stream up to `maxBytes` and report `truncated`,
 * redirects cap at 5, downloads cap at 50MB, parsers cap their result arrays.
 * The HTML/URL tools are `pure` (deterministic functions of their inputs, no
 * grants) — hand-rolled, defensive parsers that degrade to empty results rather
 * than throwing on the open web's malformed reality. Response header values
 * whose names look secret-bearing (auth, token, key…) come back `<redacted>`.
 */

import { open, mkdir, rm } from "node:fs/promises";
import { resolve, relative, sep, dirname } from "node:path";
import dns from "node:dns/promises";

import { RotorError } from "../errors.js";
import type { Row } from "../plugins/interfaces.js";
import type { ToolPack, ToolSpec } from "./spec.js";

export interface WebOptions {
  /** Workspace sandbox — `http.download` paths resolve under here; escapes refuse. */
  root: string;
  /** Wall-clock cap per network call (ms). Default 30_000. */
  timeoutMs?: number;
  /** Max response-body bytes returned (bounds tokens). Default 500_000. */
  maxBytes?: number;
}

const MAX_REDIRECTS = 5;
const DOWNLOAD_CAP = 50 * 1024 * 1024; // 50MB hard cap for http.download
const TEXT_CAP = 200_000;
const LINKS_CAP = 200;
const SEARCH_CAP = 10;
const RSS_ITEMS_CAP = 50;
const SECRET_NAME = /key|token|secret|password|credential|auth|cookie/i;

// ── workspace sandbox (same contract as fs.ts, kept local by design) ────────

function resolveInRoot(root: string, p: unknown): string {
  if (typeof p !== "string" || p === "") throw new RotorError("E_MISSING_INPUT", "http.download requires a string `path`");
  const base = resolve(root);
  const target = resolve(base, p);
  const rel = relative(base, target);
  if (rel === ".." || rel.startsWith(".." + sep)) {
    throw new RotorError("E_POLICY_DENIED", `path escapes the workspace: ${p}`, { context: { path: p } });
  }
  return target;
}

// ── HTTP plumbing ───────────────────────────────────────────────────────────

function assertHttpUrl(url: string, tool: string): URL {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    throw new RotorError("E_MISSING_INPUT", `${tool} requires a valid absolute \`url\``, { context: { url } });
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") {
    throw new RotorError("E_POLICY_DENIED", `${tool} only allows http(s) urls, got ${u.protocol}`, { context: { url } });
  }
  return u;
}

// ── SSRF guard: no requests to internal / loopback / metadata addresses ──────
// A URL-string check alone is not enough — a public hostname can resolve to an
// internal IP. So we classify the LITERAL host when it is an IP, and otherwise
// resolve it and classify every answer before the socket opens. Set
// RROTOR_ALLOW_PRIVATE_NET=1 to opt out (a trusted internal deployment).

function ipInBlockedRange(ip: string): boolean {
  const v4 = ip.startsWith("::ffff:") ? ip.slice(7) : ip; // IPv4-mapped IPv6
  const m = v4.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (m) {
    const [a, b] = m.slice(1).map(Number);
    if (a === 0 || a === 127 || a === 10) return true; // this-host, loopback, private
    if (a === 169 && b === 254) return true; // link-local incl. 169.254.169.254 metadata
    if (a === 172 && b >= 16 && b <= 31) return true; // private
    if (a === 192 && b === 168) return true; // private
    if (a === 100 && b >= 64 && b <= 127) return true; // CGNAT
    return false;
  }
  const lc = ip.toLowerCase();
  if (lc === "::" || lc === "::1") return true; // unspecified, loopback
  if (lc.startsWith("fe80") || lc.startsWith("fc") || lc.startsWith("fd")) return true; // link-local, ULA (incl. fd00:ec2::254)
  return false;
}

async function assertPublicHost(u: URL, tool: string): Promise<void> {
  if (process.env.RROTOR_ALLOW_PRIVATE_NET === "1") return;
  const host = u.hostname.replace(/^\[|\]$/g, ""); // strip IPv6 brackets
  const deny = (addr: string): never => {
    throw new RotorError("E_POLICY_DENIED", `${tool} refuses request to a private/loopback/link-local address (${addr}) — set RROTOR_ALLOW_PRIVATE_NET=1 to allow`, { context: { url: u.toString(), addr } });
  };
  if (/^[\d.]+$/.test(host) || host.includes(":")) {
    if (ipInBlockedRange(host)) deny(host);
    return;
  }
  let answers: Array<{ address: string }>;
  try {
    answers = await dns.lookup(host, { all: true });
  } catch (e) {
    throw new RotorError("E_TOOL", `${tool}: DNS lookup failed for ${host}: ${(e as Error).message}`, { context: { url: u.toString() }, cause: e });
  }
  for (const a of answers) if (ipInBlockedRange(a.address)) deny(a.address);
}

function cleanHeaders(raw: unknown): Record<string, string> | undefined {
  if (raw == null || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) out[k] = String(v);
  return out;
}

/** Response headers as a plain object; secret-looking names are redacted. */
function redactHeaders(h: Headers): Record<string, string> {
  const out: Record<string, string> = {};
  h.forEach((v, k) => {
    out[k] = SECRET_NAME.test(k) ? "<redacted>" : v.slice(0, 2000);
  });
  return out;
}

/** Follow redirects manually (≤5 hops) so the bound is explicit and Location
 *  resolution is under our control. Returns the final, unconsumed Response. */
async function followFetch(opts: { tool: string; method: string; url: string; headers?: Record<string, string>; body?: string; signal: AbortSignal }): Promise<Response> {
  let { method, url, body } = opts;
  for (let hop = 0; hop <= MAX_REDIRECTS; hop++) {
    const u = assertHttpUrl(url, opts.tool);
    await assertPublicHost(u, opts.tool); // re-checked every hop → redirect-based SSRF is blocked too
    const res = await fetch(url, { method, headers: opts.headers, body, redirect: "manual", signal: opts.signal });
    if ([301, 302, 303, 307, 308].includes(res.status)) {
      const loc = res.headers.get("location");
      if (loc) {
        void res.body?.cancel().catch(() => {});
        url = new URL(loc, url).toString();
        if (res.status !== 307 && res.status !== 308) {
          method = "GET"; // classic redirect semantics: re-issue as a body-less GET
          body = undefined;
        }
        continue;
      }
    }
    return res;
  }
  throw new RotorError("E_TOOL", `${opts.tool}: too many redirects (>${MAX_REDIRECTS})`, { context: { url: opts.url } });
}

/** Read a response body up to `maxBytes`, cancelling the stream when capped. */
async function readBounded(res: Response, maxBytes: number): Promise<{ body: string; bytes: number; truncated: boolean }> {
  const reader = res.body?.getReader();
  if (!reader) return { body: "", bytes: 0, truncated: false };
  const chunks: Buffer[] = [];
  let total = 0;
  let truncated = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    const chunk = Buffer.from(value);
    total += chunk.byteLength;
    if (total > maxBytes) {
      chunks.push(chunk.subarray(0, chunk.byteLength - (total - maxBytes)));
      truncated = true;
      await reader.cancel().catch(() => {});
      break;
    }
    chunks.push(chunk);
  }
  const buf = Buffer.concat(chunks);
  return { body: buf.toString("utf8"), bytes: buf.byteLength, truncated };
}

async function boundedRequest(opts: { tool: string; method: string; url: string; headers?: Record<string, string>; body?: string; timeoutMs: number; maxBytes: number }): Promise<Record<string, unknown>> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts.timeoutMs);
  try {
    const res = await followFetch({ ...opts, signal: ctrl.signal });
    const { body, bytes, truncated } = await readBounded(res, opts.maxBytes);
    return { status: res.status, headers: redactHeaders(res.headers), body, bytes, truncated };
  } catch (e) {
    if (e instanceof RotorError) throw e;
    const why = ctrl.signal.aborted ? `timed out after ${opts.timeoutMs}ms` : (e as Error).message;
    throw new RotorError("E_TOOL", `${opts.tool} failed: ${why}`, { context: { url: opts.url }, cause: e });
  } finally {
    clearTimeout(timer);
  }
}

// ── HTML digestion (pure, defensive, hand-rolled) ───────────────────────────

const NAMED_ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };

export function decodeEntities(s: string): string {
  return s.replace(/&(#x?[0-9a-f]+|[a-z]+);/gi, (m, ent: string) => {
    const lower = ent.toLowerCase();
    if (NAMED_ENTITIES[lower]) return NAMED_ENTITIES[lower];
    try {
      if (lower.startsWith("#x")) return String.fromCodePoint(parseInt(lower.slice(2), 16));
      if (lower.startsWith("#")) return String.fromCodePoint(parseInt(lower.slice(1), 10));
    } catch {
      return m;
    }
    return m;
  });
}

const stripTags = (s: string): string => s.replace(/<[^>]*>/g, " ");
// collapse runs of whitespace; drop the space a stripped inline tag leaves before punctuation
const collapseWs = (s: string): string =>
  s
    .replace(/\s+/g, " ")
    .replace(/ ([.,;:!?])/g, "$1")
    .trim();

/** script/style/comments out, tags to spaces, entities decoded, whitespace collapsed. */
export function htmlToText(html: string): string {
  const noBlocks = html
    .replace(/<script\b[\s\S]*?<\/script\s*>/gi, " ")
    .replace(/<style\b[\s\S]*?<\/style\s*>/gi, " ")
    .replace(/<!--[\s\S]*?-->/g, " ");
  return collapseWs(decodeEntities(stripTags(noBlocks)));
}

/** Extract one attribute value from an attribute string ("k=v" pairs, "-quoted). */
function attr(attrs: string, name: string): string | undefined {
  const m = new RegExp(`${name}\\s*=\\s*("([^"]*)"|'([^']*)')`, "i").exec(attrs);
  return m ? decodeEntities(m[2] ?? m[3] ?? "") : undefined;
}

/** DuckDuckGo html-endpoint result parser. Defensive by contract: layout drift
 *  yields fewer (or zero) results, never a throw. Exported for fixture tests. */
export function parseSearchResults(html: string, max: number): Array<{ title: string; url: string; snippet: string }> {
  const out: Array<{ title: string; url: string; snippet: string }> = [];
  const aRe = /<a\b([^>]*class="[^"]*\bresult__a\b[^"]*"[^>]*)>([\s\S]*?)<\/a>/gi;
  let m: RegExpExecArray | null;
  while ((m = aRe.exec(html)) && out.length < max) {
    try {
      const href = attr(m[1], "href");
      if (!href) continue;
      const url = normalizeResultHref(href);
      if (!url) continue;
      const title = collapseWs(decodeEntities(stripTags(m[2]))).slice(0, 300);
      // the snippet element sits shortly after its result anchor
      const tail = html.slice(aRe.lastIndex, aRe.lastIndex + 3000);
      const sm = /class="[^"]*\bresult__snippet\b[^"]*"[^>]*>([\s\S]*?)<\/(?:a|td|div|span)\s*>/i.exec(tail);
      const snippet = sm ? collapseWs(decodeEntities(stripTags(sm[1]))).slice(0, 500) : "";
      out.push({ title, url, snippet });
    } catch {
      continue; // one malformed block never sinks the rest
    }
  }
  return out;
}

/** DDG wraps result urls in a //duckduckgo.com/l/?uddg=… redirect — unwrap it. */
function normalizeResultHref(href: string): string {
  const abs = href.startsWith("//") ? "https:" + href : href;
  try {
    const u = new URL(abs);
    const uddg = u.searchParams.get("uddg");
    if (uddg && /^https?:\/\//.test(uddg)) return uddg;
    if (u.protocol === "http:" || u.protocol === "https:") return u.toString();
  } catch {
    return "";
  }
  return "";
}

// ── RSS/Atom (minimal, regex-level, bounded) ────────────────────────────────

function tagText(xml: string, tag: string): string {
  const m = new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}\\s*>`, "i").exec(xml);
  if (!m) return "";
  let t = m[1].trim();
  const cdata = /^<!\[CDATA\[([\s\S]*?)\]\]>$/.exec(t);
  if (cdata) t = cdata[1].trim();
  return collapseWs(decodeEntities(stripTags(t)));
}

function parseFeed(xml: string, maxItems: number): { title: string; items: Array<{ title: string; link: string; published: string; summary: string }>; count: number; truncated: boolean } {
  const isAtom = /<feed[\s>]/i.test(xml) && !/<rss[\s>]/i.test(xml);
  const items: Array<{ title: string; link: string; published: string; summary: string }> = [];
  let truncated = false;
  if (isAtom) {
    const eRe = /<entry(?:\s[^>]*)?>([\s\S]*?)<\/entry\s*>/gi;
    let m: RegExpExecArray | null;
    while ((m = eRe.exec(xml))) {
      if (items.length >= maxItems) {
        truncated = true;
        break;
      }
      const e = m[1];
      let link = "";
      const lRe = /<link\b([^>]*)\/?>/gi;
      let lm: RegExpExecArray | null;
      while ((lm = lRe.exec(e))) {
        const rel = attr(lm[1], "rel");
        const href = attr(lm[1], "href") ?? "";
        if (!rel || rel === "alternate") {
          link = href;
          break;
        }
        if (!link) link = href;
      }
      items.push({
        title: tagText(e, "title").slice(0, 300),
        link,
        published: tagText(e, "published") || tagText(e, "updated"),
        summary: (tagText(e, "summary") || tagText(e, "content")).slice(0, 1000),
      });
    }
    // feed title = the first <title> outside any entry; entries were consumed above
    const head = xml.split(/<entry[\s>]/i)[0];
    return { title: tagText(head, "title").slice(0, 300), items, count: items.length, truncated };
  }
  const iRe = /<item(?:\s[^>]*)?>([\s\S]*?)<\/item\s*>/gi;
  let m: RegExpExecArray | null;
  while ((m = iRe.exec(xml))) {
    if (items.length >= maxItems) {
      truncated = true;
      break;
    }
    const i = m[1];
    items.push({
      title: tagText(i, "title").slice(0, 300),
      link: tagText(i, "link"),
      published: tagText(i, "pubDate") || tagText(i, "dc:date"),
      summary: tagText(i, "description").slice(0, 1000),
    });
  }
  const head = xml.split(/<item[\s>]/i)[0];
  return { title: tagText(head, "title").slice(0, 300), items, count: items.length, truncated };
}

// ── the pack ────────────────────────────────────────────────────────────────

export function webPack(opts: WebOptions): ToolPack {
  const { root } = opts;
  const timeoutMs = opts.timeoutMs ?? 30_000;
  const maxBytes = opts.maxBytes ?? 500_000;

  const tools: ToolSpec[] = [
    {
      name: "http.get",
      version: 1,
      description: "HTTP GET a url. Returns status, headers (secrets redacted), and a bounded text body.",
      effect: "reading",
      grants: ["net.read"],
      input: { type: "object", properties: { url: { type: "string" }, headers: { type: "object" } }, required: ["url"] },
      output: {
        type: "object",
        properties: { status: { type: "number" }, headers: { type: "object" }, body: { type: "string" }, bytes: { type: "number" }, truncated: { type: "boolean" } },
      },
      handler: async (args: Row) => {
        const url = String(args.url ?? "");
        assertHttpUrl(url, "http.get");
        return boundedRequest({ tool: "http.get", method: "GET", url, headers: cleanHeaders(args.headers), timeoutMs, maxBytes });
      },
    },
    {
      name: "http.request",
      version: 1,
      description: "HTTP request with any method and optional body/json. Returns status, headers, bounded body.",
      effect: "external",
      grants: ["net.write"],
      input: {
        type: "object",
        properties: { method: { type: "string" }, url: { type: "string" }, headers: { type: "object" }, body: { type: "string" }, json: {} },
        required: ["method", "url"],
      },
      output: {
        type: "object",
        properties: { status: { type: "number" }, headers: { type: "object" }, body: { type: "string" }, bytes: { type: "number" }, truncated: { type: "boolean" } },
      },
      handler: async (args: Row) => {
        const method = String(args.method ?? "").trim().toUpperCase();
        const url = String(args.url ?? "");
        if (!method) throw new RotorError("E_MISSING_INPUT", "http.request requires a `method`");
        assertHttpUrl(url, "http.request");
        const headers = cleanHeaders(args.headers) ?? {};
        let body: string | undefined = typeof args.body === "string" ? args.body : undefined;
        if (args.json !== undefined) {
          body = JSON.stringify(args.json);
          if (!Object.keys(headers).some((k) => k.toLowerCase() === "content-type")) headers["content-type"] = "application/json";
        }
        return boundedRequest({ tool: "http.request", method, url, headers, body, timeoutMs, maxBytes });
      },
    },
    {
      name: "http.download",
      version: 1,
      description: "Download a url to a file under the workspace root (streamed, 50MB cap).",
      effect: "mutating",
      grants: ["net.read", "fs.write"],
      input: { type: "object", properties: { url: { type: "string" }, path: { type: "string" } }, required: ["url", "path"] },
      output: { type: "object", properties: { path: { type: "string" }, bytes_written: { type: "number" }, content_type: { type: "string" } } },
      handler: async (args: Row) => {
        const url = String(args.url ?? "");
        assertHttpUrl(url, "http.download");
        const target = resolveInRoot(root, args.path);
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), timeoutMs);
        let written = 0;
        try {
          const res = await followFetch({ tool: "http.download", method: "GET", url, signal: ctrl.signal });
          if (res.status >= 400) throw new RotorError("E_TOOL", `http.download: upstream returned ${res.status}`, { context: { url, status: res.status } });
          await mkdir(dirname(target), { recursive: true });
          const fh = await open(target, "w");
          try {
            const reader = res.body?.getReader();
            if (reader) {
              for (;;) {
                const { done, value } = await reader.read();
                if (done) break;
                written += value.byteLength;
                if (written > DOWNLOAD_CAP) {
                  await reader.cancel().catch(() => {});
                  throw new RotorError("E_TOOL", `http.download: body exceeds the ${DOWNLOAD_CAP} byte cap`, { context: { url } });
                }
                await fh.write(Buffer.from(value));
              }
            }
          } finally {
            await fh.close();
          }
          return { path: relative(resolve(root), target), bytes_written: written, content_type: res.headers.get("content-type") ?? "" };
        } catch (e) {
          await rm(target, { force: true }).catch(() => {}); // never leave a capped/failed partial
          if (e instanceof RotorError) throw e;
          const why = ctrl.signal.aborted ? `timed out after ${timeoutMs}ms` : (e as Error).message;
          throw new RotorError("E_TOOL", `http.download failed: ${why}`, { context: { url }, cause: e });
        } finally {
          clearTimeout(timer);
        }
      },
    },
    {
      name: "web.search",
      version: 1,
      description: "Web search (DuckDuckGo html endpoint). Returns up to 10 {title, url, snippet} results.",
      effect: "reading",
      grants: ["net.read"],
      input: { type: "object", properties: { query: { type: "string" }, max: { type: "number" } }, required: ["query"] },
      output: { type: "object", properties: { results: { type: "array" }, count: { type: "number" } } },
      handler: async (args: Row) => {
        const query = String(args.query ?? "").trim();
        if (!query) throw new RotorError("E_MISSING_INPUT", "web.search requires a `query`");
        const max = Math.max(1, Math.min(Number(args.max ?? SEARCH_CAP) || SEARCH_CAP, SEARCH_CAP));
        const url = "https://html.duckduckgo.com/html/?q=" + encodeURIComponent(query);
        const r = await boundedRequest({
          tool: "web.search",
          method: "GET",
          url,
          headers: { "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36" },
          timeoutMs,
          maxBytes,
        });
        const results = parseSearchResults(String(r.body ?? ""), max);
        return { results, count: results.length };
      },
    },
    {
      name: "html.text",
      version: 1,
      description: "Convert HTML to plain text: scripts/styles/comments stripped, entities decoded, whitespace collapsed.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { html: { type: "string" } }, required: ["html"] },
      output: { type: "object", properties: { text: { type: "string" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const text = htmlToText(String(args.html ?? ""));
        return { text: text.slice(0, TEXT_CAP), truncated: text.length > TEXT_CAP };
      },
    },
    {
      name: "html.links",
      version: 1,
      description: "Extract anchor links from HTML as {href, text}; relative hrefs resolve against `base`. Capped at 200.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { html: { type: "string" }, base: { type: "string" } }, required: ["html"] },
      output: { type: "object", properties: { links: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const html = String(args.html ?? "");
        const base = typeof args.base === "string" && args.base !== "" ? args.base : undefined;
        const all: Array<{ href: string; text: string }> = [];
        const aRe = /<a\b([^>]*)>([\s\S]*?)<\/a\s*>/gi;
        let m: RegExpExecArray | null;
        let total = 0;
        while ((m = aRe.exec(html))) {
          const raw = attr(m[1], "href");
          if (!raw) continue;
          total++;
          if (all.length >= LINKS_CAP) continue; // keep counting past the cap
          let href = raw;
          if (base) {
            try {
              href = new URL(raw, base).toString();
            } catch {
              /* keep the raw href when base resolution fails */
            }
          }
          all.push({ href, text: collapseWs(decodeEntities(stripTags(m[2]))).slice(0, 200) });
        }
        return { links: all, count: all.length, truncated: total > LINKS_CAP };
      },
    },
    {
      name: "html.meta",
      version: 1,
      description: "Extract page metadata from HTML: title, description, canonical url, and og:* properties.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { html: { type: "string" } }, required: ["html"] },
      output: {
        type: "object",
        properties: { title: { type: "string" }, description: { type: "string" }, canonical: { type: "string" }, og: { type: "object" } },
      },
      handler: async (args: Row) => {
        const html = String(args.html ?? "");
        const titleM = /<title[^>]*>([\s\S]*?)<\/title\s*>/i.exec(html);
        const title = titleM ? collapseWs(decodeEntities(stripTags(titleM[1]))).slice(0, 300) : "";
        let description = "";
        const og: Record<string, string> = {};
        const metaRe = /<meta\b([^>]*)\/?>/gi;
        let m: RegExpExecArray | null;
        while ((m = metaRe.exec(html))) {
          const name = attr(m[1], "name") ?? attr(m[1], "property") ?? "";
          const content = attr(m[1], "content") ?? "";
          if (!name || !content) continue;
          if (name.toLowerCase() === "description" && !description) description = content.slice(0, 500);
          else if (name.toLowerCase().startsWith("og:") && Object.keys(og).length < 20) og[name.slice(3)] = content.slice(0, 500);
        }
        let canonical = "";
        const linkRe = /<link\b([^>]*)\/?>/gi;
        while ((m = linkRe.exec(html))) {
          if ((attr(m[1], "rel") ?? "").toLowerCase() === "canonical") {
            canonical = attr(m[1], "href") ?? "";
            break;
          }
        }
        return { title, description, canonical, og };
      },
    },
    {
      name: "url.parse",
      version: 1,
      description: "Parse a url into {scheme, host, port, path, query, fragment}.",
      effect: "pure",
      grants: [],
      input: { type: "object", properties: { url: { type: "string" } }, required: ["url"] },
      output: {
        type: "object",
        properties: {
          scheme: { type: "string" },
          host: { type: "string" },
          port: { type: ["number", "null"] },
          path: { type: "string" },
          query: { type: "object" },
          fragment: { type: "string" },
        },
      },
      handler: async (args: Row) => {
        const raw = String(args.url ?? "");
        let u: URL;
        try {
          u = new URL(raw);
        } catch {
          throw new RotorError("E_MISSING_INPUT", `url.parse: not a valid absolute url: ${raw}`, { context: { url: raw } });
        }
        const query: Record<string, string> = {};
        u.searchParams.forEach((v, k) => {
          if (Object.keys(query).length < 100) query[k] = v;
        });
        return {
          scheme: u.protocol.replace(/:$/, ""),
          host: u.hostname,
          port: u.port ? Number(u.port) : null,
          path: u.pathname,
          query,
          fragment: u.hash.replace(/^#/, ""),
        };
      },
    },
    {
      name: "url.build",
      version: 1,
      description: "Build a url from parts: scheme (default https), host, port, path, query object, fragment.",
      effect: "pure",
      grants: [],
      input: {
        type: "object",
        properties: {
          scheme: { type: "string" },
          host: { type: "string" },
          port: { type: "number" },
          path: { type: "string" },
          query: { type: "object" },
          fragment: { type: "string" },
        },
        required: ["host"],
      },
      output: { type: "object", properties: { url: { type: "string" } } },
      handler: async (args: Row) => {
        const host = String(args.host ?? "").trim();
        if (!host) throw new RotorError("E_MISSING_INPUT", "url.build requires a `host`");
        const scheme = String(args.scheme ?? "https");
        let u: URL;
        try {
          u = new URL(`${scheme}://${host}`);
          if (args.port != null) u.port = String(args.port);
          if (typeof args.path === "string" && args.path !== "") u.pathname = args.path;
          if (args.query && typeof args.query === "object" && !Array.isArray(args.query)) {
            for (const [k, v] of Object.entries(args.query as Record<string, unknown>)) u.searchParams.set(k, String(v));
          }
          if (typeof args.fragment === "string" && args.fragment !== "") u.hash = args.fragment;
        } catch (e) {
          throw new RotorError("E_MISSING_INPUT", `url.build: invalid parts: ${(e as Error).message}`, { context: { host, scheme } });
        }
        return { url: u.toString() };
      },
    },
    {
      name: "rss.read",
      version: 1,
      description: "Fetch and parse an RSS 2.0 or Atom feed. Returns feed title and up to 50 items.",
      effect: "reading",
      grants: ["net.read"],
      input: { type: "object", properties: { url: { type: "string" } }, required: ["url"] },
      output: { type: "object", properties: { title: { type: "string" }, items: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const url = String(args.url ?? "");
        assertHttpUrl(url, "rss.read");
        const r = await boundedRequest({ tool: "rss.read", method: "GET", url, timeoutMs, maxBytes });
        return parseFeed(String(r.body ?? ""), RSS_ITEMS_CAP);
      },
    },
    {
      name: "dns.resolve",
      version: 1,
      description: "Resolve DNS records for a host: A (default), AAAA, MX, TXT, or CNAME.",
      effect: "reading",
      grants: ["net.read"],
      input: {
        type: "object",
        properties: { host: { type: "string" }, type: { type: "string", enum: ["A", "AAAA", "MX", "TXT", "CNAME"] } },
        required: ["host"],
      },
      output: { type: "object", properties: { records: { type: "array" }, count: { type: "number" }, truncated: { type: "boolean" } } },
      handler: async (args: Row) => {
        const host = String(args.host ?? "").trim();
        if (!host) throw new RotorError("E_MISSING_INPUT", "dns.resolve requires a `host`");
        const type = String(args.type ?? "A").toUpperCase();
        if (!["A", "AAAA", "MX", "TXT", "CNAME"].includes(type)) {
          throw new RotorError("E_MISSING_INPUT", `dns.resolve: unsupported record type ${type}`, { context: { host, type } });
        }
        try {
          const raw = await dns.resolve(host, type);
          let records: unknown[];
          if (type === "TXT") records = (raw as string[][]).map((r) => r.join(""));
          else if (type === "MX") records = (raw as Array<{ exchange: string; priority: number }>).map((r) => ({ exchange: r.exchange, priority: r.priority }));
          else records = raw as string[];
          // Don't hand back internal A/AAAA answers — that's internal-range recon.
          if ((type === "A" || type === "AAAA") && process.env.RROTOR_ALLOW_PRIVATE_NET !== "1") {
            records = (records as string[]).filter((ip) => !ipInBlockedRange(ip));
          }
          const capped = records.slice(0, 100);
          return { records: capped, count: capped.length, truncated: records.length > 100 };
        } catch (e) {
          throw new RotorError("E_TOOL", `dns.resolve failed for ${host}/${type}: ${(e as Error).message}`, { context: { host, type }, cause: e });
        }
      },
    },
  ];

  return { name: "web", version: "1.0.0", tools };
}

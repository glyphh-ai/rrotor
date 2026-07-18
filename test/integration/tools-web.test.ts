/**
 * The `web` tool pack exercised against a LOCAL node:http fixture server (no
 * outside network): raw HTTP bounds + redirects + header redaction, sandboxed
 * downloads, the pure HTML/URL parsers on fixture strings, RSS/Atom parsing,
 * and the search parser on canned DuckDuckGo markup (the live call is skipped).
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, rmSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as http from "node:http";
import type { AddressInfo } from "node:net";

import { BasicConnections } from "../../src/plugins/connections.js";
import { webPack, parseSearchResults } from "../../src/tools/web.js";

let root: string;
let server: http.Server;
let base: string;

const RSS_XML = `<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Rotor News</title>
  <item><title>First &amp; Foremost</title><link>https://example.com/1</link><pubDate>Mon, 01 Jul 2026 00:00:00 GMT</pubDate><description><![CDATA[The <b>first</b> item]]></description></item>
  <item><title>Second</title><link>https://example.com/2</link><pubDate>Tue, 02 Jul 2026 00:00:00 GMT</pubDate><description>plain second</description></item>
</channel></rss>`;

const ATOM_XML = `<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Rotor</title>
  <entry>
    <title>Atom One</title>
    <link rel="self" href="https://example.com/self"/>
    <link rel="alternate" href="https://example.com/a1"/>
    <published>2026-07-01T00:00:00Z</published>
    <summary>alpha summary</summary>
  </entry>
</feed>`;

const rssMany = (): string =>
  `<rss version="2.0"><channel><title>Many</title>${Array.from({ length: 60 }, (_, i) => `<item><title>t${i}</title><link>https://x/${i}</link></item>`).join("")}</channel></rss>`;

const DDG_HTML = `<html><body>
<div class="result">
  <h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">Example &amp; <b>Page</b></a></h2>
  <a class="result__snippet" href="#">A snippet about <b>things</b>.</a>
</div>
<div class="result">
  <h2 class="result__title"><a class="result__a" href="https://other.example.org/doc">Other Doc</a></h2>
  <a class="result__snippet" href="#">Second snippet.</a>
</div>
</body></html>`;

beforeAll(async () => {
  // The fixture server is on 127.0.0.1, which the SSRF guard blocks by default —
  // opt in for these tests (a dedicated test below proves the guard blocks it otherwise).
  process.env.RROTOR_ALLOW_PRIVATE_NET = "1";
  root = mkdtempSync(join(tmpdir(), "rrotor-web-"));
  server = http.createServer((req, res) => {
    const u = new URL(req.url ?? "/", "http://x");
    if (u.pathname === "/text") {
      res.writeHead(200, { "content-type": "text/plain", "x-api-key": "supersecret", "x-plain": "visible" });
      res.end("hello web");
    } else if (u.pathname === "/big") {
      res.writeHead(200, { "content-type": "text/plain" });
      res.end("x".repeat(10_000));
    } else if (u.pathname === "/redirect") {
      res.writeHead(302, { location: "/text" });
      res.end();
    } else if (u.pathname === "/loop") {
      res.writeHead(302, { location: "/loop" });
      res.end();
    } else if (u.pathname === "/echo") {
      let body = "";
      req.on("data", (d) => (body += d));
      req.on("end", () => {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ method: req.method, body, ct: req.headers["content-type"] ?? "" }));
      });
    } else if (u.pathname === "/file.bin") {
      res.writeHead(200, { "content-type": "application/octet-stream" });
      res.end(Buffer.from([1, 2, 3, 4, 5]));
    } else if (u.pathname === "/rss") {
      res.writeHead(200, { "content-type": "application/rss+xml" });
      res.end(RSS_XML);
    } else if (u.pathname === "/atom") {
      res.writeHead(200, { "content-type": "application/atom+xml" });
      res.end(ATOM_XML);
    } else if (u.pathname === "/rss-many") {
      res.writeHead(200, { "content-type": "application/rss+xml" });
      res.end(rssMany());
    } else {
      res.writeHead(404);
      res.end("nope");
    }
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(async () => {
  await new Promise<void>((r) => server.close(() => r()));
  rmSync(root, { recursive: true, force: true });
});

/** A connections plugin with the pack installed, plus a direct dispatch helper. */
function withPack(opts: { maxBytes?: number; timeoutMs?: number } = {}) {
  const c = new BasicConnections();
  for (const t of webPack({ root, ...opts }).tools) c.register(t.name, t.handler);
  return c;
}
const call = async (c: BasicConnections, name: string, args: Record<string, unknown>) => {
  const r = await c.dispatch(name, args);
  if (!r.ok) throw new Error(r.error);
  return r.result as Record<string, unknown>;
};

describe("SSRF guard", () => {
  it("blocks loopback, private, and cloud-metadata targets by default", async () => {
    delete process.env.RROTOR_ALLOW_PRIVATE_NET; // default posture
    const c = withPack();
    for (const url of [
      "http://127.0.0.1/",
      "http://169.254.169.254/latest/meta-data/",
      "http://10.0.0.1/",
      "http://192.168.1.1/admin",
      "http://[::1]/",
    ]) {
      await expect(call(c, "http.get", { url })).rejects.toThrow(/private|loopback|link-local/i);
    }
    process.env.RROTOR_ALLOW_PRIVATE_NET = "1"; // restore for the rest of the suite
  });
});

describe("http.get", () => {
  it("fetches a url, redacts secret-looking response headers", async () => {
    const c = withPack();
    const r = await call(c, "http.get", { url: `${base}/text` });
    expect(r.status).toBe(200);
    expect(r.body).toBe("hello web");
    expect(r.truncated).toBe(false);
    const headers = r.headers as Record<string, string>;
    expect(headers["x-api-key"]).toBe("<redacted>");
    expect(headers["x-plain"]).toBe("visible");
  });

  it("follows redirects to the final body", async () => {
    const c = withPack();
    const r = await call(c, "http.get", { url: `${base}/redirect` });
    expect(r.status).toBe(200);
    expect(r.body).toBe("hello web");
  });

  it("caps the body at maxBytes and flags truncation", async () => {
    const c = withPack({ maxBytes: 100 });
    const r = await call(c, "http.get", { url: `${base}/big` });
    expect(r.truncated).toBe(true);
    expect(r.bytes).toBe(100);
    expect(String(r.body).length).toBe(100);
  });

  it("refuses non-http(s) schemes and bounded redirect loops", async () => {
    const c = withPack();
    const bad = await c.dispatch("http.get", { url: "ftp://example.com/x" });
    expect(bad).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    await expect(c.dispatch("http.get", { url: "file:///etc/passwd" })).resolves.toMatchObject({ ok: false });
    const loop = await c.dispatch("http.get", { url: `${base}/loop` });
    expect(loop).toMatchObject({ ok: false, error: expect.stringMatching(/redirect/i) });
  });
});

describe("http.request", () => {
  it("POSTs json with an auto content-type and returns the echoed body", async () => {
    const c = withPack();
    const r = await call(c, "http.request", { method: "post", url: `${base}/echo`, json: { a: 1 } });
    expect(r.status).toBe(200);
    const echoed = JSON.parse(String(r.body)) as { method: string; body: string; ct: string };
    expect(echoed.method).toBe("POST");
    expect(JSON.parse(echoed.body)).toEqual({ a: 1 });
    expect(echoed.ct).toBe("application/json");
  });

  it("requires method and a valid http url", async () => {
    const c = withPack();
    await expect(c.dispatch("http.request", { method: "", url: `${base}/echo` })).resolves.toMatchObject({ ok: false });
    await expect(c.dispatch("http.request", { method: "GET", url: "gopher://x" })).resolves.toMatchObject({ ok: false });
  });
});

describe("http.download", () => {
  it("streams a url to a sandboxed file", async () => {
    const c = withPack();
    const r = await call(c, "http.download", { url: `${base}/file.bin`, path: "dl/f.bin" });
    expect(r.bytes_written).toBe(5);
    expect(r.content_type).toBe("application/octet-stream");
    expect(r.path).toBe(join("dl", "f.bin"));
    expect([...readFileSync(join(root, "dl/f.bin"))]).toEqual([1, 2, 3, 4, 5]);
  });

  it("refuses a path that escapes the workspace and cleans up on upstream failure", async () => {
    const c = withPack();
    const esc = await c.dispatch("http.download", { url: `${base}/file.bin`, path: "../evil.bin" });
    expect(esc).toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    const miss = await c.dispatch("http.download", { url: `${base}/missing`, path: "dl/miss.bin" });
    expect(miss).toMatchObject({ ok: false });
    expect(existsSync(join(root, "dl/miss.bin"))).toBe(false); // no partial left behind
  });
});

describe("web.search", () => {
  it("parses DDG result markup, unwrapping uddg redirect hrefs", () => {
    const results = parseSearchResults(DDG_HTML, 10);
    expect(results).toHaveLength(2);
    expect(results[0]).toEqual({ title: "Example & Page", url: "https://example.com/page", snippet: "A snippet about things." });
    expect(results[1].url).toBe("https://other.example.org/doc");
    expect(results[1].snippet).toBe("Second snippet.");
  });

  it("returns [] on layout drift instead of throwing, and caps at max", () => {
    expect(parseSearchResults("<html><body><p>totally different layout</p></body></html>", 10)).toEqual([]);
    expect(parseSearchResults(DDG_HTML, 1)).toHaveLength(1);
  });

  it("rejects an empty query", async () => {
    const c = withPack();
    await expect(c.dispatch("web.search", { query: "  " })).resolves.toMatchObject({ ok: false });
  });

  it.skip("live: searches the real DuckDuckGo html endpoint", async () => {
    const c = withPack();
    const r = await call(c, "web.search", { query: "rotorspec deterministic agent loops" });
    expect(Number(r.count)).toBeGreaterThan(0);
  });
});

describe("html.text / html.links / html.meta (pure)", () => {
  const PAGE = `<html><head>
    <title>My &amp; Page</title>
    <meta name="description" content="A test &quot;page&quot;">
    <meta property="og:title" content="OG Title"><meta property="og:image" content="https://example.com/i.png">
    <link rel="canonical" href="https://example.com/canon">
    <style>.x { color: red }</style>
    <script>var hidden = "nope";</script>
  </head><body>
    <!-- a comment -->
    <p>Hello&nbsp;<b>world</b> &lt;tag&gt;</p>
    <a href="/rel">Relative</a>
    <a href="https://abs.example.com/">Absolute</a>
    <a name="anchor-without-href">skip me</a>
  </body></html>`;

  it("html.text strips script/style/comments, decodes entities, collapses whitespace", async () => {
    const c = withPack();
    const r = await call(c, "html.text", { html: PAGE });
    const text = String(r.text);
    expect(text).toContain("Hello world <tag>");
    expect(text).not.toContain("hidden");
    expect(text).not.toContain("color: red");
    expect(text).not.toContain("a comment");
    expect(r.truncated).toBe(false);
  });

  it("html.text caps output at 200k and flags truncation", async () => {
    const c = withPack();
    const r = await call(c, "html.text", { html: "y".repeat(250_000) });
    expect(String(r.text).length).toBe(200_000);
    expect(r.truncated).toBe(true);
  });

  it("html.links resolves relative hrefs against base and caps at 200", async () => {
    const c = withPack();
    const r = await call(c, "html.links", { html: PAGE, base: "https://example.com/dir/" });
    const links = r.links as Array<{ href: string; text: string }>;
    expect(links).toEqual([
      { href: "https://example.com/rel", text: "Relative" },
      { href: "https://abs.example.com/", text: "Absolute" },
    ]);
    expect(r.truncated).toBe(false);

    const many = Array.from({ length: 250 }, (_, i) => `<a href="/p${i}">l${i}</a>`).join("");
    const capped = await call(c, "html.links", { html: many });
    expect((capped.links as unknown[]).length).toBe(200);
    expect(capped.count).toBe(200);
    expect(capped.truncated).toBe(true);
  });

  it("html.meta extracts title, description, canonical and og:*", async () => {
    const c = withPack();
    const r = await call(c, "html.meta", { html: PAGE });
    expect(r.title).toBe("My & Page");
    expect(r.description).toBe('A test "page"');
    expect(r.canonical).toBe("https://example.com/canon");
    expect(r.og).toEqual({ title: "OG Title", image: "https://example.com/i.png" });
  });
});

describe("url.parse / url.build (pure)", () => {
  it("parses a url into its parts", async () => {
    const c = withPack();
    const r = await call(c, "url.parse", { url: "https://example.com:8443/a/b?x=1&y=two#frag" });
    expect(r).toEqual({ scheme: "https", host: "example.com", port: 8443, path: "/a/b", query: { x: "1", y: "two" }, fragment: "frag" });
  });

  it("rejects a non-url", async () => {
    const c = withPack();
    await expect(c.dispatch("url.parse", { url: "not a url" })).resolves.toMatchObject({ ok: false });
  });

  it("builds a url from parts (and requires host)", async () => {
    const c = withPack();
    const r = await call(c, "url.build", { host: "example.com", port: 8443, path: "/a/b", query: { x: "1", y: "two" }, fragment: "frag" });
    expect(r.url).toBe("https://example.com:8443/a/b?x=1&y=two#frag");
    expect((await call(c, "url.build", { host: "example.com" })).url).toBe("https://example.com/");
    await expect(c.dispatch("url.build", { host: "" })).resolves.toMatchObject({ ok: false });
  });
});

describe("rss.read", () => {
  it("parses an RSS 2.0 feed (entities + CDATA)", async () => {
    const c = withPack();
    const r = await call(c, "rss.read", { url: `${base}/rss` });
    expect(r.title).toBe("Rotor News");
    expect(r.count).toBe(2);
    const items = r.items as Array<{ title: string; link: string; published: string; summary: string }>;
    expect(items[0]).toEqual({ title: "First & Foremost", link: "https://example.com/1", published: "Mon, 01 Jul 2026 00:00:00 GMT", summary: "The first item" });
    expect(items[1].summary).toBe("plain second");
  });

  it("parses an Atom feed, preferring the alternate link", async () => {
    const c = withPack();
    const r = await call(c, "rss.read", { url: `${base}/atom` });
    expect(r.title).toBe("Atom Rotor");
    const items = r.items as Array<{ title: string; link: string; published: string; summary: string }>;
    expect(items[0]).toEqual({ title: "Atom One", link: "https://example.com/a1", published: "2026-07-01T00:00:00Z", summary: "alpha summary" });
  });

  it("caps items at 50 and refuses non-http urls", async () => {
    const c = withPack();
    const r = await call(c, "rss.read", { url: `${base}/rss-many` });
    expect(r.count).toBe(50);
    expect(r.truncated).toBe(true);
    await expect(c.dispatch("rss.read", { url: "ftp://feed" })).resolves.toMatchObject({ ok: false });
  });
});

describe("dns.resolve", () => {
  it("rejects a missing host and an unsupported record type without touching the network", async () => {
    const c = withPack();
    await expect(c.dispatch("dns.resolve", { host: " " })).resolves.toMatchObject({ ok: false });
    const bad = await c.dispatch("dns.resolve", { host: "example.com", type: "SRV" });
    expect(bad).toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
  });

  it.skip("live: resolves an A record", async () => {
    const c = withPack();
    const r = await call(c, "dns.resolve", { host: "example.com", type: "A" });
    expect(Number(r.count)).toBeGreaterThan(0);
  });
});

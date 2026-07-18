/**
 * `data` tool pack — pure data workbench (JSON/YAML/CSV/XML/schema/codecs).
 * Every tool gets a happy path plus its important error/bound path, dispatched
 * directly through a BasicConnections registry like the stdlib tool tests.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { BasicConnections } from "../../src/plugins/connections.js";
import { dataPack } from "../../src/tools/data.js";

let root: string;
beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "rrotor-data-"));
});
afterAll(() => rmSync(root, { recursive: true, force: true }));

function withPack(): BasicConnections {
  const c = new BasicConnections();
  for (const t of dataPack().tools) c.register(t.name, t.handler);
  return c;
}
const call = async (c: BasicConnections, name: string, args: Record<string, unknown>) => {
  const r = await c.dispatch(name, args);
  if (!r.ok) throw new Error(r.error);
  return r.result as Record<string, unknown>;
};

const c = withPack();

describe("data pack — shape", () => {
  it("declares every tool pure with empty grants (available in every mode)", () => {
    const pack = dataPack();
    expect(pack.name).toBe("data");
    expect(pack.version).toBe("1.0.0");
    for (const t of pack.tools) {
      expect(t.effect).toBe("pure");
      expect(t.grants).toEqual([]);
      expect((t.input as { type?: unknown }).type).toBe("object");
    }
  });
});

describe("json.parse", () => {
  it("parses strict JSON and strips a ```json fence", async () => {
    expect((await call(c, "json.parse", { text: '{"a": [1, 2]}' })).value).toEqual({ a: [1, 2] });
    expect((await call(c, "json.parse", { text: '```json\n{"b": true}\n```' })).value).toEqual({ b: true });
  });
  it("refuses malformed JSON and a missing arg", async () => {
    await expect(c.dispatch("json.parse", { text: "{nope" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_TOOL/) });
    await expect(c.dispatch("json.parse", {})).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
  });
});

describe("json.stringify", () => {
  it("serializes compact and pretty", async () => {
    expect((await call(c, "json.stringify", { value: { a: 1 } })).text).toBe('{"a":1}');
    expect((await call(c, "json.stringify", { value: { a: 1 }, pretty: true })).text).toBe('{\n  "a": 1\n}');
  });
  it("requires `value`", async () => {
    await expect(c.dispatch("json.stringify", {})).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
  });
});

describe("json.query", () => {
  const doc = { users: [{ name: "ada", age: 36 }, { name: "grace" }], meta: { count: 2 } };
  it("walks dot/bracket paths with [n] and [*]", async () => {
    expect(await call(c, "json.query", { value: doc, path: "meta.count" })).toMatchObject({ result: 2, found: true });
    expect((await call(c, "json.query", { value: doc, path: "users[1].name" })).result).toBe("grace");
    expect((await call(c, "json.query", { value: doc, path: "users[*].name" })).result).toEqual(["ada", "grace"]);
  });
  it("accepts JSON text input and reports found:false on a miss", async () => {
    expect((await call(c, "json.query", { text: JSON.stringify(doc), path: "users[0].age" })).result).toBe(36);
    expect(await call(c, "json.query", { value: doc, path: "meta.missing" })).toMatchObject({ found: false, result: null });
    expect(await call(c, "json.query", { value: doc, path: "users[*].age" })).toMatchObject({ result: [36], found: true });
  });
  it("refuses a bad path", async () => {
    await expect(c.dispatch("json.query", { value: doc, path: "users[x]" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
  });
});

describe("json.diff", () => {
  it("reports add/remove/replace with paths", async () => {
    const r = await call(c, "json.diff", {
      a: { name: "a", tags: ["x", "y"], keep: 1, gone: true },
      b: { name: "b", tags: ["x"], keep: 1, fresh: null },
    });
    expect(r.count).toBe(4);
    expect(r.changes).toEqual(
      expect.arrayContaining([
        { path: "name", op: "replace", from: "a", to: "b" },
        { path: "tags[1]", op: "remove", from: "y" },
        { path: "gone", op: "remove", from: true },
        { path: "fresh", op: "add", to: null },
      ]),
    );
  });
  it("returns zero changes for deep-equal values, and truncates a flood", async () => {
    expect((await call(c, "json.diff", { a: { x: [1, { y: 2 }] }, b: { x: [1, { y: 2 }] } })).count).toBe(0);
    const big = Array.from({ length: 1500 }, (_, i) => i);
    const r = await call(c, "json.diff", { a: [], b: big });
    expect(r.truncated).toBe(true);
    expect((r.changes as unknown[]).length).toBe(1000);
  });
});

describe("json.patch", () => {
  it("applies an RFC-6902 add/replace/copy/move/remove/test sequence", async () => {
    const r = await call(c, "json.patch", {
      value: { a: { b: 1 }, list: [1, 2] },
      ops: [
        { op: "test", path: "/a/b", value: 1 },
        { op: "replace", path: "/a/b", value: 2 },
        { op: "add", path: "/list/-", value: 3 },
        { op: "copy", from: "/a", path: "/c" },
        { op: "move", from: "/list/0", path: "/first" },
        { op: "remove", path: "/c/b" },
      ],
    });
    expect(r.value).toEqual({ a: { b: 2 }, list: [2, 3], c: {}, first: 1 });
    expect(r.applied).toBe(6);
  });
  it("does not mutate the input value and fails a false `test`", async () => {
    const value = { a: 1 };
    await call(c, "json.patch", { value, ops: [{ op: "replace", path: "/a", value: 9 }] });
    expect(value.a).toBe(1);
    await expect(c.dispatch("json.patch", { value, ops: [{ op: "test", path: "/a", value: 2 }] })).resolves.toMatchObject({
      ok: false,
      error: expect.stringMatching(/E_TOOL/),
    });
    await expect(c.dispatch("json.patch", { value, ops: [{ op: "remove", path: "/nope" }] })).resolves.toMatchObject({ ok: false });
    await expect(c.dispatch("json.patch", { value, ops: [{ op: "wat", path: "/a" }] })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
  });
});

describe("yaml", () => {
  it("parses and stringifies (round trip)", async () => {
    const parsed = await call(c, "yaml.parse", { text: "name: rotor\nitems:\n  - 1\n  - two\n" });
    expect(parsed.value).toEqual({ name: "rotor", items: [1, "two"] });
    const text = String((await call(c, "yaml.stringify", { value: parsed.value })).text);
    expect((await call(c, "yaml.parse", { text })).value).toEqual(parsed.value);
  });
  it("refuses invalid YAML", async () => {
    await expect(c.dispatch("yaml.parse", { text: "{" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_TOOL/) });
  });
});

describe("csv.parse", () => {
  it("handles RFC-4180 quotes, escaped quotes and embedded newlines", async () => {
    const text = 'name,notes\r\nada,"line1\nline2"\ngrace,"said ""hi"", left"\n';
    const r = await call(c, "csv.parse", { text, headers: true });
    expect(r.columns).toEqual(["name", "notes"]);
    expect(r.rows).toEqual([
      { name: "ada", notes: "line1\nline2" },
      { name: "grace", notes: 'said "hi", left' },
    ]);
    expect(r.count).toBe(2);
    expect(r.truncated).toBe(false);
  });
  it("supports a custom delimiter and raw (headerless) rows", async () => {
    const r = await call(c, "csv.parse", { text: "a;b\n1;2\n", delimiter: ";" });
    expect(r.rows).toEqual([
      ["a", "b"],
      ["1", "2"],
    ]);
  });
  it("caps at 10000 rows with truncated:true and refuses an unterminated quote", async () => {
    const text = Array.from({ length: 10_050 }, (_, i) => `row${i},x`).join("\n");
    const r = await call(c, "csv.parse", { text });
    expect((r.rows as unknown[]).length).toBe(10_000);
    expect(r.truncated).toBe(true);
    await expect(c.dispatch("csv.parse", { text: 'a,"unterminated' })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_TOOL/) });
  });
});

describe("csv.stringify", () => {
  it("emits a header for objects and quotes delimiters/quotes/newlines", async () => {
    const rows = [
      { name: "ada", notes: "line1\nline2" },
      { name: "grace", notes: 'said "hi", left' },
    ];
    const text = String((await call(c, "csv.stringify", { rows })).text);
    expect(text.startsWith("name,notes\n")).toBe(true);
    // Round trip through csv.parse restores the exact records.
    const back = await call(c, "csv.parse", { text, headers: true });
    expect(back.rows).toEqual(rows);
  });
  it("handles array rows with explicit columns and refuses non-array input", async () => {
    const text = String((await call(c, "csv.stringify", { rows: [[1, "a;b"]], columns: ["n", "s"], delimiter: ";" })).text);
    expect(text).toBe('n;s\n1;"a;b"\n');
    await expect(c.dispatch("csv.stringify", { rows: "nope" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_MISSING_INPUT/) });
  });
});

describe("xml.parse", () => {
  it("parses elements, attrs, text, CDATA and entities", async () => {
    const r = await call(c, "xml.parse", {
      text: '<?xml version="1.0"?><!-- top --><doc id="d1">\n  <item n="1">a &amp; b</item>\n  <item n="2"><![CDATA[<raw>]]></item>\n  <empty/>\n</doc>',
    });
    const doc = r.value as { tag: string; attrs: Record<string, string>; children: Array<{ tag: string; attrs: Record<string, string>; text: string }> };
    expect(doc.tag).toBe("doc");
    expect(doc.attrs).toEqual({ id: "d1" });
    expect(doc.children.map((ch) => ch.tag)).toEqual(["item", "item", "empty"]);
    expect(doc.children[0].text).toBe("a & b");
    expect(doc.children[1].text).toBe("<raw>");
  });
  it("refuses DOCTYPE (XXE guard) with E_POLICY_DENIED and rejects malformed XML", async () => {
    const evil = '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><doc>&xxe;</doc>';
    await expect(c.dispatch("xml.parse", { text: evil })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_POLICY_DENIED/) });
    await expect(c.dispatch("xml.parse", { text: "<a><b></a>" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_TOOL/) });
  });
});

describe("jsonschema.validate", () => {
  it("validates values, including formats", async () => {
    const schema = { type: "object", properties: { email: { type: "string", format: "email" } }, required: ["email"] };
    expect((await call(c, "jsonschema.validate", { value: { email: "a@b.co" }, schema })).valid).toBe(true);
    const bad = await call(c, "jsonschema.validate", { value: { email: "not-an-email" }, schema });
    expect(bad.valid).toBe(false);
    expect((bad.errors as Array<{ keyword: string }>)[0].keyword).toBe("format");
  });
  it("caps at 20 errors and refuses a schema that does not compile", async () => {
    const schema = { type: "object", required: Array.from({ length: 30 }, (_, i) => `k${i}`) };
    const r = await call(c, "jsonschema.validate", { value: {}, schema });
    expect(r.valid).toBe(false);
    expect((r.errors as unknown[]).length).toBe(20);
    expect(r.error_count).toBe(30);
    expect(r.truncated).toBe(true);
    await expect(c.dispatch("jsonschema.validate", { value: 1, schema: { type: "not-a-type" } })).resolves.toMatchObject({
      ok: false,
      error: expect.stringMatching(/E_MISSING_INPUT/),
    });
  });
});

describe("base64", () => {
  it("round-trips text and passes through bytes_b64 normalized", async () => {
    const enc = await call(c, "base64.encode", { text: "hé!" });
    const dec = await call(c, "base64.decode", { b64: String(enc.b64) });
    expect(dec.text).toBe("hé!");
    expect(dec.bytes).toBe(4);
    expect((await call(c, "base64.encode", { bytes_b64: "aGk=" })).b64).toBe("aGk=");
  });
  it("refuses invalid base64", async () => {
    await expect(c.dispatch("base64.decode", { b64: "!!!not-base64!!!" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_TOOL/) });
    await expect(c.dispatch("base64.decode", { b64: "abcde" })).resolves.toMatchObject({ ok: false }); // bad length
  });
});

describe("hex", () => {
  it("round-trips text", async () => {
    const enc = await call(c, "hex.encode", { text: "hi" });
    expect(enc.hex).toBe("6869");
    expect((await call(c, "hex.decode", { hex: "6869" })).text).toBe("hi");
  });
  it("refuses non-hex and odd-length input", async () => {
    await expect(c.dispatch("hex.decode", { hex: "zz" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_TOOL/) });
    await expect(c.dispatch("hex.decode", { hex: "abc" })).resolves.toMatchObject({ ok: false });
  });
});

describe("url", () => {
  it("encodes as a whole URI by default and as a component on request", async () => {
    expect((await call(c, "url.encode", { text: "https://x.dev/a b?q=1" })).text).toBe("https://x.dev/a%20b?q=1");
    expect((await call(c, "url.encode", { text: "a b&c=d", component: true })).text).toBe("a%20b%26c%3Dd");
  });
  it("decodes and refuses malformed escapes", async () => {
    expect((await call(c, "url.decode", { text: "a%20b%26c" })).text).toBe("a b&c");
    await expect(c.dispatch("url.decode", { text: "%E0%A4%A" })).resolves.toMatchObject({ ok: false, error: expect.stringMatching(/E_TOOL/) });
  });
});

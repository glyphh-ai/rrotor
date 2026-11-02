/* rrotor.js run in node by tests/daemon/api/test_rrotor_js.py: a fake fetch answering by path, a bare document,
   no waiting (setTimeout fires at once). Prints "ok" and exits 0 when every check holds. */
const fs = require("fs");
const assert = require("assert");
const calls = [];
const answers = {};
function node(tag) {
  return {
    tag, children: [], attrs: {}, textContent: "", className: "", value: "", parentNode: null,
    setAttribute(k, v) { this.attrs[k] = v; },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
    attachShadow() { return this; },
  };
}
globalThis.window = globalThis;
globalThis.location = { pathname: "/apps/board/" };
globalThis.document = { body: node("body"), createElement: node, createTextNode: (t) => ({ text: t }) };
globalThis.setTimeout = (fn) => { fn(); return 0; };  // no waiting in a test: the poll's two seconds pass at once
globalThis.fetch = async (url, init) => {
  calls.push({ url, method: init.method, body: init.body === undefined ? null : JSON.parse(init.body) });
  const next = (answers[url] || []).shift() || { status: 404, body: { error: { code: "E_NOPE", message: "no" } } };
  return { ok: next.status < 400, status: next.status, text: async () => JSON.stringify(next.body) };
};
const source = fs.readFileSync(process.argv[2], "utf8");
function load() { eval(source); return window.rrotor; }
async function refused(promise) { try { await promise; } catch (err) { return err; } throw new Error("no error"); }
(async () => {
  let rrotor = load();
  assert.strictEqual(rrotor.name, "board");
  assert.strictEqual(rrotor["continue"], rrotor.resume);
  answers["/apps/board/call/rows"] = [{ status: 200, body: { status: "done", data: { rows: [1] } } }];
  assert.deepStrictEqual(await rrotor.call("rows", { n: 1 }), { rows: [1] });
  assert.deepStrictEqual(calls[0], { url: "/apps/board/call/rows", method: "POST", body: { n: 1 } });
  answers["/apps/board/call/ask"] = [
    { status: 200, body: { status: "needs_input", run_id: "r1", step: "day", prompt: "Which day?" } },
  ];
  let err = await refused(rrotor.call("ask"));
  assert.deepStrictEqual([err.code, err.runId, err.step, err.prompt], ["E_NEEDS_INPUT", "r1", "day", "Which day?"]);
  answers["/apps/board/runs/r1/continue"] = [{ status: 200, body: { status: "done", data: "monday" } }];
  assert.strictEqual(await rrotor["continue"]("r1", "monday"), "monday");
  assert.deepStrictEqual(calls[calls.length - 1].body, { answer: "monday" });
  answers["/apps/board/call/bad"] = [
    { status: 200, body: { status: "failed", run_id: "r3", data: { code: "E_BOOM", reason: "boom" } } },
    { status: 403, body: { error: { code: "E_APP_FUNCTION", message: "The page may not call bad." } } },
  ];
  err = await refused(rrotor.call("bad"));
  assert.deepStrictEqual([err.code, err.message, err.runId], ["E_BOOM", "boom", "r3"]);
  err = await refused(rrotor.call("bad"));
  assert.deepStrictEqual([err.code, err.status, err.message], ["E_APP_FUNCTION", 403, "The page may not call bad."]);
  answers["/apps/board/me"] = [{ status: 200, body: { name: "board", title: "B", type: "page", run_as: "app", viewer: null } }];
  assert.strictEqual((await rrotor.app).run_as, "app");
  assert.strictEqual((await rrotor.app).name, "board");  // read once
  assert.strictEqual(await rrotor.ready(), true);  // an app that runs as itself: nothing to sign in
  assert.ok(!calls.some((c) => c.url === "/apps/board/clis"));

  rrotor = load();  // a fresh page: an app that runs as the viewer, one CLI not signed in
  answers["/apps/board/me"] = [{ status: 200, body: { name: "board", run_as: "viewer", viewer: { sub: "u1" } } }];
  const clis = [
    { name: "tool", title: "Tool", signed_in: false, login: true },
    { name: "done", title: "Done", signed_in: true, login: true },
    { name: "fixed", title: "Fixed", signed_in: false, login: false },
  ];
  answers["/apps/board/clis"] = [{ status: 200, body: { clis } }, { status: 200, body: { clis } }];
  assert.deepStrictEqual(await rrotor.clis(), clis);
  const asked = { status: "needs_input", run_id: "r2", step: "login", data: { url: "https://a.example/x", code: "AB-12", typed: false } };
  answers["/apps/board/clis/tool/login"] = [{ status: 200, body: asked }];
  answers["/apps/board/runs/r2/continue"] = [{ status: 200, body: { status: "done", data: { signed_in: true } } }];
  answers["/apps/board/runs/r2"] = [{ status: 200, body: asked }, { status: 200, body: { status: "done", data: {} } }];
  assert.strictEqual(await rrotor.ready(), true);
  const after = calls.map((c) => c.method + " " + c.url);
  assert.ok(after.includes("POST /apps/board/clis/tool/login"));
  assert.ok(after.includes("POST /apps/board/runs/r2/continue"));
  assert.ok(after.filter((c) => c === "GET /apps/board/runs/r2").length >= 1);
  assert.deepStrictEqual(calls.find((c) => c.url === "/apps/board/runs/r2/continue").body, { answer: { code: "AB-12" } });
  assert.ok(!after.includes("POST /apps/board/clis/done/login") && !after.includes("POST /apps/board/clis/fixed/login"));
  const card = document.body.children[document.body.children.length - 1];
  const texts = JSON.stringify(card);
  assert.ok(texts.includes("Sign in to Tool to continue") && texts.includes("AB-12") && texts.includes("Copy"));
  assert.ok(texts.includes("Signed in") && texts.includes("https://a.example/x") && texts.includes("_blank"));

  rrotor = load();  // a sign-in that fails
  answers["/apps/board/me"] = [{ status: 200, body: { run_as: "viewer" } }];
  answers["/apps/board/clis"] = [{ status: 200, body: { clis: [clis[0]] } }];
  answers["/apps/board/clis/tool/login"] = [{ status: 200, body: { status: "failed", run_id: "r4", data: { code: "E_CLI_EXEC", reason: "no" } } }];
  err = await refused(rrotor.ready(["tool"]));
  assert.strictEqual(err.code, "E_CLI_EXEC");
  console.log("ok");
})().catch((err) => { console.error(err); process.exit(1); });

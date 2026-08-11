/**
 * The engine's frame translation, with the Claude Agent SDK faked: an
 * injected queryFn yields SDK-shaped messages and the engine must emit the
 * desktop-parity frame stream (delta/tool/progress/done|error), honor abort,
 * wire the permission gate into canUseTool, and keep secrets out of frames.
 */

import { describe, it, expect } from "vitest";
import { mkdtempSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { runHarness, buildQueryArgs, buildPrompt, assemblePrompt, SANDBOX_TOOLS } from "../../src/harness/engine.js";
import type { QueryFn } from "../../src/harness/engine.js";
import { HarnessSession } from "../../src/harness/session.js";
import type { HarnessRunConfig } from "../../src/harness/config.js";
import type { WireFrame } from "../../src/harness/frames.js";

function cfg(over: Partial<HarnessRunConfig> = {}): HarnessRunConfig {
  const home = mkdtempSync(join(tmpdir(), "engine-"));
  return {
    runId: over.runId ?? "run-t",
    sessionId: "sess-t",
    prompt: "do the thing",
    history: [],
    mode: "code",
    permission: "auto",
    gatewayUrl: "https://gw.test",
    runtimeToken: "gy_rt_engine_secret",
    workdir: join(home, "workspace"),
    attachDir: join(home, "workspace"),
    configDir: join(home, "agent-config"),
    attachments: [],
    attachmentMaxBytes: 1024,
    maxTurns: 0,
    ...over,
  };
}

const stream = (...msgs: unknown[]): QueryFn => () =>
  (async function* () {
    for (const m of msgs) yield m;
  })();

const frames = (s: HarnessSession): WireFrame[] => s.framesSince(-1);
const types = (s: HarnessSession): string[] => frames(s).map((f) => f.type);

describe("runHarness — frame translation", () => {
  it("streams partial text as delta frames and closes with progress + done", async () => {
    const s = new HarnessSession({ runId: "run-t" });
    await runHarness(
      s,
      cfg(),
      {
        queryFn: stream(
          { type: "stream_event", event: { type: "message_start", message: { usage: { input_tokens: 100, cache_read_input_tokens: 20 } } } },
          { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "Hel" } } },
          { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "lo" } } },
          { type: "result", subtype: "success", result: "Hello", num_turns: 1, usage: { input_tokens: 120, output_tokens: 7 } },
        ),
      },
    );
    const all = frames(s);
    const deltas = all.filter((f) => f.type === "delta").map((f) => (f as { delta: string }).delta);
    expect(deltas.join("")).toBe("Hello");
    // The result's usage is authoritative.
    const lastProgress = all.filter((f) => f.type === "progress").pop() as { inTokens: number; outTokens: number };
    expect(lastProgress).toMatchObject({ inTokens: 120, outTokens: 7 });
    expect(all[all.length - 1]).toMatchObject({ type: "done", stopped: false });
    expect(s.status).toBe("done");
  });

  it("falls back to whole-message deltas when no partials stream", async () => {
    const s = new HarnessSession({});
    await runHarness(
      s,
      cfg(),
      {
        queryFn: stream(
          { type: "assistant", message: { content: [{ type: "text", text: "first" }], usage: { input_tokens: 10, output_tokens: 2 } } },
          { type: "assistant", message: { content: [{ type: "text", text: "second" }], usage: { input_tokens: 5, output_tokens: 3 } } },
          { type: "result", subtype: "success", result: "" },
        ),
      },
    );
    const deltas = frames(s).filter((f) => f.type === "delta").map((f) => (f as { delta: string }).delta);
    expect(deltas).toEqual(["first", "\n\nsecond"]);
  });

  it("emits tool start (from tool_use) and done (from tool_result), correlated by id", async () => {
    const s = new HarnessSession({});
    await runHarness(
      s,
      cfg(),
      {
        queryFn: stream(
          { type: "assistant", message: { content: [{ type: "tool_use", id: "tu1", name: "Bash", input: { command: "ls" } }] } },
          { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "tu1", content: "a.txt\nb.txt", is_error: false }] } },
          { type: "assistant", message: { content: [{ type: "tool_use", id: "tu2", name: "Read", input: { file_path: "/x" } }] } },
          { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "tu2", content: [{ type: "text", text: "ERROR: nope" }], is_error: true }] } },
          { type: "result", subtype: "success", result: "done" },
        ),
      },
    );
    const tools = frames(s).filter((f) => f.type === "tool");
    expect(tools[0]).toMatchObject({ phase: "start", name: "Bash" });
    expect(tools[1]).toMatchObject({ phase: "done", name: "Bash", failed: false, preview: "a.txt\nb.txt" });
    expect(tools[2]).toMatchObject({ phase: "start", name: "Read" });
    expect(tools[3]).toMatchObject({ phase: "done", name: "Read", failed: true });
  });

  it("maps a non-success result subtype to a branded error frame", async () => {
    const s = new HarnessSession({});
    await runHarness(s, cfg(), { queryFn: stream({ type: "result", subtype: "error_max_turns" }) });
    const last = frames(s).pop() as { type: string; error: string };
    expect(last.type).toBe("error");
    expect(last.error).toBe("run ended: error max turns");
    expect(s.status).toBe("error");
  });

  it("redacts the runtime token from error frames and tool previews", async () => {
    const s = new HarnessSession({});
    const c = cfg();
    await runHarness(
      s,
      c,
      {
        queryFn: () =>
          (async function* () {
            yield { type: "assistant", message: { content: [{ type: "tool_use", id: "t", name: "Bash", input: {} }] } };
            yield { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "t", content: `leak ${c.runtimeToken}`, is_error: true }] } };
            throw new Error(`gateway said 401 for ${c.runtimeToken}`);
          })(),
      },
    );
    const all = JSON.stringify(frames(s));
    expect(all).not.toContain(c.runtimeToken);
    expect(all).toContain("•••");
  });

  it("an abort mid-stream ends with done{stopped:true}", async () => {
    const s = new HarnessSession({});
    const queryFn: QueryFn = () =>
      (async function* () {
        yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "part" } } };
        s.stop();
        yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "never" } } };
      })();
    await runHarness(s, cfg(), { queryFn });
    const last = frames(s).pop();
    expect(last).toMatchObject({ type: "done", stopped: true });
    expect(s.status).toBe("stopped");
  });

  it("a failed attachment download fails the run before the loop starts", async () => {
    const s = new HarnessSession({});
    const c = cfg({ attachments: [{ name: "a.txt", url: "https://files.test/a" }] });
    const fetchFn = (async () => ({ ok: false, status: 500, headers: new Headers(), body: null })) as unknown as typeof fetch;
    let queried = false;
    await runHarness(s, c, {
      fetchFn,
      queryFn: () => {
        queried = true;
        return (async function* () { /* never */ })();
      },
    });
    expect(queried).toBe(false);
    expect(types(s)).toEqual(["error"]);
  });
});

describe("buildQueryArgs — the SDK wiring", () => {
  it("code mode gets the sandbox toolset + ask_user, anchored at the workspace", () => {
    const c = cfg();
    const s = new HarnessSession({});
    const { options } = buildQueryArgs(c, s, []);
    expect(options.cwd).toBe(c.workdir);
    expect(options.tools).toEqual(SANDBOX_TOOLS);
    expect(options.settingSources).toEqual([]);
    expect(options.persistSession).toBe(false);
    expect(options.includePartialMessages).toBe(true);
    const env = options.env as Record<string, string | undefined>;
    expect(env.ANTHROPIC_BASE_URL).toBe(c.gatewayUrl);
    expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe(`x-glyphh-run: ${c.runId}`);
    expect(env.CLAUDE_CONFIG_DIR).toBe(c.configDir);
  });

  it("chat mode is tool-less: no built-ins, no MCP", () => {
    const { options } = buildQueryArgs(cfg({ mode: "chat" }), new HarnessSession({}), []);
    expect(options.tools).toEqual([]);
    expect(options.mcpServers).toBeUndefined();
    expect(options.allowedTools).toBeUndefined();
  });

  it("canUseTool routes through the gate: an approval frame pauses, the answer resumes", async () => {
    const c = cfg({ permission: "ask" });
    const s = new HarnessSession({});
    const { options } = buildQueryArgs(c, s, [], { approvalTimeoutMs: 5000 });
    const canUse = options.canUseTool as (t: string, i: unknown) => Promise<{ behavior: string; message?: string }>;

    const pendingDecision = canUse("Bash", { command: "npm test" });
    await new Promise((r) => setTimeout(r, 0)); // let the frame land
    const frame = s.framesSince(-1).find((f) => f.type === "approval") as Extract<WireFrame, { type: "approval" }>;
    expect(frame).toMatchObject({ kind: "command", title: "Run npm test" });
    s.answer(frame.id, { allow: true });
    expect((await pendingDecision).behavior).toBe("allow");

    const denied = canUse("Write", { file_path: "/x" });
    await new Promise((r) => setTimeout(r, 0));
    const frame2 = s.framesSince(-1).filter((f) => f.type === "approval").pop() as Extract<WireFrame, { type: "approval" }>;
    s.answer(frame2.id, { allow: false });
    const d = await denied;
    expect(d.behavior).toBe("deny");
    expect(d.message).toMatch(/denied/);
  });

  it("reads never gate, even in ask mode", async () => {
    const { options } = buildQueryArgs(cfg({ permission: "ask" }), new HarnessSession({}), []);
    const canUse = options.canUseTool as (t: string, i: unknown) => Promise<{ behavior: string }>;
    expect((await canUse("Read", { file_path: "/x" })).behavior).toBe("allow");
  });

  it("maxTurns rides through when configured", () => {
    expect(buildQueryArgs(cfg({ maxTurns: 12 }), new HarnessSession({}), []).options.maxTurns).toBe(12);
    expect(buildQueryArgs(cfg(), new HarnessSession({}), []).options.maxTurns).toBeUndefined();
  });

  it("caller-lent mcpServers become HTTP MCP servers (never pre-approved)", () => {
    const lent = [
      { name: "desktop", url: "http://127.0.0.1:4820/mcp", headers: { authorization: "Bearer x" } },
      { name: "org-tools", url: "https://tools.glyphh.app/mcp" },
    ];
    const { options } = buildQueryArgs(cfg({ mcpServers: lent }), new HarnessSession({}), []);
    const servers = options.mcpServers as Record<string, { type: string; url?: string; headers?: Record<string, string> }>;
    expect(servers.glyphh).toMatchObject({ type: "sdk" }); // ask_user survives alongside
    expect(servers.desktop).toEqual({ type: "http", url: "http://127.0.0.1:4820/mcp", headers: { authorization: "Bearer x" } });
    expect(servers["org-tools"]).toEqual({ type: "http", url: "https://tools.glyphh.app/mcp" });
    // Lent tools are NOT pre-approved: they route through the gate like the
    // built-ins (their own host may gate them too, but the pod never waives).
    expect(options.allowedTools).toBeUndefined();
    // chat stays tool-less even with servers lent.
    expect(buildQueryArgs(cfg({ mode: "chat", mcpServers: lent }), new HarnessSession({}), []).options.mcpServers).toBeUndefined();
  });
});

/**
 * REGRESSION: the permission gate must actually gate. These fail against the
 * pre-approving build, where `allowedTools` listed the whole toolset — the SDK
 * treats that list as "auto-allow without prompting", so `canUseTool` was
 * never consulted: plan mode wrote files and ask mode never asked.
 */
describe("buildQueryArgs — the gate is the decision point (never pre-approved)", () => {
  const mutating = ["Write", "Edit", "NotebookEdit", "Bash", "KillShell"];

  it("NO tool is pre-approved — allowedTools is unset in every mode", () => {
    for (const permission of ["ask", "plan", "acceptEdits", "auto", "bypass"] as const) {
      const { options } = buildQueryArgs(cfg({ permission }), new HarnessSession({}), []);
      expect(options.allowedTools).toBeUndefined();
      // Availability is `tools`; it must never double as an allowance.
      expect(options.tools).toEqual(SANDBOX_TOOLS);
    }
  });

  it("the mutating tools are available but not waived, and canUseTool is wired", () => {
    const { options } = buildQueryArgs(cfg(), new HarnessSession({}), []);
    for (const t of mutating) expect(options.tools as string[]).toContain(t);
    expect(typeof options.canUseTool).toBe("function");
    expect(options.permissionMode).toBe("default"); // the mode that consults canUseTool
  });

  it("plan mode REFUSES every mutating tool through the gate", async () => {
    const { options } = buildQueryArgs(cfg({ permission: "plan" }), new HarnessSession({}), []);
    const canUse = options.canUseTool as (t: string, i: unknown) => Promise<{ behavior: string; message?: string }>;
    for (const tool of mutating) {
      const d = await canUse(tool, { file_path: "/tmp/x", command: "echo hi > /tmp/x" });
      expect(d.behavior).toBe("deny");
      expect(d.message).toMatch(/read-only/);
    }
    // Reads stay free even in plan mode — planning needs to look around.
    expect((await canUse("Read", { file_path: "/tmp/x" })).behavior).toBe("allow");
  });

  it("ask mode emits an approval frame for BOTH Bash and Write, and parks until answered", async () => {
    for (const [tool, input, kind] of [
      ["Bash", { command: "npm run build" }, "command"],
      ["Write", { file_path: "/tmp/out.txt" }, "edit"],
    ] as const) {
      const s = new HarnessSession({});
      const { options } = buildQueryArgs(cfg({ permission: "ask" }), s, [], { approvalTimeoutMs: 5000 });
      const canUse = options.canUseTool as (t: string, i: unknown) => Promise<{ behavior: string; message?: string }>;

      const pending = canUse(tool, input);
      await new Promise((r) => setTimeout(r, 0));
      const frame = s.framesSince(-1).find((f) => f.type === "approval") as Extract<WireFrame, { type: "approval" }>;
      expect(frame, `${tool} must raise an approval frame`).toBeDefined();
      expect(frame.kind).toBe(kind);

      // Deny → the model sees a refusal it can re-plan around.
      s.answer(frame.id, { allow: false });
      const denied = await pending;
      expect(denied.behavior).toBe("deny");

      // Allow → the same tool proceeds.
      const second = canUse(tool, input);
      await new Promise((r) => setTimeout(r, 0));
      const frame2 = s.framesSince(-1).filter((f) => f.type === "approval").pop() as Extract<WireFrame, { type: "approval" }>;
      s.answer(frame2.id, { allow: true });
      expect((await second).behavior).toBe("allow");
    }
  });

  it("auto mode runs clean (no approval frames) but still routes through the gate", async () => {
    const s = new HarnessSession({});
    const { options } = buildQueryArgs(cfg({ permission: "auto" }), s, []);
    const canUse = options.canUseTool as (t: string, i: unknown) => Promise<{ behavior: string }>;
    expect((await canUse("Write", { file_path: "/tmp/x" })).behavior).toBe("allow");
    expect((await canUse("Bash", { command: "npm test" })).behavior).toBe("allow");
    expect(s.framesSince(-1).filter((f) => f.type === "approval")).toHaveLength(0);
    // …and a DANGEROUS command still stops to ask, even on auto.
    const pending = canUse("Bash", { command: "sudo rm -rf /" });
    await new Promise((r) => setTimeout(r, 0));
    const frame = s.framesSince(-1).find((f) => f.type === "approval") as Extract<WireFrame, { type: "approval" }>;
    expect(frame).toMatchObject({ kind: "dangerous" });
    s.answer(frame.id, { allow: false });
    expect((await pending).behavior).toBe("deny");
  });

  it("a lent MCP tool is gated too — plan refuses it, ask asks", async () => {
    const lent = [{ name: "desktop", url: "http://127.0.0.1:4820/mcp" }];
    const planned = buildQueryArgs(cfg({ permission: "plan", mcpServers: lent }), new HarnessSession({}), []);
    const planCanUse = planned.options.canUseTool as (t: string, i: unknown) => Promise<{ behavior: string }>;
    expect((await planCanUse("mcp__desktop__write_file", { path: "/x" })).behavior).toBe("deny");

    const s = new HarnessSession({});
    const asked = buildQueryArgs(cfg({ permission: "ask", mcpServers: lent }), s, [], { approvalTimeoutMs: 5000 });
    const askCanUse = asked.options.canUseTool as (t: string, i: unknown) => Promise<{ behavior: string }>;
    const pending = askCanUse("mcp__desktop__write_file", { path: "/x" });
    await new Promise((r) => setTimeout(r, 0));
    const frame = s.framesSince(-1).find((f) => f.type === "approval") as Extract<WireFrame, { type: "approval" }>;
    expect(frame).toBeDefined();
    s.answer(frame.id, { allow: true });
    expect((await pending).behavior).toBe("allow");
  });

  it("ask_user is never gated — the Ask channel must not need approval to ask", async () => {
    const s = new HarnessSession({});
    const { options } = buildQueryArgs(cfg({ permission: "ask" }), s, []);
    const canUse = options.canUseTool as (t: string, i: unknown) => Promise<{ behavior: string }>;
    expect((await canUse("mcp__glyphh__ask_user", { questions: [] })).behavior).toBe("allow");
    expect(s.framesSince(-1).filter((f) => f.type === "approval")).toHaveLength(0);
  });
});

/**
 * The gate's decision must actually bind the tool. The SDK is faked with a
 * generator that HONORS canUseTool the way the real subprocess does — allow →
 * perform the write, deny → report the refusal — so a mode that must not write
 * provably leaves the disk untouched.
 */
describe("runHarness — a gated run cannot write in plan mode", () => {
  const attempt = (file: string): QueryFn => (args) =>
    (async function* () {
      const canUse = (args.options as { canUseTool: (t: string, i: unknown) => Promise<{ behavior: string; message?: string }> }).canUseTool;
      const decision = await canUse("Write", { file_path: file, content: "written" });
      if (decision.behavior === "allow") {
        writeFileSync(file, "written");
        yield { type: "assistant", message: { content: [{ type: "text", text: "wrote it" }] } };
      } else {
        yield { type: "assistant", message: { content: [{ type: "text", text: `refused: ${decision.message ?? ""}` }] } };
      }
      yield { type: "result", subtype: "success", result: "" };
    })();

  it("plan mode: the file is NOT created; auto mode: it is", async () => {
    const dir = mkdtempSync(join(tmpdir(), "gate-"));
    const planned = join(dir, "plan.txt");
    const s1 = new HarnessSession({});
    await runHarness(s1, cfg({ permission: "plan" }), { queryFn: attempt(planned) });
    expect(existsSync(planned)).toBe(false);
    expect(frames(s1).some((f) => f.type === "delta" && /refused/.test(String((f as { delta?: string }).delta)))).toBe(true);

    const allowed = join(dir, "auto.txt");
    const s2 = new HarnessSession({});
    await runHarness(s2, cfg({ permission: "auto" }), { queryFn: attempt(allowed) });
    expect(existsSync(allowed)).toBe(true);
  });
});

/**
 * The turn's metered price. The pod has no interceptor, so the engine reads it
 * back from the gateway's per-run usage endpoint at turn end. It is advisory:
 * a turn must never be delayed, failed, or lost because pricing was slow.
 */
describe("runHarness — the credits frame", () => {
  const finished: QueryFn = stream(
    { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "hi" } } },
    { type: "result", subtype: "success", result: "hi", usage: { input_tokens: 10, output_tokens: 2 } },
  );
  const jsonRes = (status: number, body: unknown): Response =>
    ({ ok: status >= 200 && status < 300, status, json: async () => body }) as unknown as Response;

  it("emits credits from a 200 — correct URL + bearer, ordered BEFORE done", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    const fetchFn = (async (url: unknown, init: unknown) => {
      calls.push([String(url), init as RequestInit]);
      return jsonRes(200, { data: { runId: "run-t", requests: 2, creditsMicro: 12_345, inputTokens: 10, outputTokens: 2 } });
    }) as unknown as typeof fetch;

    const s = new HarnessSession({ runId: "run-t" });
    await runHarness(s, cfg({ runId: "run-t" }), { queryFn: finished, fetchFn });

    expect(calls).toHaveLength(1);
    expect(calls[0][0]).toBe("https://gw.test/runs/run-t/usage");
    expect((calls[0][1]?.headers as Record<string, string>).authorization).toBe("Bearer gy_rt_engine_secret");

    const credits = frames(s).find((f) => f.type === "credits") as Extract<WireFrame, { type: "credits" }>;
    expect(credits.creditsMicro).toBe(12_345);
    // credits → price, done → turn end: the desktop consumes them in that order.
    const seq = types(s);
    expect(seq.indexOf("credits")).toBeLessThan(seq.indexOf("done"));
    expect(seq[seq.length - 1]).toBe("done");
  });

  it("stays silent — and still finishes the turn — on 404, 401, and a dead socket", async () => {
    for (const responder of [
      async () => jsonRes(404, { error: "not-found" }),
      async () => jsonRes(401, { error: "unauthorized" }),
      async () => {
        throw new Error("ECONNREFUSED");
      },
    ]) {
      const s = new HarnessSession({});
      await runHarness(s, cfg(), { queryFn: finished, fetchFn: responder as unknown as typeof fetch });
      expect(types(s)).not.toContain("credits");
      expect(types(s)[types(s).length - 1]).toBe("done"); // the turn still ends cleanly
    }
  });

  it("a hung gateway cannot hold the turn open — it times out and done still lands", async () => {
    // Never resolves on its own; only the probe's abort signal ends it.
    const hung = ((_url: unknown, init: unknown) =>
      new Promise((_resolve, reject) => {
        const signal = (init as { signal?: AbortSignal }).signal;
        signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
      })) as unknown as typeof fetch;

    const s = new HarnessSession({});
    const started = Date.now();
    await runHarness(s, cfg(), { queryFn: finished, fetchFn: hung, creditsTimeoutMs: 30 });
    expect(Date.now() - started).toBeLessThan(2000); // bounded by the probe timeout, not the socket
    expect(types(s)).not.toContain("credits");
    expect(types(s)[types(s).length - 1]).toBe("done");
  });

  it("never looks up a price without a gateway or a token", async () => {
    for (const over of [{ gatewayUrl: "" }, { runtimeToken: "" }]) {
      let called = false;
      const fetchFn = (async () => {
        called = true;
        return jsonRes(200, { data: { creditsMicro: 1 } });
      }) as unknown as typeof fetch;
      const s = new HarnessSession({});
      await runHarness(s, cfg(over), { queryFn: finished, fetchFn });
      expect(called).toBe(false);
      expect(types(s)).not.toContain("credits");
    }
  });

  it("ignores a malformed payload, and an aborted run never asks at all", async () => {
    const s1 = new HarnessSession({});
    const junk = (async () => jsonRes(200, { data: { creditsMicro: "free" } })) as unknown as typeof fetch;
    await runHarness(s1, cfg(), { queryFn: finished, fetchFn: junk });
    expect(types(s1)).not.toContain("credits");

    // A stopped run ends immediately — no price lookup stands between the user
    // pressing stop and the done frame.
    let called = false;
    const fetchFn = (async () => {
      called = true;
      return jsonRes(200, { data: { creditsMicro: 5 } });
    }) as unknown as typeof fetch;
    const s2 = new HarnessSession({});
    const parked: QueryFn = (args) =>
      (async function* () {
        const ctrl = (args.options as { abortController: AbortController }).abortController;
        yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "…" } } };
        await new Promise<void>((r) => ctrl.signal.addEventListener("abort", () => r(), { once: true }));
      })();
    const run = runHarness(s2, cfg(), { queryFn: parked, fetchFn });
    await new Promise((r) => setTimeout(r, 10));
    s2.stop();
    await run;
    expect(called).toBe(false);
    expect(frames(s2)[frames(s2).length - 1]).toMatchObject({ type: "done", stopped: true });
  });
});

describe("buildPrompt — images ride as vision blocks", () => {
  const png = { mediaType: "image/png", data: "iVBORw0KGgo=" };

  it("a text-only turn stays a plain string prompt (chat); tool modes take the injectable stream", async () => {
    expect(buildPrompt("just words", [])).toBe("just words");
    // CHAT keeps the single-shot string — one turn, nothing to steer.
    expect(buildQueryArgs(cfg({ mode: "chat" }), new HarnessSession({}), []).prompt).toBe("do the thing");
    // Tool-bearing modes stream, so mid-run injections can start new turns.
    const session = new HarnessSession({});
    const prompt = buildQueryArgs(cfg(), session, []).prompt as AsyncIterable<{ message: { content: Array<{ type: string; text?: string }> } }>;
    const it = prompt[Symbol.asyncIterator]();
    const first = await it.next();
    expect((first.value as { message: { content: Array<{ type: string; text?: string }> } }).message.content).toEqual([{ type: "text", text: "do the thing" }]);
    session.closeInput();   // empty inbox → the stream ends, same as single-shot
    expect((await it.next()).done).toBe(true);
  });

  it("an injected message rides the stream as the next user turn (and its frame hits the tape)", async () => {
    const session = new HarnessSession({});
    const prompt = buildQueryArgs(cfg(), session, []).prompt as AsyncIterable<{ message: { content: Array<{ type: string; text?: string }> } }>;
    const it = prompt[Symbol.asyncIterator]();
    await it.next();   // the opening prompt
    expect(session.inject("also check the tests")).toBe(true);
    const second = await it.next();
    expect((second.value as { message: { content: Array<{ type: string; text?: string }> } }).message.content).toEqual([{ type: "text", text: "also check the tests" }]);
    expect(frames(session).some((f) => f.type === "prompt" && (f as { text?: string }).text === "also check the tests")).toBe(true);
    session.closeInput();
    expect((await it.next()).done).toBe(true);
    // Closed input takes no more messages.
    expect(session.inject("too late")).toBe(false);
  });

  it("an image turn becomes the SDK's streaming-input form, images before the text", async () => {
    const prompt = buildPrompt("what is this?", [png, { mediaType: "image/jpeg", data: "/9j/4AAQ" }]);
    expect(typeof prompt).not.toBe("string");
    const msgs = [];
    for await (const m of prompt as AsyncIterable<Record<string, unknown>>) msgs.push(m);
    expect(msgs).toHaveLength(1); // one user message, then the turn closes
    const msg = msgs[0] as { type: string; parent_tool_use_id: null; message: { role: string; content: Array<Record<string, unknown>> } };
    expect(msg.type).toBe("user");
    expect(msg.parent_tool_use_id).toBeNull();
    expect(msg.message.role).toBe("user");
    expect(msg.message.content).toEqual([
      { type: "image", source: { type: "base64", media_type: "image/png", data: "iVBORw0KGgo=" } },
      { type: "image", source: { type: "base64", media_type: "image/jpeg", data: "/9j/4AAQ" } },
      { type: "text", text: "what is this?" },
    ]);
  });

  it("buildQueryArgs uses the block form when the run carries images", async () => {
    const session = new HarnessSession({});
    const { prompt } = buildQueryArgs(cfg({ images: [png] }), session, []);
    expect(typeof prompt).not.toBe("string");
    session.closeInput();   // no injections in this test — let the stream end
    for await (const m of prompt as AsyncIterable<{ message: { content: Array<{ type: string; text?: string }> } }>) {
      // The assembled text (history + ask + manifest) is the trailing block.
      expect(m.message.content[0].type).toBe("image");
      expect(m.message.content[1]).toEqual({ type: "text", text: "do the thing" });
    }
  });
});

describe("assemblePrompt", () => {
  it("carries recent history + attachment manifest around the ask", () => {
    const text = assemblePrompt(
      [
        { role: "user", content: "hi" },
        { role: "assistant", content: "hello" },
      ],
      "now build it",
      [{ name: "spec.md", path: "/w/attachments/spec.md", bytes: 9 }],
    );
    expect(text).toContain("<conversation_so_far>\nUser: hi\nGlyphh: hello\n</conversation_so_far>");
    expect(text).toContain("now build it");
    expect(text).toContain("<attached_files>\n/w/attachments/spec.md (9 bytes)\n</attached_files>");
    expect(assemblePrompt([], "solo", [])).toBe("solo");
  });
});

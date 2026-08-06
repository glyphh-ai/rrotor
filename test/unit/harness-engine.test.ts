/**
 * The engine's frame translation, with the Claude Agent SDK faked: an
 * injected queryFn yields SDK-shaped messages and the engine must emit the
 * desktop-parity frame stream (delta/tool/progress/done|error), honor abort,
 * wire the permission gate into canUseTool, and keep secrets out of frames.
 */

import { describe, it, expect } from "vitest";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { runHarness, buildQueryArgs, assemblePrompt, SANDBOX_TOOLS } from "../../src/harness/engine.js";
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
    expect(options.allowedTools).toEqual([...SANDBOX_TOOLS, "mcp__glyphh__ask_user"]);
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

  it("caller-lent mcpServers become HTTP MCP servers, allowed wholesale as mcp__<name>", () => {
    const lent = [
      { name: "desktop", url: "http://127.0.0.1:4820/mcp", headers: { authorization: "Bearer x" } },
      { name: "org-tools", url: "https://tools.glyphh.app/mcp" },
    ];
    const { options } = buildQueryArgs(cfg({ mcpServers: lent }), new HarnessSession({}), []);
    const servers = options.mcpServers as Record<string, { type: string; url?: string; headers?: Record<string, string> }>;
    expect(servers.glyphh).toMatchObject({ type: "sdk" }); // ask_user survives alongside
    expect(servers.desktop).toEqual({ type: "http", url: "http://127.0.0.1:4820/mcp", headers: { authorization: "Bearer x" } });
    expect(servers["org-tools"]).toEqual({ type: "http", url: "https://tools.glyphh.app/mcp" });
    expect(options.allowedTools).toEqual([...SANDBOX_TOOLS, "mcp__glyphh__ask_user", "mcp__desktop", "mcp__org-tools"]);
    // chat stays tool-less even with servers lent.
    expect(buildQueryArgs(cfg({ mode: "chat", mcpServers: lent }), new HarnessSession({}), []).options.mcpServers).toBeUndefined();
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

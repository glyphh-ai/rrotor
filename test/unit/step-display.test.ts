/**
 * Step display metadata (author-controlled event text). A step's `display`
 * block is copied onto its StepRecord and streamed verbatim on the step wire
 * event, so clients render the author's words instead of raw step ids.
 */

import { describe, it, expect } from "vitest";

import { runInProcess } from "../../src/embed.js";

const ROTOR = `
apiVersion: rotor.glyphh.ai/v0.1
kind: Rotor
metadata: { name: display-demo, version: 1.0.0, namespace: test }
spec:
  inputs:
    - { name: prompt, type: string, required: true }
  entry: compose
  steps:
    - id: compose
      type: prompt
      display: { label: "composing the prompt", detail: "system block + user turn" }
      in: { question: $.inputs.prompt }
      out: { text: string }
      config:
        blocks:
          - { name: user, text: "{{question}}" }
      next: end
  outputs:
    - { name: answer, from: $.steps.compose.text }
`;

describe("step display metadata", () => {
  it("streams the author's label/detail on the step wire event", async () => {
    const steps: Array<Record<string, unknown>> = [];
    await runInProcess(ROTOR, { prompt: "hi" }, (ev) => {
      if (ev.kind === "step") steps.push(ev as unknown as Record<string, unknown>);
    });
    expect(steps).toHaveLength(1);
    expect(steps[0].display).toEqual({ label: "composing the prompt", detail: "system block + user turn" });
  });

  it("is absent when the author declared none", async () => {
    const bare = ROTOR.replace(/^\s*display:.*\n/m, "");
    const steps: Array<Record<string, unknown>> = [];
    await runInProcess(bare, { prompt: "hi" }, (ev) => {
      if (ev.kind === "step") steps.push(ev as unknown as Record<string, unknown>);
    });
    expect(steps[0].display).toBeUndefined();
  });
});

describe("transform parse extraction", () => {
  const ROTOR = `
apiVersion: rotor.glyphh.ai/v0.1
kind: Rotor
metadata: { name: parse-demo, version: 1.0.0, namespace: test }
spec:
  inputs:
    - { name: text, type: string, required: true }
  entry: extract
  steps:
    - id: extract
      type: transform
      in: { text: $.inputs.text }
      out: { file: string, test_cmd: string }
      config:
        parse:
          file: '^FILE:\\s*(.+)$'
          test_cmd: '^TEST:\\s*(.+)$'
      next: end
  outputs:
    - { name: file, from: $.steps.extract.file }
    - { name: test_cmd, from: $.steps.extract.test_cmd }
`;

  it("lifts fields out of planner-style text deterministically", async () => {
    let outputs: Record<string, unknown> = {};
    await runInProcess(ROTOR, { text: "FILE: greet.js\nTEST: node greet.js\nPLAN:\n1. write it" }, (ev) => {
      if (ev.kind === "done") outputs = ev.outputs as Record<string, unknown>;
    });
    expect(outputs).toEqual({ file: "greet.js", test_cmd: "node greet.js" });
  });

  it("unmatched fields extract as null (gate-refusable), never invented", async () => {
    let outputs: Record<string, unknown> = {};
    await runInProcess(ROTOR, { text: "no header at all" }, (ev) => {
      if (ev.kind === "done") outputs = ev.outputs as Record<string, unknown>;
    });
    expect(outputs).toEqual({ file: null, test_cmd: null });
  });
});

describe("the conversation channel (write turn + retrieve recent)", () => {
  const ROTOR = `
apiVersion: rotor.glyphh.ai/v0.1
kind: Rotor
metadata: { name: conv-demo, version: 1.0.0, namespace: test }
spec:
  inputs:
    - { name: prompt, type: string, required: true }
  space: { vector_dim: 10000, encoder_seed: 42, roles_config: universal-7x33 }
  entry: log
  steps:
    - id: log
      type: write
      in: { text: $.inputs.prompt }
      out: { logged: boolean }
      config: { mode: turn, speaker: user }
      next: recent
    - id: recent
      type: retrieve.vector
      in: { query: $.inputs.prompt }
      out: { transcript: string, count: number }
      config: { kind: recent, window: 4 }
      next: end
  outputs:
    - { name: transcript, from: $.steps.recent.transcript }
    - { name: count, from: $.steps.recent.count }
`;

  it("turns append to the session window and read back in order, speaker-tagged", async () => {
    const { initStator } = await import("../../src/exec/stator.js");
    const store = await initStator({ backend: "memory" });
    let outputs: { transcript?: string; count?: number } = {};
    await runInProcess(ROTOR, { prompt: "when did ww2 end?" }, () => {}, { store, session: "s1" });
    await runInProcess(ROTOR, { prompt: "how do you know?" }, (ev) => {
      if (ev.kind === "done") outputs = ev.outputs as typeof outputs;
    }, { store, session: "s1" });
    expect(outputs.count).toBe(2);
    expect(outputs.transcript).toBe("user: when did ww2 end?\nuser: how do you know?");
  });

  it("the window is session-scoped — another session sees nothing", async () => {
    const { initStator } = await import("../../src/exec/stator.js");
    const store = await initStator({ backend: "memory" });
    await runInProcess(ROTOR, { prompt: "secret plans" }, () => {}, { store, session: "a" });
    let outputs: { count?: number } = {};
    await runInProcess(ROTOR, { prompt: "hello" }, (ev) => {
      if (ev.kind === "done") outputs = ev.outputs as typeof outputs;
    }, { store, session: "b" });
    expect(outputs.count).toBe(1);
  });

  it("composed prompts NEVER enter the turn corpus (the pollution cut)", async () => {
    const { initStator } = await import("../../src/exec/stator.js");
    const store = await initStator({ backend: "memory" });
    const PROMPT_ROTOR = `
apiVersion: rotor.glyphh.ai/v0.1
kind: Rotor
metadata: { name: pollute-demo, version: 1.0.0, namespace: test }
spec:
  inputs:
    - { name: prompt, type: string, required: true }
  space: { vector_dim: 10000, encoder_seed: 42, roles_config: universal-7x33 }
  entry: compose
  steps:
    - id: compose
      type: prompt
      in: { question: $.inputs.prompt }
      out: { text: string }
      config:
        blocks:
          - { name: system, text: "SCAFFOLDING INSTRUCTIONS" }
          - { name: user, text: "{{question}}" }
      next: probe
    - id: probe
      type: retrieve.vector
      in: { query: $.inputs.prompt }
      out: { hits: array }
      config: { top_k: 4, kind: query, threshold: 0.05 }
      next: end
  outputs:
    - { name: hits, from: $.steps.probe.hits }
`;
    let outputs: { hits?: Array<{ text: string }> } = {};
    await runInProcess(PROMPT_ROTOR, { prompt: "SCAFFOLDING INSTRUCTIONS" }, (ev) => {
      if (ev.kind === "done") outputs = ev.outputs as typeof outputs;
    }, { store, session: "s1" });
    expect(outputs.hits).toEqual([]);
  });
});

describe("transform strip: fences", () => {
  const ROTOR = `
apiVersion: rotor.glyphh.ai/v0.1
kind: Rotor
metadata: { name: strip-demo, version: 1.0.0, namespace: test }
spec:
  inputs:
    - { name: text, type: string, required: true }
  entry: clean
  steps:
    - id: clean
      type: transform
      in: { text: $.inputs.text }
      out: { text: string }
      config: { strip: fences }
      next: end
  outputs:
    - { name: text, from: $.steps.clean.text }
`;

  it("removes a leading fence line and trailing fence", async () => {
    let out: { text?: string } = {};
    await runInProcess(ROTOR, { text: "```python\nprint('hi')\n```\n" }, (ev) => {
      if (ev.kind === "done") out = ev.outputs as typeof out;
    });
    expect(out.text).toBe("print('hi')");
  });

  it("leaves unfenced code untouched", async () => {
    let out: { text?: string } = {};
    await runInProcess(ROTOR, { text: "print('hi')" }, (ev) => {
      if (ev.kind === "done") out = ev.outputs as typeof out;
    });
    expect(out.text).toBe("print('hi')");
  });
});

describe("provider wire adapters", () => {
  it("anthropic: Messages API request + response shapes", async () => {
    const { buildCall, parseResponse, providerForUrl, authHeaders } = await import("../../src/plugins/providers.js");
    expect(providerForUrl("https://api.anthropic.com")).toBe("anthropic");
    expect(providerForUrl("http://127.0.0.1:8080")).toBe("openai");
    const call = buildCall("anthropic", "https://api.anthropic.com", { prompt: "hi", model: "claude-sonnet-4-5" }, authHeaders("anthropic", "sk-ant-x"));
    expect(call.url).toBe("https://api.anthropic.com/v1/messages");
    expect(call.headers["x-api-key"]).toBe("sk-ant-x");
    expect(call.headers["anthropic-version"]).toBeTruthy();
    expect((call.body as { messages: unknown[] }).messages).toHaveLength(1);
    expect((call.body as { temperature?: number }).temperature).toBeUndefined();
    const parsed = parseResponse("anthropic", {
      content: [{ type: "text", text: "hello " }, { type: "text", text: "world" }],
      usage: { input_tokens: 12, output_tokens: 4 },
    });
    expect(parsed).toEqual({ text: "hello world", inputTokens: 12, outputTokens: 4 });
  });

  it("openai: chat-completions request + response shapes", async () => {
    const { buildCall, parseResponse } = await import("../../src/plugins/providers.js");
    const call = buildCall("openai", "http://127.0.0.1:8080/", { prompt: "hi", model: "glyphh-local", seed: 7 }, { authorization: "Bearer k" });
    expect(call.url).toBe("http://127.0.0.1:8080/v1/chat/completions");
    expect((call.body as { seed?: number }).seed).toBe(7);
    const parsed = parseResponse("openai", { choices: [{ message: { content: "yo" } }], usage: { prompt_tokens: 3, completion_tokens: 1 } });
    expect(parsed).toEqual({ text: "yo", inputTokens: 3, outputTokens: 1 });
  });
});

describe(".env loading", () => {
  it("parses KEY=VALUE with comments/quotes and never overrides real env", async () => {
    const { mkdtempSync, writeFileSync } = await import("node:fs");
    const { tmpdir } = await import("node:os");
    const { join } = await import("node:path");
    const dir = mkdtempSync(join(tmpdir(), "rrotor-env-"));
    writeFileSync(join(dir, ".env"), '# comment\r\nTEST_ENV_A="quoted value"\r\nexport TEST_ENV_B=plain\nTEST_ENV_C=should-not-win\n');
    process.env.TEST_ENV_C = "already-set";
    const prevCwd = process.cwd();
    process.chdir(dir);
    try {
      await import("../../src/env.js");
      expect(process.env.TEST_ENV_A).toBe("quoted value");
      expect(process.env.TEST_ENV_B).toBe("plain");
      expect(process.env.TEST_ENV_C).toBe("already-set");
    } finally {
      process.chdir(prevCwd);
      delete process.env.TEST_ENV_A;
      delete process.env.TEST_ENV_B;
      delete process.env.TEST_ENV_C;
    }
  });
});


describe("lane degradation is visible", () => {
  it("a dead frontier leaves notes + a degrade frame and falls to the stub", async () => {
    const { BasicModels } = await import("../../src/plugins/models.js");
    const models = new BasicModels({ frontierUrl: "http://127.0.0.1:1", timeoutMs: 2000 });
    const res = await models.execute({ prompt: "hi", lane: "frontier" }, "frontier");
    expect(res.text).toContain("[stub:");
    expect(res.notes?.length).toBeGreaterThan(0);
    expect(res.notes?.[0]).toContain("frontier http://127.0.0.1:1");
    expect(res.frames.map((f) => f.type)).toContain("degrade");
  });
});

/**
 * Harness config resolution + the two load-bearing env invariants:
 * CLAUDE_CONFIG_DIR isolation (the subprocess NEVER rides a personal
 * ~/.claude) and gateway-only model routing (base URL, run tag header,
 * runtime-token auth). Plus secret redaction — tokens never reach a log line.
 */

import { describe, it, expect } from "vitest";
import { mkdtempSync, existsSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join, sep } from "node:path";

import {
  resolveRunConfig,
  buildAgentEnv,
  redactSecrets,
  brandError,
  sanitizeSegment,
  harnessHome,
  parseMcpServers,
  BadRunRequest,
} from "../../src/harness/config.js";

const HOME = mkdtempSync(join(tmpdir(), "harness-test-"));

const ENV = {
  GLYPHH_GATEWAY_URL: "https://gw.glyphh.test/v1/",
  GLYPHH_RUNTIME_TOKEN: "gy_rt_secret_token_123",
  ROTOR_SESSION_ID: "sess-9",
  HARNESS_HOME: HOME,
} as NodeJS.ProcessEnv;

describe("resolveRunConfig", () => {
  it("merges env + body, body wins", () => {
    const cfg = resolveRunConfig("run-1", { prompt: "hi", model: "claude-sonnet-5", mode: "cowork", permission: "ask" }, ENV);
    expect(cfg.gatewayUrl).toBe("https://gw.glyphh.test/v1"); // trailing slash trimmed
    expect(cfg.runtimeToken).toBe("gy_rt_secret_token_123");
    expect(cfg.sessionId).toBe("sess-9");
    expect(cfg.model).toBe("claude-sonnet-5");
    expect(cfg.mode).toBe("cowork");
    expect(cfg.permission).toBe("ask");
    const bodyWins = resolveRunConfig("run-2", { prompt: "hi", gatewayUrl: "https://other.test", runtimeToken: "tok2", sessionId: "sess-b" }, ENV);
    expect(bodyWins.gatewayUrl).toBe("https://other.test");
    expect(bodyWins.runtimeToken).toBe("tok2");
    expect(bodyWins.sessionId).toBe("sess-b");
  });

  it("sandboxes per SESSION under the harness home (workdir + config dir)", () => {
    const cfg = resolveRunConfig("run-1", { prompt: "hi" }, ENV);
    expect(cfg.workdir).toBe(join(HOME, "sessions", "sess-9", "workspace"));
    expect(cfg.configDir).toBe(join(HOME, "sessions", "sess-9", "agent-config"));
    // No session id → still isolated, keyed by run.
    const solo = resolveRunConfig("run-x", { prompt: "hi" }, { ...ENV, ROTOR_SESSION_ID: undefined });
    expect(solo.workdir).toBe(join(HOME, "sessions", "run-x", "workspace"));
  });

  it("requires prompt, gateway, and token", () => {
    expect(() => resolveRunConfig("r", {}, ENV)).toThrow(BadRunRequest);
    expect(() => resolveRunConfig("r", { prompt: "hi" }, { ...ENV, GLYPHH_GATEWAY_URL: undefined })).toThrow(/gateway/);
    expect(() => resolveRunConfig("r", { prompt: "hi" }, { ...ENV, GLYPHH_RUNTIME_TOKEN: undefined })).toThrow(/runtime token/);
  });

  it("filters malformed history and attachments; defaults mode/permission", () => {
    const cfg = resolveRunConfig(
      "r",
      {
        prompt: "hi",
        history: [{ role: "user", content: "a" }, { role: "tool", content: "x" }, "junk", { role: "assistant", content: "b" }],
        attachments: [{ name: "a.txt", url: "https://files.test/a" }, { name: "bad", url: "ftp://x" }, { url: "https://x" }],
        mode: "weird",
        permission: "sudo",
      },
      ENV,
    );
    expect(cfg.history).toEqual([
      { role: "user", content: "a" },
      { role: "assistant", content: "b" },
    ]);
    expect(cfg.attachments).toEqual([{ name: "a.txt", url: "https://files.test/a" }]);
    expect(cfg.mode).toBe("code");
    expect(cfg.permission).toBe("auto");
  });

  it("sanitizes hostile session ids out of the sandbox path", () => {
    const cfg = resolveRunConfig("r", { prompt: "hi", sessionId: "../../etc/passwd" }, ENV);
    expect(cfg.workdir.startsWith(join(HOME, "sessions") + sep)).toBe(true);
    expect(cfg.workdir).not.toContain("..");
    expect(sanitizeSegment("../../x")).not.toContain("/");
    expect(sanitizeSegment("")).toBe("_");
  });
});

describe("parseMcpServers — the caller-lent tool surface", () => {
  it("accepts https anywhere and http on loopback only; headers keep string values", () => {
    const refs = parseMcpServers([
      { name: "desktop", url: "http://127.0.0.1:4820/mcp", headers: { authorization: "Bearer x", junk: 42 } },
      { name: "org-tools", url: "https://tools.glyphh.app/mcp" },
      { name: "local2", url: "http://localhost:9000/mcp" },
    ]);
    expect(refs.map((r) => r.name)).toEqual(["desktop", "org-tools", "local2"]);
    expect(refs[0].headers).toEqual({ authorization: "Bearer x" }); // non-string dropped
    expect(refs[1].headers).toBeUndefined();
    expect(parseMcpServers(undefined)).toEqual([]);
  });

  it("rejects bad names — wrong charset, oversize, and the reserved `glyphh`", () => {
    for (const name of ["Desk Top", "UPPER", "x".repeat(33), "", "glyphh"]) {
      expect(() => parseMcpServers([{ name, url: "https://ok.example/mcp" }])).toThrow(BadRunRequest);
    }
  });

  it("rejects bad urls — unparseable, non-http schemes, and plaintext off-loopback", () => {
    for (const url of ["not a url", "ftp://x.example", "http://pod.internal:8080/mcp", "http://10.0.0.5/mcp"]) {
      expect(() => parseMcpServers([{ name: "d", url }])).toThrow(BadRunRequest);
    }
  });

  it("caps at 4 servers", () => {
    const many = Array.from({ length: 5 }, (_, i) => ({ name: `s${i}`, url: "https://ok.example/mcp" }));
    expect(() => parseMcpServers(many)).toThrow(/at most 4/);
  });

  it("rides resolveRunConfig into the run config (absent → omitted)", () => {
    const cfg = resolveRunConfig(
      "run-m",
      { prompt: "x", mcpServers: [{ name: "desktop", url: "http://127.0.0.1:4820/mcp" }] },
      ENV,
    );
    expect(cfg.mcpServers).toEqual([{ name: "desktop", url: "http://127.0.0.1:4820/mcp" }]);
    expect(resolveRunConfig("run-m2", { prompt: "x" }, ENV).mcpServers).toBeUndefined();
  });
});

describe("buildAgentEnv — the isolation + routing invariants", () => {
  const cfg = {
    runId: "run-77",
    gatewayUrl: "https://gw.glyphh.test/v1",
    runtimeToken: "gy_rt_secret_token_123",
    configDir: join(HOME, "sessions", "sess-9", "agent-config"),
  };

  it("routes the subprocess through the gateway with the run tag + runtime token", () => {
    const env = buildAgentEnv(cfg, {});
    expect(env.ANTHROPIC_BASE_URL).toBe("https://gw.glyphh.test/v1");
    expect(env.ANTHROPIC_CUSTOM_HEADERS).toBe("x-glyphh-run: run-77");
    // AUTH_TOKEN rides Authorization: Bearer — the only header the gateway
    // reads; API_KEY (x-api-key) is cleared so it can never shadow it.
    expect(env.ANTHROPIC_AUTH_TOKEN).toBe("gy_rt_secret_token_123");
    expect(env.ANTHROPIC_API_KEY).toBeUndefined();
    expect(env.CLAUDE_CODE_USE_BEDROCK).toBeUndefined();
    expect(env.CLAUDE_CODE_USE_VERTEX).toBeUndefined();
    expect(env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC).toBe("1");
    expect(env.DISABLE_TELEMETRY).toBe("1");
  });

  it("isolates CLAUDE_CONFIG_DIR inside the harness home — never ~/.claude", () => {
    const env = buildAgentEnv(cfg, {});
    expect(env.CLAUDE_CONFIG_DIR).toBe(cfg.configDir);
    expect(env.CLAUDE_CONFIG_DIR!.startsWith(HOME)).toBe(true);
    expect(env.CLAUDE_CONFIG_DIR!.startsWith(join(homedir(), ".claude"))).toBe(false);
    expect(existsSync(cfg.configDir)).toBe(true); // created eagerly
  });

  it("strips ambient ANTHROPIC_/CLAUDE_ vars so nothing can leak a different endpoint in", () => {
    const env = buildAgentEnv(cfg, {
      PATH: "/usr/bin",
      ANTHROPIC_BASE_URL: "https://evil.example",
      ANTHROPIC_API_KEY: "sk-personal",
      CLAUDE_CONFIG_DIR: join(homedir(), ".claude"),
      CLAUDE_CODE_USE_BEDROCK: "1",
    });
    expect(env.PATH).toBe("/usr/bin");
    expect(env.ANTHROPIC_BASE_URL).toBe("https://gw.glyphh.test/v1");
    expect(env.ANTHROPIC_AUTH_TOKEN).toBe("gy_rt_secret_token_123");
    expect(env.ANTHROPIC_API_KEY).toBeUndefined(); // the ambient sk-personal is gone
    expect(env.CLAUDE_CONFIG_DIR).toBe(cfg.configDir);
    expect(env.CLAUDE_CODE_USE_BEDROCK).toBeUndefined();
  });
});

describe("redaction + branding", () => {
  it("scrubs secrets wholesale", () => {
    expect(redactSecrets("401 from gw with token gy_rt_secret_token_123 in header", ["gy_rt_secret_token_123"])).toBe(
      "401 from gw with token ••• in header",
    );
    // Short/absent secrets never redact (avoids scrubbing e.g. "a").
    expect(redactSecrets("keep", [undefined, "ab"])).toBe("keep");
  });

  it("re-voices harness errors as Glyphh's own", () => {
    expect(brandError("Claude Code hit an error. Run /login to fix it.")).toBe("Glyphh hit an error. Sign in again.");
    expect(brandError("model claude-sonnet-5 overloaded")).toContain("claude-sonnet-5");
  });
});

describe("harnessHome", () => {
  it("defaults under tmp; HARNESS_HOME overrides", () => {
    expect(harnessHome({} as NodeJS.ProcessEnv)).toBe(join(tmpdir(), "glyphh-harness"));
    expect(harnessHome(ENV)).toBe(HOME);
  });
});

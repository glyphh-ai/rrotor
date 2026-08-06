/**
 * harness-glyphh-apps.test.ts — the control plane's app tools, lent by the pod
 * to itself.
 *
 * This is the seam that makes "build an app and publish it" work the same way
 * on the desktop and in a cloud pod. A cloud pod cannot reach the desktop's
 * loopback MCP bridge, so before this it had no publish tool at all and the
 * agent reached for `python3 -m http.server` — a URL nobody can open.
 *
 * The pod derives the surface from configuration it is ALREADY required to
 * have (gateway URL + runtime token) rather than waiting for each client to
 * pass it, because there is more than one client and every one of them would
 * have to agree forever.
 */

import { describe, it, expect } from "vitest";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { runtimeMcpUrl, appsServerRef, withPublishPolicy, APPS_SERVER_NAME, PUBLISH_POLICY } from "../../src/harness/glyphh-apps.js";
import { buildQueryArgs } from "../../src/harness/engine.js";
import { HarnessSession } from "../../src/harness/session.js";
import type { HarnessRunConfig } from "../../src/harness/config.js";

function cfg(over: Partial<HarnessRunConfig> = {}): HarnessRunConfig {
  const home = mkdtempSync(join(tmpdir(), "apps-"));
  return {
    runId: "run-t",
    sessionId: "sess-t",
    prompt: "build me an app",
    history: [],
    mode: "code",
    permission: "auto",
    gatewayUrl: "https://api.glyphh.ai/api/gateway",
    runtimeToken: "gy_rk_pod_secret",
    workdir: join(home, "workspace"),
    attachDir: join(home, "workspace"),
    configDir: join(home, "agent-config"),
    attachments: [],
    attachmentMaxBytes: 1024,
    maxTurns: 0,
    ...over,
  };
}

/** The lent servers the SDK loop would actually be given. */
function lentServers(over: Partial<HarnessRunConfig> = {}): Record<string, { type: string; url?: string; headers?: Record<string, string> }> {
  const { options } = buildQueryArgs(cfg(over), new HarnessSession({ runId: "run-t" }), []);
  return (options.mcpServers ?? {}) as Record<string, { type: string; url?: string; headers?: Record<string, string> }>;
}

describe("runtimeMcpUrl — deriving the control plane from the gateway", () => {
  it("drops the /api/gateway suffix and points at the runtime MCP route", () => {
    expect(runtimeMcpUrl("https://api.glyphh.ai/api/gateway")).toBe("https://api.glyphh.ai/api/runtime/mcp");
  });

  it("works for a dev control plane on a non-loopback LAN host", () => {
    // parseMcpServers would refuse this (plaintext, not loopback) — and should,
    // for CALLER-supplied urls. This one is derived from the pod's own config.
    expect(runtimeMcpUrl("http://192.168.1.20:3000/api/gateway")).toBe("http://192.168.1.20:3000/api/runtime/mcp");
    expect(runtimeMcpUrl("http://localhost:3000/api/gateway")).toBe("http://localhost:3000/api/runtime/mcp");
  });

  it("lets an explicit control URL win, whether it is an origin or the full route", () => {
    expect(runtimeMcpUrl("https://gw.example/api/gateway", "https://control.example"))
      .toBe("https://control.example/api/runtime/mcp");
    expect(runtimeMcpUrl("https://gw.example/api/gateway", "https://control.example/api/runtime/mcp"))
      .toBe("https://control.example/api/runtime/mcp");
  });

  it("returns null rather than a broken url when there is nothing to derive from", () => {
    expect(runtimeMcpUrl("")).toBeNull();
    expect(runtimeMcpUrl("not a url")).toBeNull();
    expect(runtimeMcpUrl("ftp://files.example/api/gateway")).toBeNull();
  });
});

describe("appsServerRef — the lent entry", () => {
  it("carries the run's OWN runtime token as the bearer", () => {
    const ref = appsServerRef({ gatewayUrl: "https://api.glyphh.ai/api/gateway", runtimeToken: "gy_rk_abc" });
    expect(ref).toEqual({
      name: APPS_SERVER_NAME,
      url: "https://api.glyphh.ai/api/runtime/mcp",
      headers: { authorization: "Bearer gy_rk_abc" },
    });
  });

  it("is null with no credential — an unauthenticated pod lends nothing", () => {
    expect(appsServerRef({ gatewayUrl: "https://api.glyphh.ai/api/gateway", runtimeToken: "" })).toBeNull();
  });

  it("uses a name the harness's own validator accepts, and never `glyphh`", () => {
    expect(APPS_SERVER_NAME).toMatch(/^[a-z0-9_-]{1,32}$/);
    expect(APPS_SERVER_NAME).not.toBe("glyphh");
  });
});

describe("the engine wires it into every tool-bearing run", () => {
  it("lends the app tools on a CLOUD run, where nothing else can", () => {
    const servers = lentServers();
    expect(servers[APPS_SERVER_NAME]).toEqual({
      type: "http",
      url: "https://api.glyphh.ai/api/runtime/mcp",
      headers: { authorization: "Bearer gy_rk_pod_secret" },
    });
  });

  it("lends them ALONGSIDE the desktop's loopback bridge on a local run", () => {
    const servers = lentServers({
      mcpServers: [{ name: "desktop", url: "http://127.0.0.1:51234/mcp", headers: { authorization: "Bearer local" } }],
    });
    // Both surfaces present: machine tools from the desktop, app tools from the
    // control plane. This is what makes desktop and cloud identical.
    expect(servers["desktop"]).toBeTruthy();
    expect(servers[APPS_SERVER_NAME]).toBeTruthy();
  });

  it("never overrides a server the caller explicitly lent under the same name", () => {
    const servers = lentServers({
      mcpServers: [{ name: APPS_SERVER_NAME, url: "https://caller.example/mcp" }],
    });
    expect(servers[APPS_SERVER_NAME]!.url).toBe("https://caller.example/mcp");
  });

  it("lends nothing in chat mode — a tool-less turn stays tool-less", () => {
    const { options } = buildQueryArgs(cfg({ mode: "chat" }), new HarnessSession({ runId: "run-t" }), []);
    expect(options.mcpServers).toBeUndefined();
    expect(options.tools).toEqual([]);
  });

  it("omits the surface when the gateway cannot be resolved, instead of failing the run", () => {
    const servers = lentServers({ gatewayUrl: "not a url" });
    expect(servers[APPS_SERVER_NAME]).toBeUndefined();
    // The ask_user server is still there — the turn still runs.
    expect(servers["glyphh"]).toBeTruthy();
  });
});

describe("the publish policy travels with the tools", () => {
  it("is appended to the caller's system prompt when the app tools are lent", () => {
    const { options } = buildQueryArgs(cfg({ system: "You are Glyphh." }), new HarnessSession({ runId: "run-t" }), []);
    const system = String(options.systemPrompt);
    expect(system).toContain("You are Glyphh.");
    expect(system).toContain("## Building and publishing a Glyphh app");
  });

  it("names the whole flow in order", () => {
    for (const step of ["scaffold_app", "npm run build", "create_app_entry", "build_app"]) {
      expect(PUBLISH_POLICY).toContain(step);
    }
  });

  it("forbids a local dev server by name — the exact failure this path removes", () => {
    expect(PUBLISH_POLICY).toContain("python3 -m http.server");
    expect(PUBLISH_POLICY).toContain("npx serve");
    expect(PUBLISH_POLICY).toContain("vite preview");
  });

  it("is not appended twice across turns", () => {
    const once = withPublishPolicy("You are Glyphh.");
    expect(withPublishPolicy(once)).toBe(once);
  });

  it("is absent from a chat turn, which has no tools to act on it", () => {
    const { options } = buildQueryArgs(cfg({ mode: "chat", system: "You are Glyphh." }), new HarnessSession({ runId: "run-t" }), []);
    expect(String(options.systemPrompt)).not.toContain("## Building and publishing a Glyphh app");
  });
});

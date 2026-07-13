/**
 * Gateway translation + prompt-cache tests (BUILD_PLAN.md Phase 9). §8.3
 * translation round-trips across MCP ↔ internal ↔ provider and fails typed on an
 * unknown representation; §8.6 prompt-cache lowering is provider-specific and
 * determinism-neutral (write then hit).
 */

import { describe, it, expect } from "vitest";

import { BasicGateway } from "../../src/plugins/gateway.js";

const gw = () => new BasicGateway();
const call = { name: "query", args: { q: "hi", n: 3 } };

describe("gateway translation (§8.3)", () => {
  it("round-trips internal ↔ mcp", () => {
    const g = gw();
    expect(g.translate("mcp", "internal", g.translate("internal", "mcp", call))).toEqual(call);
  });

  it("round-trips internal ↔ provider (OpenAI function shape)", () => {
    const g = gw();
    const provider = g.translate("internal", "provider", call) as { function: { name: string; arguments: string } };
    expect(provider.function.name).toBe("query");
    expect(JSON.parse(provider.function.arguments)).toEqual(call.args);
    expect(g.translate("provider", "internal", provider)).toEqual(call);
  });

  it("crosses mcp ↔ provider through the internal pivot", () => {
    const g = gw();
    const provider = g.translate("mcp", "provider", g.translate("internal", "mcp", call));
    expect(g.translate("provider", "mcp", provider)).toEqual(g.translate("internal", "mcp", call));
  });

  it("returns the payload unchanged for a same-representation translate", () => {
    expect(gw().translate("mcp", "mcp", call)).toBe(call);
  });

  it("throws E_UNTRANSLATABLE for an unknown representation", () => {
    expect(() => gw().translate("internal", "smoke-signal", call)).toThrowError(/E_UNTRANSLATABLE/);
  });
});

describe("prompt-cache lowering (§8.6)", () => {
  it("lowers to each provider's wire form", () => {
    const g = gw();
    expect(g.lowerPromptCache(["system", "tools"], "local")).toMatchObject({ cache_prompt: true });
    expect(g.lowerPromptCache(["system"], "anthropic")).toMatchObject({
      directives: [{ segment: "system", cache_control: { type: "ephemeral" } }],
    });
    expect(g.lowerPromptCache(["system"], "openai")).toMatchObject({ order: ["system"], annotation: null });
  });

  it("is a no-op (undefined) with no breakpoints or an unhonored provider", () => {
    const g = gw();
    expect(g.lowerPromptCache(undefined, "local")).toBeUndefined();
    expect(g.lowerPromptCache([], "local")).toBeUndefined();
    expect(g.lowerPromptCache(["system"], "carrier-pigeon")).toBeUndefined();
  });

  it("accounts a prefix as write on first sight, hit thereafter", () => {
    const g = gw();
    expect(g.accountPromptCache("k", 100)).toEqual({ disposition: "write", cache_read: 0, cache_write: 100 });
    expect(g.accountPromptCache("k", 100)).toEqual({ disposition: "hit", cache_read: 100, cache_write: 0 });
    // A different prefix is its own write.
    expect(g.accountPromptCache("other", 20).disposition).toBe("write");
  });
});

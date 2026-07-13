/**
 * BasicConnections — the in-process, loopback-only `HandlerRegistry`
 * (docs/runtime.md §3.4). One `Map<method, handler>` dispatches both `tool.mcp`
 * (in-runtime substrate tools, each with a JSON `inputSchema`) and `tool.app`
 * (client/app methods). `dispatch` NEVER raises — its failure becomes a frame;
 * `invoke` raises for callers that want to catch. The cloud reach-in was
 * removed — nothing dials into a personal machine; app methods are stubbed here
 * and a live client overrides them.
 *
 * A few substrate tools are seeded so `tool.mcp` works out of the box.
 */

import type { CapabilityStatus } from "../runtime/registry.js";
import type {
  ConnectionsPlugin,
  DispatchResult,
  Row,
  ToolHandler,
  ToolSchema,
} from "./interfaces.js";

export class BasicConnections implements ConnectionsPlugin {
  readonly name = "connections";
  private readonly handlers = new Map<string, ToolHandler>();
  private readonly schemas = new Map<string, ToolSchema>();

  constructor() {
    this.seedSubstrate();
  }

  status(): CapabilityStatus {
    return {
      ready: true,
      detail: `loopback registry; ${this.handlers.size} tools`,
      tier: "basic",
    };
  }

  register(method: string, handler: ToolHandler, schema?: ToolSchema): void {
    this.handlers.set(method, handler);
    this.schemas.set(method, schema ?? { name: method });
  }

  listTools(): ToolSchema[] {
    // Sorted for a deterministic manifest.
    return Array.from(this.schemas.values()).sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  }

  async dispatch(method: string, args: Row): Promise<DispatchResult> {
    const handler = this.handlers.get(method);
    if (!handler) return { ok: false, error: `E_NO_TOOL: ${method}` };
    try {
      const result = await handler(args);
      return { ok: true, result };
    } catch (e) {
      return { ok: false, error: e instanceof Error ? e.message : String(e) };
    }
  }

  async invoke(method: string, args: Row): Promise<unknown> {
    const handler = this.handlers.get(method);
    if (!handler) throw new Error(`E_NO_TOOL: ${method}`);
    return await handler(args);
  }

  /** A minimal, loopback-only substrate surface — the model chooses *what*, the
   *  tool guarantees *how*. A live client replaces the `tool.app` stubs. */
  private seedSubstrate(): void {
    this.register(
      "think",
      (args) => ({ thought: String(args.text ?? "") }),
      { name: "think", description: "Record a reasoning step.", inputSchema: { type: "object", properties: { text: { type: "string" } } } },
    );
    this.register(
      "echo",
      (args) => ({ ...args }),
      { name: "echo", description: "Return the arguments unchanged.", inputSchema: { type: "object" } },
    );
    this.register(
      "now_tick",
      (args) => ({ tick: Number(args.tick ?? 0) }),
      { name: "now_tick", description: "Echo the logical tick (never wall-clock).", inputSchema: { type: "object" } },
    );
    // A few more substrate-tool stubs so example rotors that name them dispatch
    // out of the box (a live substrate returns real rows). Deterministic + empty.
    this.register(
      "query",
      (args) => ({ rows: [], count: 0, echo: args }),
      { name: "query", description: "Closed-op query stub (returns no rows on the bare box).", inputSchema: { type: "object" } },
    );
    this.register(
      "recall",
      (args) => ({ hits: [], query: args.query ?? args.text ?? null }),
      { name: "recall", description: "Associative recall stub (no hits on the bare box).", inputSchema: { type: "object" } },
    );
    this.register(
      "keys",
      () => ({ keys: [] }),
      { name: "keys", description: "List memory keys (empty on the bare box).", inputSchema: { type: "object" } },
    );
    this.register(
      "compute",
      (args) => ({ result: computeArith(String(args.expr ?? args.text ?? "")) }),
      { name: "compute", description: "Evaluate a closed arithmetic expression (deterministic).", inputSchema: { type: "object", properties: { expr: { type: "string" } } } },
    );
    this.register(
      "concat",
      (args) => ({ text: (Array.isArray(args.parts) ? args.parts : []).map((p) => String(p)).join(String(args.sep ?? "")) }),
      { name: "concat", description: "Join string parts deterministically.", inputSchema: { type: "object" } },
    );
    // `tool.app` methods are LOOPBACK-only (§7.16) — nothing dials into a personal
    // machine. They echo a deterministic applied result; a live client overrides
    // them with real UI effects.
    for (const m of [
      "panels.open",
      "panels.switch",
      "panels.close",
      "layouts.open",
      "apps.callTool",
      "apps.install",
    ]) {
      this.register(m, (args) => ({ method: m, params: args, applied: true, loopback: true }), {
        name: m,
        description: `App method ${m} (loopback; a live client applies real effects).`,
      });
    }
  }
}

/** A closed, deterministic arithmetic evaluator — `+ - * /` over numbers, no
 *  `eval`, no identifiers. Returns `null` on anything it does not recognize. */
function computeArith(expr: string): number | null {
  const tokens = expr.match(/\d+(?:\.\d+)?|[+\-*/()]/g);
  if (!tokens || tokens.length === 0) return null;
  let pos = 0;
  const peek = () => tokens[pos];
  const eat = () => tokens[pos++];
  const parseExpr = (): number => {
    let v = parseTerm();
    while (peek() === "+" || peek() === "-") v = eat() === "+" ? v + parseTerm() : v - parseTerm();
    return v;
  };
  const parseTerm = (): number => {
    let v = parseFactor();
    while (peek() === "*" || peek() === "/") v = eat() === "*" ? v * parseFactor() : v / parseFactor();
    return v;
  };
  const parseFactor = (): number => {
    if (peek() === "(") {
      eat();
      const v = parseExpr();
      if (peek() === ")") eat();
      return v;
    }
    return Number(eat());
  };
  const result = parseExpr();
  return Number.isFinite(result) ? result : null;
}

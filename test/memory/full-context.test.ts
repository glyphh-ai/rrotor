/**
 * ============================================================================
 *  FULL-CONTEXT FALLBACK — "memory off must NEVER mean thread-loss"
 * ============================================================================
 *
 * System under test: `src/harness/transcript.ts` (per-session transcript
 * retention + first-class compaction) and its engine wiring
 * (`src/harness/engine.ts` runHarness / assemblePrompt). The one property this
 * suite exists to prove: **when the memory rotor is NOT active, every turn's
 * worker prompt carries the COMPLETE conversation — all prior turns verbatim —
 * with compaction (never silent truncation) when it outgrows the budget.**
 *
 * This is the deterministic floor UNDER the memory rotor: the rotor is opt-in
 * (`rotorOn = memoryOn && cfg.memory?.rotor?.enabled === true`) and usually
 * OFF, and the deterministic fact floor is lossy (memory-rotor.test.ts, the
 * "BOUND" test). The real incident: a cloud session built + published an app,
 * then next turn claimed "my workspace is empty" because the per-run prompt
 * carried only what the client happened to resend.
 *
 * DETERMINISM / OFFLINE — conventions of memory-rotor.test.ts: no live model,
 * no network. The SDK is faked via the injected `queryFn` (capturing the exact
 * prompt the worker receives); the compaction summarizer + credits probe ride
 * the injectable `fetchFn`; the rotor-on test stubs `global.fetch` for the
 * assembler. Each test isolates its own TranscriptStore via `deps.transcripts`.
 *
 * WHAT EACH GROUP GUARANTEES
 *   A. Retention (THE REGRESSION) — rotor OFF, memory OFF: turn 2's worker
 *      prompt contains turn 1's user prompt AND the assistant's answer
 *      VERBATIM, even when the client resends no/partial history; a pod
 *      restart is reconciled from the client's fuller history.
 *   B. Compaction — over budget, the oldest turns fold into a summary block
 *      (LLM back-path, record-once), recent turns stay VERBATIM, and durable
 *      anchors (slug/URL/paths/goal) ALWAYS survive — even when the
 *      summarizer omits them or fails outright (deterministic gist fold).
 *      A `compaction` frame is emitted so surfaces can show it.
 *   C. Replay determinism — the compaction call is record-once (a repeat
 *      compact re-invokes nothing; the rendered context is byte-identical);
 *      appends are idempotent per runId.
 *   D. Rotor ON unchanged — the rotor owns the system prompt; the fallback
 *      does NOT engage (no double-inject), while retention still records the
 *      exchange so a later rotor-off turn has the thread.
 *
 * Run: `npx vitest run test/memory/full-context.test.ts`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { runHarness } from "../../src/harness/engine.js";
import type { QueryFn } from "../../src/harness/engine.js";
import { HarnessSession } from "../../src/harness/session.js";
import type { HarnessRunConfig } from "../../src/harness/config.js";
import {
  TranscriptStore,
  CONTEXT_DEFAULTS,
  estimateTokens,
  harvestAnchors,
  renderContext,
} from "../../src/harness/transcript.js";
import type { Principal } from "../../src/auth/introspect.js";

// ── The scenario the whole suite reuses (memory-rotor.test.ts parity): the
//    fact the user must be able to continue from is the published app. ───────
const APP_SLUG = "slides-9f2";
const APP_URL = "https://slides-9f2.glyphh.app";
const PUBLISH_TURN = `Build a PowerPoint app and publish it. It is called SlideForge, slug ${APP_SLUG}, live at ${APP_URL}.`;
const PUBLISH_ANSWER = `Done — SlideForge is published. Slug ${APP_SLUG}, live at ${APP_URL}. Source at /workspace/slideforge/index.html.`;
const CONTINUE_TURN = "which app did we build — open that app again";

// ── Harness plumbing fakes ──────────────────────────────────────────────────

/** A run config on a throwaway workspace. gatewayUrl deliberately does NOT
 *  match the `<control>/api/gateway` shape, so with no principal the engine's
 *  memory plane stays UNARMED — the exact incident condition. */
function cfg(over: Partial<HarnessRunConfig> = {}): HarnessRunConfig {
  const home = mkdtempSync(join(tmpdir(), "fullctx-"));
  return {
    runId: over.runId ?? "run-t",
    sessionId: "sess-t",
    prompt: "do the thing",
    history: [],
    mode: "code",
    permission: "auto",
    gatewayUrl: "https://gw.test",
    runtimeToken: "gy_rt_fullctx_secret",
    workdir: join(home, "workspace"),
    attachDir: join(home, "workspace"),
    configDir: join(home, "agent-config"),
    attachments: [],
    attachmentMaxBytes: 1024,
    maxTurns: 0,
    ...over,
  };
}

/** A captured worker call: the exact prompt + system prompt the SDK received. */
interface Captured {
  prompt: string;
  system: string;
}

/** Fake the SDK: capture what the worker was given, answer with `answer`. */
function worker(answer: string, captured: Captured[]): QueryFn {
  return (args) => {
    captured.push({ prompt: String(args.prompt), system: String(args.options.systemPrompt ?? "") });
    return (async function* () {
      yield { type: "result", subtype: "success", result: answer };
    })();
  };
}

/** The injectable fetch: credits probe → 404 (no frame); the compaction
 *  summarizer (`…/v1/messages`) → a deterministic fixture (or a failure). */
interface FakeGateway {
  fetchFn: typeof fetch;
  readonly summarizeCalls: number;
  /** Every summarizer input, in call order. */
  inputs: string[];
}
function fakeFetch(opts: { summary?: string; fail?: boolean } = {}): FakeGateway {
  let calls = 0;
  const inputs: string[] = [];
  const fetchFn = (async (input: unknown, init?: { body?: unknown }): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : (input as { url: string }).url;
    if (url.includes("/messages")) {
      calls++;
      if (opts.fail) return new Response("summarizer down", { status: 503 });
      const body = JSON.parse(String(init?.body ?? "{}")) as { messages?: Array<{ content: string }> };
      inputs.push(body.messages?.map((m) => m.content).join("\n") ?? "");
      const text = opts.summary ?? "Earlier work summarized.";
      return new Response(JSON.stringify({ content: [{ type: "text", text }] }), { status: 200 });
    }
    if (url.includes("/usage")) return new Response("{}", { status: 404 });
    throw new Error(`unexpected fetch in test: ${url}`);
  }) as unknown as typeof fetch;
  return {
    fetchFn,
    get summarizeCalls() {
      return calls;
    },
    inputs,
  };
}

/** Run one full harness turn against an isolated store; returns the session. */
async function turn(
  store: TranscriptStore,
  over: Partial<HarnessRunConfig>,
  answer: string,
  captured: Captured[],
  fetchFn: typeof fetch,
  principal?: Principal,
): Promise<HarnessSession> {
  const c = cfg(over);
  const s = new HarnessSession({ runId: c.runId, sessionId: c.sessionId });
  await runHarness(s, c, { queryFn: worker(answer, captured), fetchFn, transcripts: store }, principal);
  return s;
}

beforeEach(() => {
  // The direct stator path must stay unarmed — memory truly OFF in groups A–C.
  delete process.env.ROTOR_STATOR_URL;
});
afterEach(() => {
  vi.unstubAllGlobals();
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP A — Retention: THE REGRESSION with memory OFF
// ═══════════════════════════════════════════════════════════════════════════
describe("A. retention — rotor OFF ⇒ turn 2 carries turn 1 verbatim", () => {
  it("turn 2's worker prompt contains turn 1's prompt AND answer verbatim (client resends NOTHING)", async () => {
    const store = new TranscriptStore();
    const captured: Captured[] = [];
    const { fetchFn } = fakeFetch();

    // ── Turn 1: build + publish (memory unarmed: no principal, no stator). ──
    const s1 = await turn(store, { runId: "run-1", prompt: PUBLISH_TURN }, PUBLISH_ANSWER, captured, fetchFn);
    expect(s1.status).toBe("done");

    // ── Turn 2: the incident replay — the client sends EMPTY history. ──
    await turn(store, { runId: "run-2", prompt: CONTINUE_TURN, history: [] }, "Opening SlideForge.", captured, fetchFn);

    const p2 = captured[1].prompt;
    expect(p2).toContain("<conversation_so_far>");
    expect(p2).toContain(PUBLISH_TURN); // turn 1's user prompt, VERBATIM
    expect(p2).toContain(PUBLISH_ANSWER); // and the assistant's answer, VERBATIM
    expect(p2).toContain(APP_SLUG);
    expect(p2).toContain(APP_URL);
    // The current ask still rides untouched after the context block.
    expect(p2).toContain(CONTINUE_TURN);
  });

  it("a PARTIAL client history never shrinks the pod's fuller record", async () => {
    const store = new TranscriptStore();
    const captured: Captured[] = [];
    const { fetchFn } = fakeFetch();

    await turn(store, { runId: "run-1", prompt: PUBLISH_TURN }, PUBLISH_ANSWER, captured, fetchFn);
    await turn(store, { runId: "run-2", prompt: "make the theme dark" }, "Dark theme applied.", captured, fetchFn);

    // Turn 3: the client resends only the LAST exchange (partial history — the
    // incident's other shape). The pod's 4-turn record must win.
    await turn(
      store,
      { runId: "run-3", prompt: CONTINUE_TURN, history: [{ role: "user", content: "make the theme dark" }, { role: "assistant", content: "Dark theme applied." }] },
      "Opening it.",
      captured,
      fetchFn,
    );
    const p3 = captured[2].prompt;
    expect(p3).toContain(PUBLISH_TURN); // the pod record survived the partial resend
    expect(p3).toContain("Dark theme applied.");
  });

  it("pod restart: a FULLER client history reseeds the (empty) store — the thread survives pod loss", async () => {
    const freshStore = new TranscriptStore(); // the restarted pod: nothing retained
    const captured: Captured[] = [];
    const { fetchFn } = fakeFetch();
    await turn(
      freshStore,
      {
        runId: "run-9",
        prompt: CONTINUE_TURN,
        history: [
          { role: "user", content: PUBLISH_TURN },
          { role: "assistant", content: PUBLISH_ANSWER },
        ],
      },
      "Opening it.",
      captured,
      fetchFn,
    );
    expect(captured[0].prompt).toContain(PUBLISH_TURN);
    expect(captured[0].prompt).toContain(APP_URL);
  });
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP B — Compaction, first-class
// ═══════════════════════════════════════════════════════════════════════════
describe("B. compaction — budget-triggered, anchors always survive", () => {
  /** Seed a long session: the goal-bearing publish exchange, then filler. */
  function seed(store: TranscriptStore, key: string, fillerTurns: number): void {
    store.append(key, "seed-0", PUBLISH_TURN, PUBLISH_ANSWER);
    for (let i = 1; i <= fillerTurns; i++) {
      store.append(key, `seed-${i}`, `unrelated follow-up number ${i} with plenty of padding text to burn budget`, `handled follow-up ${i}`);
    }
  }

  it("over budget: oldest turns fold into the summary, recent turns stay VERBATIM, a compaction frame is emitted", async () => {
    const store = new TranscriptStore();
    seed(store, "sess-t", 10);
    const captured: Captured[] = [];
    const fx = fakeFetch({ summary: `Earlier: user had SlideForge built and published (slug ${APP_SLUG}).` });

    const s = await turn(
      store,
      { runId: "run-c", prompt: "continue", context: { budgetTokens: 120, keepTurns: 2 } },
      "Continuing.",
      captured,
      fx.fetchFn,
    );

    // The compaction frame rode the stream BEFORE the terminal frame.
    const comp = s.framesSince(-1).find((f) => f.type === "compaction") as { folded: number; kept: number; via: string } | undefined;
    expect(comp).toBeDefined();
    expect(comp!.folded).toBeGreaterThan(0);
    expect(comp!.kept).toBe(2);
    expect(comp!.via).toBe("model");

    const p = captured[0].prompt;
    expect(p).toContain("[Compacted summary");
    expect(p).toContain("Earlier: user had SlideForge built and published"); // the record-once summary
    // Recent turns VERBATIM (the newest keepTurns=2).
    expect(p).toContain("unrelated follow-up number 10 with plenty of padding text to burn budget");
    expect(p).toContain("handled follow-up 10");
    // The oldest turns are folded, not carried verbatim.
    expect(p).not.toContain("unrelated follow-up number 1 with plenty of padding text to burn budget");
  });

  it("anchors (slug/URL/path/goal) survive compaction even when the summarizer OMITS them", async () => {
    const store = new TranscriptStore();
    seed(store, "sess-t", 10);
    const captured: Captured[] = [];
    // A summarizer that paraphrases everything away — the worst case.
    const fx = fakeFetch({ summary: "Some earlier work happened." });

    await turn(store, { runId: "run-c", prompt: "continue", context: { budgetTokens: 120, keepTurns: 2 } }, "ok", captured, fx.fetchFn);

    const p = captured[0].prompt;
    expect(p).toContain("[Durable anchors");
    expect(p).toContain(APP_URL); // harvested deterministically, injected verbatim
    expect(p).toContain(`slug ${APP_SLUG}`);
    expect(p).toContain("/workspace/slideforge/index.html"); // the file path produced
    expect(p).toContain(`Session goal: ${PUBLISH_TURN}`); // the first user turn is the goal
  });

  it("summarizer failure degrades to the deterministic gist fold — never a lost thread, never a throw", async () => {
    const store = new TranscriptStore();
    seed(store, "sess-t", 10);
    const captured: Captured[] = [];
    const fx = fakeFetch({ fail: true });

    const s = await turn(store, { runId: "run-c", prompt: "continue", context: { budgetTokens: 120, keepTurns: 2 } }, "ok", captured, fx.fetchFn);
    expect(s.status).toBe("done"); // compaction failure never fails the turn

    const comp = s.framesSince(-1).find((f) => f.type === "compaction") as { via: string } | undefined;
    expect(comp?.via).toBe("deterministic");
    const p = captured[0].prompt;
    expect(p).toContain("(user, earlier)"); // gist lines stand in for the summary
    expect(p).toContain(APP_URL); // anchors still verbatim
    expect(p).toContain(`slug ${APP_SLUG}`);
  });

  it("under budget: nothing folds, no frame, every turn verbatim (defaults are generous)", async () => {
    const store = new TranscriptStore();
    store.append("sess-t", "seed-0", PUBLISH_TURN, PUBLISH_ANSWER);
    const captured: Captured[] = [];
    const fx = fakeFetch();

    const s = await turn(store, { runId: "run-c", prompt: "continue" }, "ok", captured, fx.fetchFn);
    expect(s.framesSince(-1).some((f) => f.type === "compaction")).toBe(false);
    expect(fx.summarizeCalls).toBe(0);
    expect(captured[0].prompt).toContain(PUBLISH_TURN);
    expect(captured[0].prompt).not.toContain("[Compacted summary");
  });

  it("the verbatim floor: recent turns alone over budget are KEPT, not dropped", async () => {
    const store = new TranscriptStore();
    const huge = `giant turn ${"x".repeat(4000)} with ${APP_URL}`;
    store.append("k", "r1", huge, "ok");
    // budget 10 tokens, keepTurns 8 > retained turns ⇒ nothing foldable.
    const ev = await store.compact("k", { budgetTokens: 10, keepTurns: 8 });
    expect(ev).toBeNull();
    expect(store.render("k")).toContain(huge); // over budget but never lost
  });
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP C — Replay determinism (record-once, the docs' mechanism)
// ═══════════════════════════════════════════════════════════════════════════
describe("C. replay determinism", () => {
  it("compaction is record-once: a repeat compact re-invokes NOTHING and the render is byte-identical", async () => {
    const store = new TranscriptStore();
    store.append("k", "r1", PUBLISH_TURN, PUBLISH_ANSWER);
    for (let i = 2; i <= 12; i++) store.append("k", `r${i}`, `follow-up ${i} with some padding text in it`, `answer ${i}`);

    let calls = 0;
    const summarize = async (input: string): Promise<string> => {
      calls++;
      expect(input).toContain("MUST SURVIVE VERBATIM"); // anchors ride into the call
      return "Deterministic fixture summary.";
    };

    const first = await store.compact("k", { budgetTokens: 60, keepTurns: 2, summarize });
    expect(first).not.toBeNull();
    expect(calls).toBe(1);
    const a = store.render("k");

    // REPLAY: the same compact again — the summary is recorded state now.
    const second = await store.compact("k", { budgetTokens: 60, keepTurns: 2, summarize });
    expect(second).toBeNull(); // nothing left to fold at this budget
    expect(calls).toBe(1); // the model was NOT re-invoked
    expect(store.render("k")).toBe(a); // byte-identical
  });

  it("appends are idempotent per runId (a retried/replayed run never double-appends)", () => {
    const store = new TranscriptStore();
    store.append("k", "run-1", "hello", "hi");
    store.append("k", "run-1", "hello", "hi"); // the replay
    expect(store.turnCount("k")).toBe(2); // one user + one assistant, once
    expect(store.render("k")).toBe("User: hello\nGlyphh: hi");
  });

  it("renderContext is a pure function of state (same state ⇒ same bytes)", () => {
    const state = {
      summary: "sum",
      folded: 3,
      turns: [{ role: "user" as const, content: "a" }, { role: "assistant" as const, content: "b" }],
      anchors: [APP_URL],
      goal: "build the app",
    };
    expect(renderContext(state)).toBe(renderContext(state));
    expect(renderContext(state)).toContain(APP_URL);
  });

  it("harvestAnchors is deterministic and catches URL, app host, slug, and absolute paths", () => {
    const anchors = harvestAnchors(PUBLISH_ANSWER);
    expect(anchors).toContain(APP_URL);
    expect(anchors).toContain(`${APP_SLUG}.glyphh.app`);
    expect(anchors).toContain(`slug ${APP_SLUG}`);
    expect(anchors).toContain("/workspace/slideforge/index.html");
    expect(harvestAnchors(PUBLISH_ANSWER)).toEqual(anchors); // stable
  });

  it("sane defaults exist and the estimate is the documented chars/4", () => {
    expect(CONTEXT_DEFAULTS.budgetTokens).toBeGreaterThanOrEqual(8_000);
    expect(CONTEXT_DEFAULTS.keepTurns).toBeGreaterThanOrEqual(2);
    expect(estimateTokens("abcdefgh")).toBe(2);
  });
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP D — Rotor ON: current behavior stands, no double-inject
// ═══════════════════════════════════════════════════════════════════════════
describe("D. rotor ON — the fallback stands down", () => {
  const principal: Principal = { orgId: "org-t", userId: "user-t" };

  function stubRotorFetch(): void {
    // The rotor's assembler + attention ride GLOBAL fetch via the gateway;
    // the credits probe may too. Route both; nothing else may leave the test.
    vi.stubGlobal("fetch", (async (input: unknown): Promise<Response> => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : (input as { url: string }).url;
      if (url.includes("/messages")) {
        return new Response(
          JSON.stringify({ content: [{ type: "text", text: "ROTOR-CONSTRUCTED-BRIEF" }], usage: { input_tokens: 1, output_tokens: 1 } }),
          { status: 200 },
        );
      }
      if (url.includes("/usage")) return new Response("{}", { status: 404 });
      throw new Error(`unexpected fetch in test: ${url}`);
    }) as unknown as typeof fetch);
  }

  it("rotor ON: the rotor owns the system prompt; the transcript block is NOT injected (no double-inject)", async () => {
    stubRotorFetch();
    const store = new TranscriptStore();
    // The store already holds the thread (from earlier rotor-off turns) — the
    // rotor-on turn must NOT inject it alongside the rotor's own brief.
    store.append("sess-t", "old-run", PUBLISH_TURN, PUBLISH_ANSWER);

    const captured: Captured[] = [];
    const c = cfg({ runId: "run-r", prompt: CONTINUE_TURN, memory: { rotor: { enabled: true } } });
    const s = new HarnessSession({ runId: c.runId, sessionId: c.sessionId });
    await runHarness(s, c, { queryFn: worker("Opening it.", captured), transcripts: store }, principal);

    expect(s.status).toBe("done");
    expect(captured[0].system).toContain("ROTOR-CONSTRUCTED-BRIEF"); // the rotor built the system prompt
    // No fallback injection: the prompt is the bare ask (no conversation block,
    // no compaction artifacts) — the rotor path is byte-for-byte the old one.
    expect(captured[0].prompt).not.toContain("<conversation_so_far>");
    expect(captured[0].prompt).not.toContain(PUBLISH_TURN);
    expect(captured[0].prompt).not.toContain("[Compacted summary");
    expect(s.framesSince(-1).some((f) => f.type === "compaction")).toBe(false);
  });

  it("rotor ON still RETAINS the exchange, so a later rotor-off turn has the thread", async () => {
    stubRotorFetch();
    const store = new TranscriptStore();
    const captured: Captured[] = [];
    const c = cfg({ runId: "run-r1", prompt: PUBLISH_TURN, memory: { rotor: { enabled: true } } });
    const s = new HarnessSession({ runId: c.runId, sessionId: c.sessionId });
    await runHarness(s, c, { queryFn: worker(PUBLISH_ANSWER, captured), transcripts: store }, principal);
    expect(store.turnCount("sess-t")).toBe(2); // retained even while the rotor ran

    // The NEXT turn runs rotor-OFF (memory unarmed) — and still has the thread.
    vi.unstubAllGlobals();
    const { fetchFn } = fakeFetch();
    await turn(store, { runId: "run-r2", prompt: CONTINUE_TURN }, "Opening it.", captured, fetchFn);
    expect(captured[1].prompt).toContain(PUBLISH_TURN);
    expect(captured[1].prompt).toContain(APP_URL);
  });
});

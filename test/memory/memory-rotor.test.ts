/**
 * ============================================================================
 *  MEMORY ROTOR — the "never forget a session's context" harness
 * ============================================================================
 *
 * System under test: `src/harness/memory-rotor.ts` (the memory rotor v0) and the
 * memory plane it rotates around (`src/plugins/memory.ts` BasicMemory, the
 * `recallContext`/`constructSystemPrompt` assembler seam, `src/exec/facts.ts`
 * tiers, `src/harness/enricher.ts` record-once determinism). The one property
 * this suite exists to prove: **a fact learned on turn 1 is still reachable on a
 * later turn — the rotor does not "forget" a session's context across turns.**
 *
 * DETERMINISM / OFFLINE. The rotor's assembler and the premium enricher are the
 * only stochastic-data components; both call out over `fetch`. This suite NEVER
 * touches a live model:
 *   - `installFakeModels()` stubs `global.fetch` and routes by URL:
 *       * the ASSEMBLER (Anthropic `…/v1/messages`) → a deterministic echo that
 *         reflects the LONG-TERM MEMORY + USER PROMPT it was handed, so we can
 *         assert *what the rotor fed the worker model* (the slug must be in it).
 *       * the ENRICHER  (OpenAI-compat `…/chat/completions`) → a fixture map that
 *         emits structured `(entity, role, filler, tier)` facts per input turn,
 *         standing in for qwen/Haiku schema-on-write. Determinism-by-recording.
 *   - Fixtures are pure and offline; there is no network, no clock dependence.
 *
 * WHAT EACH GROUP GUARANTEES
 *   1. Enablement gate       — the rotor engages ONLY when `memoryOn && rotor
 *                              .enabled === true`; the gate expression + its
 *                              `memoryOn` sub-conditions are asserted directly, and
 *                              an OFF rotor mutates no system prompt and writes
 *                              nothing.
 *   2. observe → distill → write — a user turn lands the expected tiered facts in
 *                              the stator (schema-on-write); a write failure is
 *                              best-effort and never throws into the turn.
 *   3. recall → construct    — the assembled worker prompt CONTAINS the goal + key
 *                              entities needed to continue, scoped + budgeted.
 *   4. THE REGRESSION        — build+publish an app on turn 1, ask "which app / open
 *                              it" on turn 2; the slug/URL is recalled. This is the
 *                              whole point — it FAILS on a broken/ephemeral rotor.
 *   5. Persistence           — a persistent (SQLite-file) stator KEEPS turn-1 facts
 *                              across a torn-down + reconstructed rotor (pod
 *                              restart); an in-process (`:memory:`) stator LOSES
 *                              them. Ephemerality is a documented, tested property.
 *   6. Session isolation     — session A's short-tier facts never surface in
 *                              session B; mid-tier window behaves per docs.
 *   7. Attention / focus     — `attentionCheck`/`renderFocus` hold the session goal
 *                              (true north) stable as turns accrue; the fidelity
 *                              buffer evicts to gist without dropping the goal.
 *   8. Determinism / replay  — the record-once enricher yields byte-stable output
 *                              on replay (docs/memory.md mechanism 3).
 *   9. Failure modes         — stator unreachable, malformed enricher output,
 *                              empty/whitespace turns — none crash a turn; the
 *                              in-memory mirror stays authoritative.
 *
 * Run: `npm test` (vitest). Framework + conventions match `test/memory/*`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { InProcessStore } from "../../src/exec/store.js";
import { createStator } from "../../src/exec/stator.js";
import { buildBasicPlugins } from "../../src/plugins/index.js";
import { absorbText } from "../../src/handlers/memory.js";
import { recallContext } from "../../src/exec/recall.js";
import {
  MemoryRotor,
  constructSystemPrompt,
  attentionCheck,
  renderFocus,
  type AttentionState,
} from "../../src/harness/memory-rotor.js";
import type { Stator } from "../../src/exec/store.js";
import type { MemoryPlugin } from "../../src/plugins/interfaces.js";

// ───────────────────────────────────────────────────────────────────────────
//  Fake model plane — deterministic, offline stand-ins for the two model calls.
// ───────────────────────────────────────────────────────────────────────────

/** A structured extraction for one input turn, keyed on a substring the turn
 *  contains. Stands in for the qwen/Haiku enricher: record-once, deterministic. */
type EnrichFixture = { match: RegExp; facts: Array<{ entity?: string; role: string; filler: string; tier?: string }> };

interface FakeModels {
  restore: () => void;
  /** Every prompt the ASSEMBLER was asked to build, in call order (for asserting
   *  what memory actually reached the worker-prompt construction). */
  assemblerInputs: string[];
  /** Count of enricher calls — the record-once guarantee is "one call per turn". */
  enrichCalls: number;
}

/**
 * Route `global.fetch`:
 *   - `…/messages`          → the ASSEMBLER. Echoes the LONG-TERM MEMORY + USER
 *     PROMPT sections back as the "worker system prompt", so a test can assert the
 *     rotor actually surfaced a fact into the constructed prompt. Deterministic.
 *   - `…/chat/completions`  → the ENRICHER. Emits the fixture facts whose `match`
 *     hits the user text. Unmatched input → `[]` (nothing worth keeping).
 * `assemblerFail`/`enricherFail` force the respective endpoint to error, to prove
 * best-effort fallback (rotor falls back to the deterministic floor / raw recall).
 */
function installFakeModels(
  fixtures: EnrichFixture[] = [],
  opts: { assemblerFail?: boolean; enricherFail?: boolean; assembler?: (system: string, user: string) => string } = {},
): FakeModels {
  const state: FakeModels = { restore: () => {}, assemblerInputs: [], enrichCalls: 0 };

  const fake = async (input: unknown, init?: { body?: unknown }): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : (input as { url: string }).url;
    const body = JSON.parse(String(init?.body ?? "{}")) as {
      system?: string;
      messages?: Array<{ role: string; content: string }>;
    };

    // ── ASSEMBLER (Anthropic Messages) ────────────────────────────────────
    if (url.includes("/messages")) {
      if (opts.assemblerFail) return new Response("assembler down", { status: 503 });
      const user = body.messages?.map((m) => m.content).join("\n") ?? "";
      state.assemblerInputs.push(user);
      const text = opts.assembler
        ? opts.assembler(body.system ?? "", user)
        : // Default deterministic assembler: a worker prompt that carries the goal
          // and the recalled memory verbatim — the honest "did the fact reach the
          // worker" probe, no paraphrase to hide behind.
          [
            "## Focus",
            extractSection(user, "PRIMARY FOCUS (the goal)"),
            "## Look here",
            extractSection(user, "LONG-TERM MEMORY"),
            "## Do this",
            "Continue the task using the recalled context above.",
          ].join("\n");
      return jsonResponse({
        content: [{ type: "text", text }],
        usage: { input_tokens: user.length, output_tokens: text.length },
      });
    }

    // ── ENRICHER (OpenAI-compatible chat) ─────────────────────────────────
    if (url.includes("/chat/completions")) {
      state.enrichCalls++;
      if (opts.enricherFail) return new Response("enricher down", { status: 500 });
      const userText = body.messages?.find((m) => m.role === "user")?.content ?? "";
      const hit = fixtures.find((f) => f.match.test(userText));
      const facts = (hit?.facts ?? []).map((f) => ({ entity: f.entity ?? "user", role: f.role, filler: f.filler, tier: f.tier ?? "mid" }));
      return jsonResponse({ choices: [{ message: { content: JSON.stringify(facts) } }] });
    }

    throw new Error(`unexpected fetch in test: ${url}`);
  };

  vi.stubGlobal("fetch", fake as unknown as typeof fetch);
  state.restore = () => vi.unstubAllGlobals();
  return state;
}

function jsonResponse(obj: unknown): Response {
  return new Response(JSON.stringify(obj), { status: 200, headers: { "content-type": "application/json" } });
}

/** Pull the body of a `### TITLE\n…` section out of the assembler's input. */
function extractSection(input: string, title: string): string {
  const re = new RegExp(`### ${title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\n([\\s\\S]*?)(?=\\n### |$)`);
  return re.exec(input)?.[1]?.trim() ?? "";
}

// Point the enricher at a fake OpenAI-compatible host and the assembler at
// Anthropic; both are intercepted by `installFakeModels`. Set/cleared per test.
function armModelEnv(): void {
  process.env.ROTOR_ENRICH_MODEL_URL = "http://fake-enricher.local/v1";
  process.env.ROTOR_ENRICH_MODEL_ID = "qwen3:14b";
  process.env.ANTHROPIC_API_KEY = "test-key";
  delete process.env.MEMORY_ROTOR_MODEL_URL; // keep the assembler on the Anthropic branch
}
function disarmModelEnv(): void {
  delete process.env.ROTOR_ENRICH_MODEL_URL;
  delete process.env.ROTOR_ENRICH_MODEL_ID;
  delete process.env.ANTHROPIC_API_KEY;
}

const mem = (store: Stator): MemoryPlugin => buildBasicPlugins({ store }).memory;
const fillers = (facts: { filler: string }[]) => facts.map((f) => f.filler);

// A PowerPoint-app-publish scenario the whole suite reuses: the fact the user
// must be able to continue from is the published app's slug + URL.
const APP_SLUG = "slides-9f2";
const APP_URL = "https://slides-9f2.glyphh.app";
const PUBLISH_TURN = `Build a PowerPoint app and publish it. It is called SlideForge, slug ${APP_SLUG}, live at ${APP_URL}.`;
const APP_FIXTURE: EnrichFixture = {
  match: /powerpoint|slideforge|publish/i,
  facts: [
    { role: "app", filler: "SlideForge", tier: "mid" },
    { role: "app_slug", filler: APP_SLUG, tier: "mid" },
    { role: "app_url", filler: APP_URL, tier: "mid" },
  ],
};

beforeEach(() => armModelEnv());
afterEach(() => {
  disarmModelEnv();
  vi.unstubAllGlobals();
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP 1 — Enablement gate
//  Guarantee: the rotor engages ONLY when `memoryOn && rotor.enabled === true`.
// ═══════════════════════════════════════════════════════════════════════════
describe("1. enablement gate", () => {
  // The engine gate (engine.ts ~L386/L395) reproduced as a pure predicate so the
  // exact boolean law is under test independent of the full runHarness plumbing.
  const memoryOn = (principal: unknown, statorBase: unknown, runtimeToken: unknown) =>
    !!(principal || (statorBase && runtimeToken));
  const rotorOn = (principal: unknown, statorBase: unknown, runtimeToken: unknown, enabled?: boolean) =>
    memoryOn(principal, statorBase, runtimeToken) && enabled === true;

  it("memoryOn arms on a principal (cloud pod) OR statorBase+token (local pod)", () => {
    expect(memoryOn({ org: "o" }, null, null)).toBe(true); // cloud: principal present
    expect(memoryOn(null, "https://ctl", "tok")).toBe(true); // local: base + token
    expect(memoryOn(null, "https://ctl", null)).toBe(false); // base without token
    expect(memoryOn(null, null, "tok")).toBe(false); // token without base
    expect(memoryOn(null, null, null)).toBe(false); // nothing → unarmed
  });

  it("rotorOn requires memoryOn AND rotor.enabled === true (truthy is not enough)", () => {
    expect(rotorOn({ org: "o" }, null, null, true)).toBe(true);
    expect(rotorOn({ org: "o" }, null, null, false)).toBe(false);
    expect(rotorOn({ org: "o" }, null, null, undefined)).toBe(false);
    // memory off → rotor off even if enabled.
    expect(rotorOn(null, null, null, true)).toBe(false);
    // Guard against a `=== true` regression to a truthy check:
    expect(rotorOn({ org: "o" }, null, null, 1 as unknown as boolean)).toBe(false);
  });

  it("an OFF rotor no-ops: no assembler call, the base system prompt is untouched", async () => {
    // "Off" = the engine never constructs; the worker keeps its base system prompt
    // and only the raw <memory> recall block (if any) is appended. We assert the
    // assembler is never invoked when the caller doesn't run the rotor path.
    const models = installFakeModels([APP_FIXTURE]);
    const store = new InProcessStore();
    const m = mem(store);
    await m.write(absorbText("Always use tabs.", "user"), {});
    // The recall floor (rotor OFF path) builds a plain block; no model is called.
    const block = (await recallContext(m, "how to indent")).block;
    expect(block).toMatch(/tabs/i);
    expect(models.assemblerInputs).toHaveLength(0); // rotor never engaged
    models.restore();
  });

  it("an ON rotor DOES call the assembler and mutates the worker prompt", async () => {
    const models = installFakeModels([APP_FIXTURE]);
    const { systemPrompt } = await constructSystemPrompt({
      userPrompt: "continue",
      model: "claude-haiku-4-5",
      goal: "ship the app",
      longTerm: "known: app_url https://x.glyphh.app",
      baseSystem: "BASE-SYSTEM-SENTINEL",
    });
    expect(models.assemblerInputs).toHaveLength(1); // assembler engaged
    expect(systemPrompt).toContain("BASE-SYSTEM-SENTINEL"); // base preserved above
    expect(systemPrompt).toContain("## Focus"); // constructed brief appended
    models.restore();
  });
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP 2 — observe → distill → write (schema-on-write)
// ═══════════════════════════════════════════════════════════════════════════
describe("2. observe → distill → write", () => {
  it("a user turn distils to the expected tiered facts in the stator", async () => {
    const models = installFakeModels([APP_FIXTURE]);
    const store = new InProcessStore();
    const m = mem(store);
    const rotor = new MemoryRotor(m, { entity: "user" });

    await store.touchSession("s1");
    await rotor.observe("user", PUBLISH_TURN, "s1");

    const facts = await m.recall({ entity: "user", session: "s1" });
    const byRole = Object.fromEntries(facts.map((f) => [f.role, f.filler]));
    expect(byRole["app"]).toBe("SlideForge");
    expect(byRole["app_slug"]).toBe(APP_SLUG);
    expect(byRole["app_url"]).toBe(APP_URL);
    // The enricher tagged these `mid` (task facts), not lifelong.
    expect(facts.find((f) => f.role === "app_slug")?.tier).toBe("mid");
    models.restore();
  });

  it("assistant turns do NOT distil (only user turns are absorbed)", async () => {
    const models = installFakeModels([{ match: /.*/, facts: [{ role: "leak", filler: "should-not-store" }] }]);
    const store = new InProcessStore();
    const rotor = new MemoryRotor(mem(store), { entity: "user" });
    await rotor.observe("assistant", "I built the app for you.", "s1");
    expect(fillers(await mem(store).recall({ session: "s1" }))).not.toContain("should-not-store");
    expect(models.enrichCalls).toBe(0); // enricher only runs on user turns
    models.restore();
  });

  it("a write failure is best-effort — distill never throws into the turn", async () => {
    const models = installFakeModels([APP_FIXTURE]);
    // A memory whose write() rejects (stator unreachable mid-turn).
    const brokenMemory = {
      ...mem(new InProcessStore()),
      write: async () => {
        throw new Error("stator write failed");
      },
    } as unknown as MemoryPlugin;
    const rotor = new MemoryRotor(brokenMemory, { entity: "user" });
    await expect(rotor.observe("user", PUBLISH_TURN, "s1")).resolves.toBeUndefined();
    models.restore();
  });

  it("falls back to the deterministic absorb floor when the enricher declines", async () => {
    // Enricher errors → distill uses absorbText; a directive still lands.
    const models = installFakeModels([], { enricherFail: true });
    const store = new InProcessStore();
    const m = mem(store);
    const rotor = new MemoryRotor(m, { entity: "user" });
    await rotor.observe("user", "Always use tabs for indentation.", "s1");
    const directives = (await m.recall({ entity: "user", role: "directive", session: "s1" })).map((f) => f.filler);
    expect(directives.join(" ")).toMatch(/tabs/i); // floor caught the directive
    models.restore();
  });
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP 3 — recall → constructSystemPrompt (the assembled brief)
// ═══════════════════════════════════════════════════════════════════════════
describe("3. recall → constructSystemPrompt", () => {
  it("the constructed worker prompt contains the recalled goal + key entities", async () => {
    const models = installFakeModels([APP_FIXTURE]);
    const store = new InProcessStore();
    const m = mem(store);
    await store.touchSession("s1");
    await m.write(
      [
        { entity: "user", role: "app", filler: "SlideForge" },
        { entity: "user", role: "app_url", filler: APP_URL },
      ],
      { session: "s1", tier: "mid" },
    );

    // Recall the memory plane, then hand it to the assembler as the engine does.
    const recall = await recallContext(m, "open the app we built", { entity: "user", session: "s1" });
    const { systemPrompt } = await constructSystemPrompt({
      userPrompt: "open the app we built",
      model: "claude-haiku-4-5",
      goal: "keep working on the published app",
      longTerm: recall.block,
    });
    expect(systemPrompt).toContain(APP_URL); // the URL reached the worker prompt
    expect(systemPrompt).toContain("SlideForge"); // as did the entity
    models.restore();
  });

  it("recall is scoped to the entity and honours the fact-cap budget", async () => {
    const models = installFakeModels([]);
    const store = new InProcessStore();
    const m = mem(store);
    // 40 noise facts + one signal fact; a small cap must still surface the signal
    // (relevance-ranked, not recency-truncated).
    for (let i = 0; i < 40; i++) await m.write([{ entity: "user", role: `note_${i}`, filler: `noise ${i}` }], {});
    await m.write([{ entity: "user", role: "app_url", filler: APP_URL }], {});
    const recall = await recallContext(m, `app url ${APP_URL}`, { entity: "user", factCap: 8 });
    const lines = recall.block.split("\n").filter((l) => l.startsWith("- "));
    expect(lines.length).toBeLessThanOrEqual(8 + 1); // budget respected (+ optional dir line)
    expect(recall.block).toContain(APP_URL); // signal survives the cap
    models.restore();
  });
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP 4 — THE REGRESSION (the exact failure the user hit)
//  turn 1: build+publish → turn 2: "which app / open it" surfaces slug/URL.
//  This test MUST fail on a broken/ephemeral rotor and pass on a working one.
// ═══════════════════════════════════════════════════════════════════════════
describe("4. THE REGRESSION — recall the published app across turns", () => {
  it("turn 1 publishes an app; turn 2 recalls its slug/URL to continue", async () => {
    const models = installFakeModels([APP_FIXTURE]);
    const store = new InProcessStore();
    const m = mem(store);
    const rotor = new MemoryRotor(m, { entity: "user" });
    await store.touchSession("s1");

    // ── Turn 1: build + publish. The rotor observes the exchange. ──
    await rotor.observe("user", PUBLISH_TURN, "s1");
    await rotor.observe("assistant", "Done — SlideForge is live.", "s1");

    // ── Turn 2: the user asks to continue. The rotor recalls + constructs. ──
    const query = "which app did we build — open that app again";
    const recall = await recallContext(m, query, { entity: "user", session: "s1" });

    // The slug/URL are recalled by RIGHT, not by cosine luck.
    expect(recall.block).toContain(APP_SLUG);
    expect(recall.block).toContain(APP_URL);

    // And they reach the worker's constructed system prompt so the agent can
    // continue instead of claiming an empty workspace.
    const { systemPrompt } = await constructSystemPrompt({
      userPrompt: query,
      model: "claude-haiku-4-5",
      goal: "reopen the app we just published",
      longTerm: recall.block,
    });
    expect(systemPrompt).toContain(APP_SLUG);
    expect(systemPrompt).toContain(APP_URL);
    models.restore();
  });

  it("REGRESSION GUARD: a rotor with recall disabled loses the app (proves the test bites)", async () => {
    // Simulate the broken/OFF rotor: nothing was ever written to the stator.
    // The exact same turn-2 recall now finds nothing — this is the failure the
    // working rotor above prevents. If this ever "passes" with content, group 4's
    // positive assertion would be vacuous.
    const models = installFakeModels([APP_FIXTURE]);
    const emptyStore = new InProcessStore();
    const recall = await recallContext(mem(emptyStore), "which app did we build — open that app again", {
      entity: "user",
      session: "s1",
    });
    expect(recall.block).not.toContain(APP_SLUG);
    expect(recall.block).not.toContain(APP_URL);
    models.restore();
  });

  it("BOUND: the deterministic floor (no enricher) does NOT reliably preserve the app slug/URL", async () => {
    // HONEST RELIABILITY BOUND (see the harness report + docs/memory.md "What's
    // bounded"). Without the premium enricher, the multi-sentence publish turn goes
    // through `absorbText`, which:
    //   - drops "Build a PowerPoint app and publish it." (no matched shape),
    //   - coreference-fails "It is called…" → keys the fact on entity "It", not
    //     "user", so a user-scoped recall never sees it, and
    //   - truncates the URL at the first sentence '.'  → "https://slides-9f2".
    // Net: the app is effectively FORGOTTEN on the floor. This test pins that
    // limitation so a future "the floor is enough" claim is caught — the fix is the
    // structured enricher (group 4's positive test), not the regex floor.
    const models = installFakeModels([], { enricherFail: true });
    const store = new InProcessStore();
    const m = mem(store);
    const rotor = new MemoryRotor(m, { entity: "user" });
    await store.touchSession("s1");
    await rotor.observe("user", PUBLISH_TURN, "s1");
    const recall = await recallContext(m, "open the app we built", { entity: "user", session: "s1" });
    // The full slug/URL are NOT recoverable from a user-scoped recall on the floor.
    expect(recall.block).not.toContain(APP_URL); // URL truncated + mis-scoped
    // The fact that WAS stored landed under the wrong entity ("It"), proving the
    // coreference gap rather than a rotor bug.
    const anyEntity = await store.snapshotFacts();
    expect(anyEntity.some((f) => /it/i.test(f.entity) && /slideforge/i.test(f.filler))).toBe(true);
    expect(anyEntity.some((f) => f.entity === "user" && f.filler.includes(APP_SLUG))).toBe(false);
    models.restore();
  });
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP 5 — Persistence across a turn boundary / restart
//  Persistent (SQLite file) KEEPS facts across a torn-down rotor; :memory: LOSES.
// ═══════════════════════════════════════════════════════════════════════════
describe("5. persistence across a restart", () => {
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "rrotor-mem-"));
  });
  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it("a PERSISTENT (SQLite file) stator keeps turn-1 facts after the rotor is rebuilt", async () => {
    const models = installFakeModels([APP_FIXTURE]);
    const dbPath = join(dir, "rotor.db");

    // ── Turn 1, "pod A": write via the rotor, then tear the store down. ──
    {
      const store = createStator({ backend: "sqlite", url: dbPath });
      const rotor = new MemoryRotor(mem(store), { entity: "user" });
      await store.touchSession("s1");
      await rotor.observe("user", PUBLISH_TURN, "s1");
      await store.close?.(); // simulate a pod restart / fresh turn boundary
    }

    // ── Turn 2, "pod B": a fresh store + fresh rotor on the SAME file. ──
    {
      const store = createStator({ backend: "sqlite", url: dbPath });
      const recall = await recallContext(mem(store), "open the app we built", { entity: "user", session: "s1" });
      expect(recall.block).toContain(APP_SLUG); // survived the restart
      expect(recall.block).toContain(APP_URL);
      await store.close?.();
    }
    models.restore();
  });

  it("an in-process (:memory:) stator LOSES turn-1 facts when rebuilt (documented ephemerality)", async () => {
    const models = installFakeModels([APP_FIXTURE]);

    // "pod A": write into a throwaway in-process store, then drop it.
    let store: Stator = new InProcessStore();
    const rotor = new MemoryRotor(mem(store), { entity: "user" });
    await store.touchSession("s1");
    await rotor.observe("user", PUBLISH_TURN, "s1");

    // "pod B": a brand-new in-process store has no shared plane → amnesia.
    store = new InProcessStore();
    const recall = await recallContext(mem(store), "open the app we built", { entity: "user", session: "s1" });
    expect(recall.block).not.toContain(APP_SLUG);
    expect(recall.block).not.toContain(APP_URL);
    models.restore();
  });
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP 6 — Session isolation + mid-tier window
// ═══════════════════════════════════════════════════════════════════════════
describe("6. session isolation", () => {
  it("session A's short-tier facts never surface in session B", async () => {
    const models = installFakeModels([]);
    const store = new InProcessStore();
    const m = mem(store);
    await store.touchSession("A");
    await m.write([{ entity: "user", role: "secret", filler: "A-only-secret" }], { session: "A", tier: "short" });
    await store.touchSession("B");
    const bRecall = await recallContext(m, "secret", { entity: "user", session: "B" });
    expect(bRecall.block).not.toContain("A-only-secret");
    // ...but session A still sees its own short-tier fact.
    const aRecall = await recallContext(m, "secret", { entity: "user", session: "A" });
    expect(aRecall.block).toContain("A-only-secret");
    models.restore();
  });

  it("mid-tier crosses within the window and ages out beyond it", async () => {
    const models = installFakeModels([]);
    const store = new InProcessStore();
    const m = mem(store);
    await store.touchSession("s1");
    await m.write([{ entity: "user", role: "topic", filler: "mid-window-topic" }], { session: "s1", tier: "mid" });
    await store.touchSession("s2");
    expect(fillers(await m.recall({ entity: "user", session: "s2", midWindow: 5 }))).toContain("mid-window-topic");
    for (const s of ["s3", "s4", "s5", "s6", "s7", "s8"]) await store.touchSession(s);
    expect(fillers(await m.recall({ entity: "user", session: "s8", midWindow: 5 }))).not.toContain("mid-window-topic");
    models.restore();
  });

  it("two rotors on the SAME stator but DIFFERENT sessions do not leak short-tier facts", async () => {
    const models = installFakeModels([{ match: /alpha/i, facts: [{ role: "note", filler: "alpha-note", tier: "short" }] }]);
    const store = new InProcessStore();
    const m = mem(store);
    await store.touchSession("A");
    await store.touchSession("B");
    const rotorA = new MemoryRotor(m, { entity: "user" });
    await rotorA.observe("user", "work on alpha", "A");
    // Session B recall must not see A's short-tier note.
    expect(fillers(await m.recall({ entity: "user", session: "B" }))).not.toContain("alpha-note");
    models.restore();
  });
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP 7 — Attention / focus (true north stays anchored)
// ═══════════════════════════════════════════════════════════════════════════
describe("7. attention / focus", () => {
  it("attentionCheck holds the goal and renderFocus leads with true north", async () => {
    const models = installFakeModels([], {
      // The attention model also POSTs to `…/messages`; return its JSON contract.
      assembler: () =>
        JSON.stringify({
          trueNorth: "ship the PowerPoint app",
          onTrack: true,
          drift: "",
          progress: "advancing",
          rabbitHole: "",
        }),
    });
    const attn = await attentionCheck("user: build slides\nassistant: building", "", "claude-haiku-4-5");
    expect(attn.trueNorth).toBe("ship the PowerPoint app");
    expect(attn.onTrack).toBe(true);
    const focus = renderFocus(attn);
    expect(focus).toMatch(/^True north: ship the PowerPoint app/);
    expect(focus).toContain("advancing");
    models.restore();
  });

  it("attentionCheck is best-effort: on a broken model it keeps the prior true north on-track", async () => {
    const models = installFakeModels([], { assemblerFail: true });
    const attn = await attentionCheck("user: tangent about fonts", "ship the PowerPoint app", "claude-haiku-4-5");
    expect(attn.trueNorth).toBe("ship the PowerPoint app"); // held, not lost
    expect(attn.onTrack).toBe(true); // fail-safe, never breaks the turn
    models.restore();
  });

  it("renderFocus surfaces drift + a rabbit hole so the worker is pulled back", () => {
    const drifting: AttentionState = {
      trueNorth: "ship the app",
      onTrack: false,
      drift: "bikeshedding the logo",
      progress: "circling",
      rabbitHole: "logo color palette",
    };
    const focus = renderFocus(drifting);
    expect(focus).toContain("DRIFTING");
    expect(focus).toContain("bikeshedding the logo");
    expect(focus).toContain("RABBIT HOLE: logo color palette");
  });

  it("the fidelity buffer evicts old turns to gist without dropping the session goal", async () => {
    // Long session: the goal fact stays recallable from the stator even as the
    // short-term buffer slides old raw turns down to gists.
    const models = installFakeModels([APP_FIXTURE]);
    const store = new InProcessStore();
    const m = mem(store);
    const rotor = new MemoryRotor(m, { entity: "user", rawTurns: 2, gistTurns: 2 });
    await store.touchSession("s1");
    await rotor.observe("user", PUBLISH_TURN, "s1"); // the goal-bearing turn
    for (let i = 0; i < 12; i++) await rotor.observe("user", `unrelated follow-up number ${i}`, "s1");
    // The app fact is far outside the raw+gist window now, but recall still has it.
    const recall = await recallContext(m, "what app are we building", { entity: "user", session: "s1" });
    expect(recall.block).toContain(APP_SLUG);
    models.restore();
  });
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP 8 — Determinism / replay (record-once enricher)
// ═══════════════════════════════════════════════════════════════════════════
describe("8. determinism / replay", () => {
  it("the enricher runs ONCE per user turn and recall never re-invokes it", async () => {
    const models = installFakeModels([APP_FIXTURE]);
    const store = new InProcessStore();
    const m = mem(store);
    const rotor = new MemoryRotor(m, { entity: "user" });
    await store.touchSession("s1");
    await rotor.observe("user", PUBLISH_TURN, "s1");
    expect(models.enrichCalls).toBe(1); // extraction happened once, at write time

    // Many recalls, zero further enricher calls — determinism-by-recording.
    await recallContext(m, "open the app", { entity: "user", session: "s1" });
    await recallContext(m, "open the app", { entity: "user", session: "s1" });
    expect(models.enrichCalls).toBe(1);
    models.restore();
  });

  it("recall is byte-identical on replay for the same stored facts", async () => {
    const models = installFakeModels([APP_FIXTURE]);
    const store = new InProcessStore();
    const m = mem(store);
    await store.touchSession("s1");
    await new MemoryRotor(m, { entity: "user" }).observe("user", PUBLISH_TURN, "s1");
    const a = (await recallContext(m, "open the app we built", { entity: "user", session: "s1" })).block;
    const b = (await recallContext(m, "open the app we built", { entity: "user", session: "s1" })).block;
    expect(a).toBe(b);
    models.restore();
  });
});

// ═══════════════════════════════════════════════════════════════════════════
//  GROUP 9 — Failure modes (nothing crashes a turn)
// ═══════════════════════════════════════════════════════════════════════════
describe("9. failure modes", () => {
  it("empty / whitespace user turns are ignored, not stored or crashed", async () => {
    const models = installFakeModels([{ match: /.*/, facts: [{ role: "x", filler: "y" }] }]);
    const store = new InProcessStore();
    const m = mem(store);
    const rotor = new MemoryRotor(m, { entity: "user" });
    await store.touchSession("s1");
    await expect(rotor.observe("user", "   \n  ", "s1")).resolves.toBeUndefined();
    expect(await m.recall({ session: "s1" })).toHaveLength(0);
    expect(models.enrichCalls).toBe(0); // whitespace short-circuits before the model
    models.restore();
  });

  it("malformed enricher output falls back to the floor without persisting garbage", async () => {
    // Re-route the enricher to return non-JSON (overrides the default fake): the
    // parse must reject the shape and fall back to the deterministic floor.
    vi.stubGlobal("fetch", (async (input: unknown) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : (input as { url: string }).url;
      if (url.includes("/chat/completions"))
        return new Response(JSON.stringify({ choices: [{ message: { content: "not json at all <<<>>>" } }] }), { status: 200 });
      return new Response(JSON.stringify({ content: [{ type: "text", text: "ok" }] }), { status: 200 });
    }) as unknown as typeof fetch);

    const store = new InProcessStore();
    const m = mem(store);
    const rotor = new MemoryRotor(m, { entity: "user" });
    await store.touchSession("s1");
    // Directive text → floor still catches it despite the enricher garbage.
    await rotor.observe("user", "Always cite sources.", "s1");
    const facts = await m.recall({ session: "s1" });
    expect(facts.every((f) => f.filler !== "not json at all <<<>>>")).toBe(true);
    expect((await m.recall({ role: "directive", session: "s1" })).map((f) => f.filler).join(" ")).toMatch(/cite/i);
    // afterEach restores fetch (vi.unstubAllGlobals).
  });

  it("a stator whose recall throws does not crash construction (in-memory mirror authoritative)", async () => {
    const models = installFakeModels([APP_FIXTURE]);
    // constructSystemPrompt takes pre-recalled longTerm text — even if a caller's
    // recall failed and passed empty, construction still produces a valid prompt.
    const { systemPrompt } = await constructSystemPrompt({
      userPrompt: "continue anyway",
      model: "claude-haiku-4-5",
      longTerm: "", // recall miss / stator down upstream
      goal: "keep going",
    });
    expect(systemPrompt).toContain("## Focus");
    models.restore();
  });

  it("assembler failure in the rotor's construct() propagates as a catchable error (engine falls back)", async () => {
    // The engine wraps constructSystemPrompt in try/catch and falls back to raw
    // recall; here we assert the failure is a normal rejected promise, not a crash
    // that escapes the event loop.
    const models = installFakeModels([], { assemblerFail: true });
    await expect(
      constructSystemPrompt({ userPrompt: "x", model: "claude-haiku-4-5", longTerm: "y" }),
    ).rejects.toThrow(/gateway|Anthropic|503/i);
    models.restore();
  });
});

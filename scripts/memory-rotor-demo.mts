/**
 * Demo: watch the memory rotor build a worker system prompt from a scripted
 * coding conversation. Run:
 *   MEMORY_ROTOR_MODEL_ID=claude-haiku-4-5 \
 *   npx tsx scripts/memory-rotor-demo.mts
 * (needs ANTHROPIC_API_KEY, or MEMORY_ROTOR_MODEL_URL/ID/API_KEY for another host.)
 */
import { BasicMemory } from "../src/plugins/memory.js";
import { InProcessStore, embedderFromEnv } from "../src/index.js";
import { MemoryRotor } from "../src/harness/memory-rotor.js";

const STANDARDS = `- All network/DB I/O wrapped in try/catch/finally; structured logging with secret redaction.
- No files over ~300 lines; split by concern.
- Never use confirm()/alert(); use the app modal.
- Deploys go through CI, never a direct push.`;

// A scripted multi-turn coding session (user turns get distilled to facts).
const convo: Array<{ role: string; text: string }> = [
  { role: "user", text: "We're building the auth service in packages/auth. Always use the AuthedHttp wrapper for calls; on a 401 it force-refreshes and retries once." },
  { role: "assistant", text: "Got it — AuthedHttp for all calls, 401 → forced refresh + single retry." },
  { role: "user", text: "Important: I hate silent fallbacks. If a token refresh fails, surface the error to the user, never fall back to a cached session." },
  { role: "assistant", text: "Understood. Refresh failure surfaces to the user; no cached-session fallback." },
  { role: "user", text: "The session refresh lives in packages/auth/src/session.ts, function refreshSession(). It's been flaky under concurrency." },
  { role: "assistant", text: "Noted — refreshSession() in session.ts, flaky under concurrency." },
];

async function main() {
  // A metering sink stands in for the host's credit pipeline: every memory-rotor
  // call lands here with tokens + model + session for pricing/attribution.
  const meter: Array<{ phase: string; model: string; inputTokens: number; outputTokens: number; sessionId?: string }> = [];

  const memory = new BasicMemory(new InProcessStore(), embedderFromEnv());
  const rotor = new MemoryRotor(memory, {
    standards: STANDARDS,
    rawTurns: 2,
    gistTurns: 4,
    sessionId: "sess-1",
    onUsage: (u) => meter.push(u),
  });

  // Preload the prior conversation as history (back-path distillation).
  for (const t of convo) await rotor.observe(t.role, t.text, "sess-1");

  const prompts = [
    "Add a mutex around refreshSession so concurrent 401s don't trigger duplicate refreshes.",
    "Now add a unit test proving two concurrent 401s cause exactly one refresh.",
  ];

  for (let i = 0; i < prompts.length; i++) {
    const p = prompts[i]!;
    console.log(`\n${"═".repeat(72)}\nTURN ${i + 1}  ·  user (passed to worker verbatim): ${p}`);

    // FOREGROUND — no assembler call when warm.
    const t0 = Date.now();
    const r = await rotor.next(p, { session: "sess-1" });
    console.log(`  foreground next(): warm=${r.warm}  ${Date.now() - t0}ms  ` +
      (r.warm ? "← served from cache, ZERO assembler tokens in path" : "← cold (first turn): fell back to a live assemble"));
    console.log(`  → worker system prompt ${r.systemPrompt.length} chars (recall delta ${r.recallDelta.length} chars)`);

    // The worker (Fable) would run here on r.systemPrompt + the verbatim prompt. Mock it:
    const reply = `(worker: implemented "${p.slice(0, 44)}…")`;

    // BACK PATH — the metered assembler burn happens HERE, off the user's clock.
    const w0 = Date.now();
    const u = await rotor.warm(reply, { session: "sess-1" });
    console.log(`  back-path warm(): ${Date.now() - w0}ms  burned ${u.inputTokens}+${u.outputTokens} tok on ${u.model}  [metered]`);
  }

  console.log(`\n${"─".repeat(72)}\nMETERING LEDGER (host prices each line via the model's rate card):`);
  for (const m of meter) console.log("  " + JSON.stringify(m));
  const warm = meter.filter((m) => m.phase === "warm").length;
  const cold = meter.filter((m) => m.phase === "construct").length;
  console.log(`\n${meter.length} assembler calls: ${warm} warm (back-path, off-clock) + ${cold} construct (cold, in-path).`);
  console.log(`Every live turn after the first was served WARM — the user never waited on the assembler.`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});

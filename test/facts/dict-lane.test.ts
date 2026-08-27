/**
 * The dict lane's GATE TEST (docs/dict-vector.md): the abc-forms needle.
 *
 * Plant one standing rule — "always use abc for forms" — as a directive
 * (rule layer, applies_to ui.forms) among a bed of distractor facts. Then:
 *   - 5 LITERAL form exchanges (share the word) → rule must be in the block
 *   - 5 PARAPHRASE form exchanges (no shared word) → the gloss bridge earns it
 *   - 10 UNRELATED exchanges → the rule must stay OUT (targeted rules only
 *     fire on their activity; untriggered directives never crowd the block)
 *
 * Plus: preserved off-schema content is selectable by its own words, and a
 * trigger-less directive rides every turn. All deterministic — no model.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { PGlite } from "@electric-sql/pglite";
import type { PgLike } from "../../src/exec/pgvector-store.js";
import type { Principal } from "../../src/auth/introspect.js";
import { GlyphStore } from "../../src/facts/store.js";
import { buildFactsServer, setFactsStoreForTests, renderFactBlock } from "../../src/facts/server.js";
import { tokenize, idfWeight, dictVector, dictCosine } from "../../src/facts/dict-lane.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";

const ORG: Principal = { orgId: "org-dict", userId: "user-1" } as Principal;

let db: PGlite;
let glyphStore: GlyphStore;
let client: Client;

async function connect(p: Principal): Promise<Client> {
  const server = buildFactsServer(p);
  const [ct, st] = InMemoryTransport.createLinkedPair();
  const c = new Client({ name: "t", version: "0" });
  await Promise.all([c.connect(ct), server.connect(st)]);
  return c;
}

async function create(name: string, facts: unknown, confidence = 0.8): Promise<void> {
  const r = await client.callTool({ name: "create_fact", arguments: { name, facts, confidence } });
  const first = (r.content as Array<{ text: string }>)[0]!.text;
  if (first.startsWith("ERROR")) throw new Error(first);
}

/** Advance the epoch: clear the derived indexes so the next render rehydrates
 *  against the current ledger + lexicon (hydration IS the epoch boundary). */
function advanceEpoch(): void {
  setFactsStoreForTests(Promise.resolve(glyphStore));
}

beforeAll(async () => {
  db = new PGlite();
  glyphStore = await GlyphStore.create({ client: db as unknown as PgLike });
  setFactsStoreForTests(Promise.resolve(glyphStore));
  client = await connect(ORG);

  // A realistic lexicon floor: common words counted heavily so IDF separates
  // them from rare anchors like "abc".
  const commonCorpus =
    "the team will use and do the work for the new project and the people know what they want " +
    "to do and use for the good of the team project work time day week";
  for (let i = 0; i < 40; i++) await glyphStore.bumpLexicon(ORG, tokenize(commonCorpus));

  // THE RULE: always use abc for forms — targeted at ui.forms.
  await create("form-library-rule", {
    entity: { name: "form library standard", kind: "rule" },
    rule: { action: "must-use", object: "abc", applies_to: "ui.forms" },
    epistemic: { certainty: "KNOW", modality: "MUST" },
  }, 0.95);

  // A trigger-less directive — rides every turn.
  await create("tone-rule", {
    entity: { name: "writing tone", kind: "rule" },
    rule: { action: "prefer", object: "plain direct language" },
  }, 0.9);

  // Distractor bed across other domains.
  const distractors: Array<[string, unknown]> = [
    ["deploy-window", { entity: { name: "deploy window" }, temporal: { time: "second Tuesday" }, relational: { object: "release train" } }],
    ["db-standard", { entity: { name: "database standard" }, relational: { object: "postgres" }, quantitative: { count: "16" } }],
    ["oncall-alias", { entity: { name: "oncall alias" }, relational: { object: "falcon-ops" } }],
    ["budget-cap", { entity: { name: "infra budget" }, quantitative: { magnitude: "1800", unit: "dollars monthly" } }],
    ["retro-cadence", { entity: { name: "retro cadence" }, temporal: { frequency: "every second week" } }],
    ["customer-champion", { entity: { name: "pilot customer" }, relational: { object: "nordwind analytics" } }],
    ["email-signoff", { entity: { name: "email signoff" }, relational: { object: "warm regards" } }],
    ["meeting-length", { entity: { name: "standup length" }, quantitative: { magnitude: "15", unit: "minutes" } }],
    ["repo-owner", { entity: { name: "platform repo owner" }, relational: { possessor: "Lena Fischer" } }],
    ["test-timeout", { entity: { name: "heavy test timeout" }, quantitative: { magnitude: "20", unit: "seconds" } }],
    ["region-rule", { entity: { name: "deploy region" }, spatial: { location: "eu-central-1" } }],
    ["license-choice", { entity: { name: "oss license" }, relational: { object: "apache 2" } }],
    ["chat-channel", { entity: { name: "escalation channel" }, relational: { object: "beacon-ops" } }],
    ["secret-project", { entity: { name: "SMOKE project" }, relational: { codename: "nightjar-7", owner: "Priya Chen" } }],
  ];
  for (const [name, facts] of distractors) await create(name, facts);

  advanceEpoch();
}, 60_000);

afterAll(async () => { await db.close(); });

const LITERAL_FORM_TASKS = [
  "build me a signup form with validation and a submit button",
  "add a contact form to the landing page",
  "the checkout form needs an address field",
  "fix the bug where the form clears on submit",
  "make the feedback form fields required",
];

const PARAPHRASE_FORM_TASKS = [
  "build the signup flow with input validation",
  "add a registration screen where users enter their details",
  "the data entry fields on checkout need work",
  "wire up the login inputs and the submit handler",
  "create a questionnaire for onboarding with dropdowns",
];

const UNRELATED_TASKS = [
  "ship the release to production this afternoon",
  "write the migration for the new postgres table",
  "draft the quarterly budget email to finance",
  "schedule the retro and send the calendar invite",
  "investigate the latency alert from last night",
  "review the pull request for the api refactor",
  "rotate the leaked credential and audit access",
  "summarize yesterdays standup notes for the team",
  "negotiate the contract renewal with the vendor",
  "plan the sprint backlog for next week",
];

describe("dict-lane — the abc-forms needle", () => {
  it("LITERAL form tasks: the rule is in the block, 5/5 (atoms)", async () => {
    for (const task of LITERAL_FORM_TASKS) {
      const block = await renderFactBlock(ORG, task);
      expect(block, `missed on: ${task}`).toContain("abc");
    }
  });

  it("PARAPHRASE form tasks: the rule is in the block, 5/5 (gloss bridge)", async () => {
    for (const task of PARAPHRASE_FORM_TASKS) {
      const block = await renderFactBlock(ORG, task);
      expect(block, `missed on: ${task}`).toContain("abc");
    }
  });

  it("UNRELATED tasks: the targeted rule stays out, 10/10", async () => {
    for (const task of UNRELATED_TASKS) {
      const block = await renderFactBlock(ORG, task);
      expect(block, `leaked on: ${task}`).not.toContain("must-use=abc");
    }
  });

  it("a trigger-less directive rides every turn", async () => {
    for (const task of [...UNRELATED_TASKS.slice(0, 3), LITERAL_FORM_TASKS[0]!]) {
      const block = await renderFactBlock(ORG, task);
      expect(block, `tone rule missing on: ${task}`).toContain("plain direct language");
    }
  });

  it("preserved off-schema content is selectable by its own words", async () => {
    const block = await renderFactBlock(ORG, "what was the codename for the nightjar effort and who owns it?");
    expect(block).toContain("nightjar-7");
  });

  it("selection is deterministic within an epoch", async () => {
    const a = await renderFactBlock(ORG, LITERAL_FORM_TASKS[0]!);
    const b = await renderFactBlock(ORG, LITERAL_FORM_TASKS[0]!);
    expect(a).toBe(b);
  });
});

describe("dict-lane — units", () => {
  it("idf is monotone-decreasing and floors sensibly", () => {
    expect(idfWeight(0)).toBe(1);
    expect(idfWeight(10)).toBeLessThan(idfWeight(1));
    expect(idfWeight(1000)).toBeLessThan(0.15);
  });

  it("rare anchors dominate common words in similarity", () => {
    const lex = new Map([["use", 5000], ["the", 9000], ["abc", 1]]);
    const rule = dictVector({ words: tokenize("always use abc for the forms") }, lex);
    const anchored = dictVector({ words: tokenize("abc") }, lex);
    const commons = dictVector({ words: tokenize("use the") }, lex);
    expect(dictCosine(rule, anchored)).toBeGreaterThan(dictCosine(rule, commons));
  });
});

/**
 * oracle.test.ts — the TS glyph port vs the VENDORED PYTHON CANON.
 *
 * The canon (glyphh/glyphh — ada @ 041c0a20 + runtime @ 011f0b3, verbatim)
 * is the referee: test/glyph/oracle.py emits its ground truth and every
 * assertion here demands the port reproduce it — atoms and cortices to the
 * BYTE, config JSON and space ids to the CHARACTER, fact-tree JSON and text
 * renderings exactly. This is what makes src/glyph a faithful port rather
 * than a reimplementation. Skips loudly if python3/numpy are unavailable.
 */

import { describe, it, expect, beforeAll } from "vitest";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { generateSymbol, bind, bundle, cosineSimilarity } from "../../src/glyph/ops.js";
import { GlyphEncoder } from "../../src/glyph/encoder.js";
import { EncoderConfig } from "../../src/glyph/config.js";
import { concept } from "../../src/glyph/types.js";
import { FactTree } from "../../src/glyph/fact-tree.js";

const here = dirname(fileURLToPath(import.meta.url));

const b64 = (v: Int8Array): string => Buffer.from(v.buffer, v.byteOffset, v.byteLength).toString("base64");

interface Oracle {
  atoms: Record<string, string>;
  ops: { bind_ab: string; bundle_abc: string; cos_ab: number };
  default_config_json: string;
  default_space_id: string;
  explicit_config_json: string;
  explicit_space_id: string;
  legacy_glyph: {
    space_id: string; identifier_prefix: string; version: string; global_cortex: string;
    layers: Record<string, { cortex: string; segments: Record<string, { cortex: string; roles: Record<string, string> }> }>;
  };
  temporal_glyph: { identifier: string; global_cortex: string; temporal_cortex: string };
  weighted_glyph: { identifier_prefix: string; global_cortex: string };
  fact_tree_json: unknown;
  fact_tree_text: string;
}

let oracle: Oracle | null = null;

beforeAll(() => {
  try {
    const raw = execFileSync("python3", [join(here, "oracle.py")], { maxBuffer: 64 * 1024 * 1024, timeout: 120_000 });
    oracle = JSON.parse(raw.toString()) as Oracle;
  } catch (err) {
    // A missing python3/numpy must FAIL the suite, not skip silently — the
    // port's whole claim to faithfulness is this referee.
    throw new Error(`glyph oracle unavailable — python3 + numpy are required to verify the port: ${(err as Error).message}`);
  }
}, 120_000);

describe("glyph port vs the Python canon (byte parity)", () => {
  it("atoms are byte-identical", () => {
    for (const [key, expected] of Object.entries(oracle!.atoms)) {
      expect(b64(generateSymbol(42, key, 256)), `atom '${key}'`).toBe(expected);
    }
  });

  it("bind / bundle / cosine agree exactly", () => {
    const a = generateSymbol(42, "a", 256), b = generateSymbol(42, "b", 256), c = generateSymbol(42, "c", 256);
    expect(b64(bind(a, b))).toBe(oracle!.ops.bind_ab);
    expect(b64(bundle([a, b, c]))).toBe(oracle!.ops.bundle_abc);
    expect(cosineSimilarity(a, b)).toBeCloseTo(oracle!.ops.cos_ab, 12);
  });

  it("default config JSON and space id match to the character", () => {
    const cfg = new EncoderConfig({ dimension: 256, seed: 42 });
    expect(cfg.toJson()).toBe(oracle!.default_config_json);
    expect(new GlyphEncoder(cfg).spaceId).toBe(oracle!.default_space_id);
  });

  it("explicit-layer config JSON and space id match to the character", () => {
    const cfg = new EncoderConfig({
      dimension: 256, seed: 42,
      layers: [{
        name: "semantic", similarityWeight: 1.0, securityWeight: 1.0,
        segments: [{
          name: "attributes", similarityWeight: 1.0, securityWeight: 1.0,
          roles: [
            { name: "kind", keyPart: true, similarityWeight: 1.0, securityWeight: 1.0 },
            { name: "subject", keyPart: false, similarityWeight: 1.0, securityWeight: 1.0 },
            { name: "value", keyPart: false, similarityWeight: 0.5, securityWeight: 1.0 },
          ],
        }],
      }],
    });
    expect(cfg.toJson()).toBe(oracle!.explicit_config_json);
    expect(new GlyphEncoder(cfg).spaceId).toBe(oracle!.explicit_space_id);
  });

  it("legacy encode reproduces every cortex at every level, byte for byte", () => {
    const enc = new GlyphEncoder(new EncoderConfig({ dimension: 256, seed: 42, includeTemporal: false }));
    const g = enc.encode(concept({
      name: "deploy rule",
      attributes: { kind: "preference", subject: "deploys", value: "through ci" },
      relationships: [["holder", "chris"], ["applies_to", "server"]],
      metadata: { domain: "ops" },
    }));
    const o = oracle!.legacy_glyph;
    expect(g.spaceId).toBe(o.space_id);
    expect(g.identifier.split("@")[0]).toBe(o.identifier_prefix);
    expect(g.version).toBe(o.version);
    expect(b64(g.globalCortex.data)).toBe(o.global_cortex);
    expect(Object.keys(g.layers).sort()).toEqual(Object.keys(o.layers).sort());
    for (const [ln, ol] of Object.entries(o.layers)) {
      const layer = g.layers[ln]!;
      expect(b64(layer.cortex.data), `layer '${ln}' cortex`).toBe(ol.cortex);
      for (const [sn, os] of Object.entries(ol.segments)) {
        const seg = layer.segments[sn]!;
        expect(b64(seg.cortex.data), `segment '${ln}.${sn}' cortex`).toBe(os.cortex);
        for (const [rn, orv] of Object.entries(os.roles)) {
          expect(b64(seg.roles[rn]!.data), `role '${ln}.${sn}.${rn}'`).toBe(orv);
        }
      }
    }
  });

  it("the temporal layer pinned through a source role matches, identifier included", () => {
    const enc = new GlyphEncoder(new EncoderConfig({
      dimension: 256, seed: 42, temporalSource: "semantic.attributes.when",
      temporalConfig: { signalType: "sequence" },
    }));
    const g = enc.encode(concept({ name: "t", attributes: { kind: "event", when: "2026-01-01T00:00:00" } }));
    const o = oracle!.temporal_glyph;
    expect(g.identifier).toBe(o.identifier);
    expect(b64(g.globalCortex.data)).toBe(o.global_cortex);
    expect(b64(g.layers._temporal!.cortex.data)).toBe(o.temporal_cortex);
  });

  it("weight-baked explicit encoding matches", () => {
    const enc = new GlyphEncoder(new EncoderConfig({
      dimension: 256, seed: 42, includeTemporal: false, applyWeightsDuringEncoding: true,
      layers: [{
        name: "semantic", similarityWeight: 1.0, securityWeight: 1.0,
        segments: [{
          name: "attributes", similarityWeight: 1.0, securityWeight: 1.0,
          roles: [
            { name: "kind", keyPart: true, similarityWeight: 1.0, securityWeight: 1.0 },
            { name: "subject", keyPart: false, similarityWeight: 0.25, securityWeight: 1.0 },
          ],
        }],
      }],
    }));
    const g = enc.encode(concept({ name: "w", attributes: { kind: "k1", subject: "s1" } }));
    expect(g.identifier.split("@")[0]).toBe(oracle!.weighted_glyph.identifier_prefix);
    expect(b64(g.globalCortex.data)).toBe(oracle!.weighted_glyph.global_cortex);
  });

  it("fact tree JSON and text renderings match exactly", () => {
    const t = new FactTree();
    t.addFact({
      path: ["computation", "raw_similarity"],
      description: "Raw Similarity",
      value: 0.87,
      citations: [{
        glyphId: "car_red@2024-01-15T10:30:00Z#v1", component: "cortex",
        timestamp: "2024-01-15T10:30:00", version: "v1", dataHash: "a3f2e8b1",
      }],
      dataSample: { dot_product: 8700, dimensions_compared: 256 },
      mathExplanation: "cos(v1, v2) = (v1 . v2) / (||v1|| * ||v2||)",
    });
    t.addFact({ path: ["computation", "verdict"], description: "Verdict", value: "same fact" });
    expect(t.toJson()).toEqual(oracle!.fact_tree_json);
    expect(t.toText()).toBe(oracle!.fact_tree_text);
  });
});

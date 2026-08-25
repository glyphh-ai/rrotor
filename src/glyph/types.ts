/**
 * types.ts — the canonical Glyph data structures. FAITHFUL PORT of
 * `glyphh/core/types.py` (glyphh-ai/ada @ 041c0a20; vendored reference in
 * ../../glyphh/glyphh/ — the oracle suite runs this port against it).
 *
 * The hierarchy, verbatim from the canon:
 *   role bindings → (bundle) → Segment cortex
 *   segment cortices → (bundle) → Layer cortex
 *   layer cortices → (bundle) → global cortex
 * and a Glyph's identity is the composite `primary_key@timestamp#version` —
 * an id'd, timestamped, versioned record: the auditable history unit.
 */

import { createHash } from "node:crypto";
import type { Bipolar } from "./ops.js";

export class GlyphVector {
  readonly data: Bipolar;
  readonly dimension: number;
  readonly spaceId: string;

  constructor(data: Bipolar, dimension: number, spaceId: string) {
    for (let i = 0; i < data.length; i++) {
      const v = data[i]!;
      if (v !== 1 && v !== -1) {
        throw new Error(`Vector must be bipolar (values in {-1, +1}). Found invalid value: ${v}`);
      }
    }
    if (data.length !== dimension) {
      throw new Error(`Dimension mismatch: expected ${dimension}, got ${data.length}`);
    }
    this.data = data;
    this.dimension = dimension;
    this.spaceId = spaceId;
  }

  equals(other: GlyphVector): boolean {
    if (this.spaceId !== other.spaceId || this.dimension !== other.dimension) return false;
    for (let i = 0; i < this.data.length; i++) if (this.data[i] !== other.data[i]) return false;
    return true;
  }
}

export interface Concept {
  name: string;
  attributes: Record<string, unknown>;
  relationships: Array<[string, string]>;
  metadata: Record<string, unknown>;
}

export function concept(input: {
  name: string;
  attributes?: Record<string, unknown>;
  relationships?: Array<[string, string]>;
  metadata?: Record<string, unknown>;
}): Concept {
  return {
    name: input.name,
    attributes: input.attributes ?? {},
    relationships: input.relationships ?? [],
    metadata: input.metadata ?? {},
  };
}

export interface Segment {
  name: string;
  cortex: GlyphVector;
  roles: Record<string, GlyphVector>;
  roleValues: Record<string, unknown>;
  weights: Record<string, number>;
}

export interface Layer {
  name: string;
  cortex: GlyphVector;
  segments: Record<string, Segment>;
  weights: Record<string, number>;
}

export interface Glyph {
  /** Composite identifier: `primary_key@timestamp#version`. */
  identifier: string;
  name: string;
  spaceId: string;
  globalCortex: GlyphVector;
  layers: Record<string, Layer>;
  securityLevels: Record<string, number>;
  metadata: Record<string, unknown>;
  /** ISO timestamp (the canon stores a datetime; the port keeps the ISO form
   *  it stamps into the identifier — one clock, one truth). */
  timestamp: string;
  version: string;
}

/** The canon's Glyph.__post_init__ validations, verbatim in spirit. */
export function validateGlyph(g: Glyph): void {
  if (!g.identifier.includes("@") || !g.identifier.includes("#")) {
    throw new Error(`Invalid identifier format: ${g.identifier}. Expected format: primary_key@timestamp#version`);
  }
  if (g.globalCortex.spaceId !== g.spaceId) {
    throw new Error(`Space ID mismatch: glyph space_id=${g.spaceId}, global_cortex space_id=${g.globalCortex.spaceId}`);
  }
  for (const [layerName, layer] of Object.entries(g.layers)) {
    if (layer.cortex.spaceId !== g.spaceId) {
      throw new Error(`Space ID mismatch in layer '${layerName}': expected ${g.spaceId}, got ${layer.cortex.spaceId}`);
    }
    for (const [segName, seg] of Object.entries(layer.segments)) {
      if (seg.cortex.spaceId !== g.spaceId) {
        throw new Error(`Space ID mismatch in segment '${segName}' of layer '${layerName}': expected ${g.spaceId}, got ${seg.cortex.spaceId}`);
      }
      for (const [roleName, roleVec] of Object.entries(seg.roles)) {
        if (roleVec.spaceId !== g.spaceId) {
          throw new Error(`Space ID mismatch in role '${roleName}' of segment '${segName}' in layer '${layerName}': expected ${g.spaceId}, got ${roleVec.spaceId}`);
        }
      }
    }
  }
}

/** compute_space_id, verbatim: sha256(`${dim}:${seed}:${configJson}`)[:16]. */
export function computeSpaceId(dimension: number, seed: number, configJson: string): string {
  return createHash("sha256").update(`${dimension}:${seed}:${configJson}`, "utf8").digest("hex").slice(0, 16);
}

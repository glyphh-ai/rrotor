/**
 * encoder.ts — the strict-contract Glyph encoder. FAITHFUL PORT of
 * `glyphh/encoder/base.py`'s encode surface (glyphh-ai/ada @ 041c0a20):
 * symbol atoms (cached, byte-identical to numpy via ops.generateSymbol),
 * bind/bundle/weightedBundle with full validation, segment → layer → global
 * cortex assembly on BOTH paths (legacy default and explicit layer config),
 * the always-on `_temporal` layer, and the composite identifier
 * `primary_key@temporal#v1`.
 *
 * DEFERRED with loud errors (see config.ts): numeric/continuous encodings and
 * bag-of-words text roles — the NSM fact substrate is symbolic by design.
 */

import { bind as bindOp, bundle as bundleOp, generateSymbol } from "./ops.js";
import {
  GlyphVector, computeSpaceId, validateGlyph,
  type Concept, type Glyph, type Layer, type Segment,
} from "./types.js";
import {
  EncoderConfig, TEMPORAL_LAYER_NAME, TEMPORAL_SEGMENT_NAME, TEMPORAL_ROLE_NAME,
} from "./config.js";

export interface LegacyLayerSpec {
  name: string;
  segments: Array<{ name: string; source: "attributes" | "relationships" }>;
}

/** The canon's default legacy layer config, verbatim. */
const DEFAULT_LEGACY: { layers: LegacyLayerSpec[] } = {
  layers: [
    {
      name: "semantic",
      segments: [
        { name: "attributes", source: "attributes" },
        { name: "relations", source: "relationships" },
      ],
    },
  ],
};

export class GlyphEncoder {
  readonly dimension: number;
  readonly seed: number;
  readonly config: EncoderConfig;
  readonly spaceId: string;
  private readonly symbolCache = new Map<string, GlyphVector>();
  /** Injectable clock so a caller (and the oracle) can pin the timestamp. */
  now: () => Date = () => new Date();

  constructor(config: EncoderConfig) {
    this.dimension = config.dimension;
    this.seed = config.seed;
    this.config = config;
    this.spaceId = computeSpaceId(this.dimension, this.seed, config.toJson());
  }

  generateSymbol(key: string): GlyphVector {
    const cached = this.symbolCache.get(key);
    if (cached) return cached;
    const vector = new GlyphVector(generateSymbol(this.seed, key, this.dimension), this.dimension, this.spaceId);
    this.symbolCache.set(key, vector);
    return vector;
  }

  private validateVector(v: GlyphVector): void {
    if (v.dimension !== this.dimension) {
      throw new Error(`Vector dimension must match encoder dimension (expected ${this.dimension}, got ${v.dimension})`);
    }
    if (v.spaceId !== this.spaceId) {
      throw new Error(`Vector must belong to the same vector space as encoder (expected ${this.spaceId}, got ${v.spaceId})`);
    }
  }

  bind(role: GlyphVector, value: GlyphVector): GlyphVector {
    this.validateVector(role);
    this.validateVector(value);
    return new GlyphVector(bindOp(role.data, value.data), this.dimension, this.spaceId);
  }

  bundle(vectors: GlyphVector[], weights?: number[]): GlyphVector {
    if (!vectors.length) throw new Error("Cannot bundle empty vector list");
    for (const v of vectors) this.validateVector(v);
    if (weights) return this.weightedBundle(vectors.map((v, i) => [v, weights[i] ?? 1.0]));
    return new GlyphVector(bundleOp(vectors.map((v) => v.data)), this.dimension, this.spaceId);
  }

  /** Weighted majority vote: sum(vec[i] * w) per dimension; >= 0 → +1. */
  weightedBundle(weighted: Array<[GlyphVector, number]>): GlyphVector {
    if (!weighted.length) throw new Error("Cannot bundle empty vector list");
    for (const [v] of weighted) this.validateVector(v);
    const sums = new Float32Array(this.dimension);
    for (const [v, w] of weighted) {
      const fw = Math.fround(w);
      for (let i = 0; i < this.dimension; i++) sums[i] = Math.fround(sums[i]! + Math.fround(v.data[i]! * fw));
    }
    const out = new Int8Array(this.dimension);
    for (let i = 0; i < this.dimension; i++) out[i] = sums[i]! >= 0 ? 1 : -1;
    return new GlyphVector(out, this.dimension, this.spaceId);
  }

  encodeSegment(segmentName: string, attributes: Record<string, unknown>, weights?: Record<string, number>): Segment {
    const keys = Object.keys(attributes);
    if (!keys.length) {
      throw new Error(`Cannot encode segment with no attributes (segment:${segmentName})`);
    }
    const roles: Record<string, GlyphVector> = {};
    const roleValues: Record<string, unknown> = {};
    const bindings: GlyphVector[] = [];
    for (const roleName of keys) {
      const value = attributes[roleName];
      const bound = this.bind(this.generateSymbol(roleName), this.generateSymbol(String(value)));
      roles[roleName] = bound;
      roleValues[roleName] = value;
      bindings.push(bound);
    }
    return { name: segmentName, cortex: this.bundle(bindings), roles, roleValues, weights: weights ?? {} };
  }

  mergeSegments(segments: Segment[], weights?: number[]): GlyphVector {
    if (!segments.length) throw new Error("Cannot merge empty segment list");
    return this.bundle(segments.map((s) => s.cortex), weights);
  }

  private createTemporalLayer(conc: Concept, timestamp: Date): { layer: Layer; temporalValue: string } {
    let temporalValue: string;
    const source = this.config.temporalSource;
    if (source === "auto") {
      temporalValue = isoLikePython(timestamp);
    } else {
      const parts = source.split(".");
      if (parts.length === 3) {
        const roleName = parts[2]!;
        const v = conc.attributes[roleName];
        temporalValue = v != null ? this.formatTemporalValue(v) : isoLikePython(timestamp);
      } else {
        temporalValue = isoLikePython(timestamp);
      }
    }
    const binding = this.bind(this.generateSymbol(TEMPORAL_ROLE_NAME), this.generateSymbol(String(temporalValue)));
    const segment: Segment = {
      name: TEMPORAL_SEGMENT_NAME,
      cortex: binding,
      roles: { [TEMPORAL_ROLE_NAME]: binding },
      roleValues: { [TEMPORAL_ROLE_NAME]: temporalValue },
      weights: { similarity: 1.0, security: 1.0 },
    };
    const layer: Layer = {
      name: TEMPORAL_LAYER_NAME,
      cortex: binding,
      segments: { [TEMPORAL_SEGMENT_NAME]: segment },
      weights: { similarity: 1.0, security: 1.0 },
    };
    return { layer, temporalValue };
  }

  /** _format_temporal_value, verbatim quirks included: the DEFAULT TemporalConfig
   *  is signal_type "auto", which DISCARDS the role's value and stamps now() —
   *  pin signalType (sequence/version) to carry the role value through. The
   *  datetime/epoch arms parse-and-reformat; failures fall back to now(). */
  private formatTemporalValue(value: unknown): string {
    const cfg = this.config.temporalConfig ?? { signalType: "auto" as const };
    try {
      switch (cfg.signalType) {
        case "auto": return isoLikePython(this.now());
        case "datetime": {
          // The canon strptime-parses with cfg.format then isoformat()s. The
          // port supports the ISO-shaped formats the fact substrate uses; an
          // unparseable value falls back to now(), like the canon.
          const d = new Date(String(value));
          if (Number.isNaN(d.getTime())) throw new Error("unparseable");
          return isoLikePython(d);
        }
        case "epoch": {
          const n = Number(value);
          if (!Number.isFinite(n)) throw new Error("unparseable");
          return isoLikePython(new Date(n * 1000));
        }
        case "sequence": return String(value);
        case "version": return String(value);
        default: return String(value);
      }
    } catch {
      return isoLikePython(this.now());
    }
  }

  private buildIdentifier(conc: Concept, timestamp: Date, temporalValue: string | null): string {
    const keyParts: string[] = [];
    for (const layer of this.config.layers) {
      for (const segment of layer.segments) {
        for (const role of segment.roles) {
          if (role.keyPart && role.name in conc.attributes) {
            keyParts.push(sanitizeIdPart(String(conc.attributes[role.name])));
          }
        }
      }
    }
    const primaryKey = keyParts.length ? keyParts.join("_") : sanitizeIdPart(conc.name);
    const temporal = temporalValue ?? isoLikePython(timestamp);
    return `${primaryKey}@${temporal}#v1`;
  }

  encode(conc: Concept, layerConfig?: { layers: LegacyLayerSpec[] }): Glyph {
    return this.config.layers.length
      ? this.encodeWithExplicitConfig(conc)
      : this.encodeLegacy(conc, layerConfig);
  }

  private encodeLegacy(conc: Concept, layerConfig?: { layers: LegacyLayerSpec[] }): Glyph {
    const cfg = layerConfig ?? DEFAULT_LEGACY;
    const layers: Record<string, Layer> = {};
    const layerCortices: GlyphVector[] = [];

    for (const spec of cfg.layers) {
      const segments: Record<string, Segment> = {};
      const segmentList: Segment[] = [];
      for (const segSpec of spec.segments) {
        let data: Record<string, unknown>;
        if (segSpec.source === "attributes") data = conc.attributes;
        else if (segSpec.source === "relationships") data = Object.fromEntries(conc.relationships);
        else throw new Error(`Unknown segment source: ${String(segSpec.source)}`);
        if (!Object.keys(data).length) continue;
        const segment = this.encodeSegment(segSpec.name, data);
        segments[segSpec.name] = segment;
        segmentList.push(segment);
      }
      if (!segmentList.length) continue;
      const layer: Layer = { name: spec.name, cortex: this.mergeSegments(segmentList), segments, weights: {} };
      layers[spec.name] = layer;
      layerCortices.push(layer.cortex);
    }

    if (!layerCortices.length) {
      throw new Error(`Cannot encode concept with no layers (${conc.name}: no attributes or relationships)`);
    }

    const timestamp = this.now();
    let temporalValue: string;
    if (this.config.includeTemporal) {
      const t = this.createTemporalLayer(conc, timestamp);
      layers[t.layer.name] = t.layer;
      layerCortices.push(t.layer.cortex);
      temporalValue = t.temporalValue;
    } else {
      temporalValue = isoLikePython(timestamp);
    }

    const glyph: Glyph = {
      identifier: this.buildIdentifier(conc, timestamp, temporalValue),
      name: conc.name,
      spaceId: this.spaceId,
      globalCortex: this.bundle(layerCortices),
      layers,
      securityLevels: {},
      metadata: conc.metadata,
      timestamp: isoLikePython(timestamp),
      version: "v1",
    };
    validateGlyph(glyph);
    return glyph;
  }

  private encodeWithExplicitConfig(conc: Concept): Glyph {
    const timestamp = this.now();
    const useWeighted = this.config.applyWeightsDuringEncoding;
    const layers: Record<string, Layer> = {};
    const layerCortices: GlyphVector[] = [];
    const layerWeights: number[] = [];

    for (const layerDef of this.config.layers) {
      const segments: Record<string, Segment> = {};
      const segmentCortices: GlyphVector[] = [];
      const segmentWeights: number[] = [];

      for (const segmentDef of layerDef.segments) {
        const roles: Record<string, GlyphVector> = {};
        const roleValues: Record<string, unknown> = {};
        const roleBindings: GlyphVector[] = [];
        const roleWeights: number[] = [];

        for (const roleDef of segmentDef.roles) {
          if (!(roleDef.name in conc.attributes)) continue;
          const value = conc.attributes[roleDef.name];
          if (roleDef.continuousConfig != null) {
            throw new Error(`role '${roleDef.name}': continuous encoding is not ported — the NSM fact substrate is symbolic (see config.ts)`);
          }
          if (roleDef.numericConfig != null) {
            throw new Error(`role '${roleDef.name}': numeric encoding is not ported — the NSM fact substrate is symbolic (see config.ts)`);
          }
          if (roleDef.textEncoding === "bag_of_words") {
            throw new Error(`role '${roleDef.name}': bag_of_words encoding is not ported — NSM's closed vocabulary replaces fuzzy text matching`);
          }
          const binding = this.bind(this.generateSymbol(roleDef.name), this.generateSymbol(String(value)));
          roles[roleDef.name] = binding;
          roleValues[roleDef.name] = value;
          roleBindings.push(binding);
          roleWeights.push(roleDef.similarityWeight);
        }

        if (!roleBindings.length) continue;
        const segmentCortex = useWeighted
          ? this.weightedBundle(roleBindings.map((b, i) => [b, roleWeights[i]!]))
          : this.bundle(roleBindings);
        const segment: Segment = {
          name: segmentDef.name,
          cortex: segmentCortex,
          roles,
          roleValues,
          weights: { similarity: segmentDef.similarityWeight, security: segmentDef.securityWeight },
        };
        segments[segmentDef.name] = segment;
        segmentCortices.push(segmentCortex);
        segmentWeights.push(segmentDef.similarityWeight);
      }

      if (!segmentCortices.length) continue;
      const layerCortex = useWeighted
        ? this.weightedBundle(segmentCortices.map((c, i) => [c, segmentWeights[i]!]))
        : this.bundle(segmentCortices);
      const layer: Layer = {
        name: layerDef.name,
        cortex: layerCortex,
        segments,
        weights: { similarity: layerDef.similarityWeight, security: layerDef.securityWeight },
      };
      layers[layerDef.name] = layer;
      layerCortices.push(layerCortex);
      layerWeights.push(layerDef.similarityWeight);
    }

    if (!layerCortices.length) {
      throw new Error(`Cannot encode concept: no attributes match defined roles (${conc.name})`);
    }

    let temporalValue: string;
    if (this.config.includeTemporal) {
      const t = this.createTemporalLayer(conc, timestamp);
      layers[t.layer.name] = t.layer;
      layerCortices.push(t.layer.cortex);
      layerWeights.push(1.0);
      temporalValue = t.temporalValue;
    } else {
      temporalValue = isoLikePython(timestamp);
    }

    const globalCortex = useWeighted
      ? this.weightedBundle(layerCortices.map((c, i) => [c, layerWeights[i]!]))
      : this.bundle(layerCortices);

    const glyph: Glyph = {
      identifier: this.buildIdentifier(conc, timestamp, temporalValue),
      name: conc.name,
      spaceId: this.spaceId,
      globalCortex,
      layers,
      securityLevels: { cortex: this.config.securityWeight },
      metadata: conc.metadata,
      timestamp: isoLikePython(timestamp),
      version: "v1",
    };
    validateGlyph(glyph);
    return glyph;
  }
}

/** Identifier sanitation, verbatim: spaces/@/# become underscores. */
function sanitizeIdPart(v: string): string {
  return v.replace(/ /g, "_").replace(/@/g, "_").replace(/#/g, "_");
}

/** Python `datetime.now().isoformat()`: microsecond precision, no zone,
 *  seconds omitted-microseconds only when exactly zero (we always carry them,
 *  matching the canon's typical stamps). */
export function isoLikePython(d: Date): string {
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  const micros = d.getMilliseconds() * 1000;
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(micros, 6)}`
  );
}

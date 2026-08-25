/**
 * config.ts — encoder configuration. FAITHFUL PORT of the encode-relevant
 * surface of `glyphh/core/config.py` (glyphh-ai/ada @ 041c0a20).
 *
 * SPACE-ID CRITICAL: `toJson()` must reproduce Python's
 * `json.dumps(self.to_dict(), sort_keys=True)` BYTE-FOR-BYTE — the string is
 * hashed into the space_id, so a single space or float rendering difference
 * forks the vector space. The oracle suite asserts the default config's JSON
 * and space_id against the vendored canon.
 *
 * DEFERRED (loudly, not silently): numeric/continuous role encodings, NL and
 * GQL pattern configs, and bag-of-words text encoding — those arms of the
 * canon depend on the CharacterEncoder/morphology stack and are not needed by
 * the NSM fact substrate (a closed filler vocabulary replaces fuzzy text
 * matching by design). Setting any of them here throws.
 */

export interface TemporalConfig {
  /** auto | datetime | epoch | sequence | version — the canon's signal types. */
  signalType: "auto" | "datetime" | "epoch" | "sequence" | "version";
  /** strptime format, required when signalType === "datetime". */
  format?: string | null;
  autoIncrement?: boolean;
}

export const TEMPORAL_LAYER_NAME = "_temporal";
export const TEMPORAL_SEGMENT_NAME = "signal";
export const TEMPORAL_ROLE_NAME = "value";

export interface RoleConfig {
  name: string;
  similarityWeight: number;
  securityWeight: number;
  /** Part of the composite identifier's primary key. */
  keyPart: boolean;
  /** DEFERRED arms — present for config fidelity, refused at encode. */
  textEncoding?: "symbolic" | "bag_of_words";
  numericConfig?: unknown;
  continuousConfig?: unknown;
}

export interface SegmentConfig {
  name: string;
  roles: RoleConfig[];
  similarityWeight: number;
  securityWeight: number;
}

export interface LayerConfig {
  name: string;
  segments: SegmentConfig[];
  similarityWeight: number;
  securityWeight: number;
}

export interface EncoderConfigInput {
  dimension: number;
  seed: number;
  similarityWeight?: number;
  securityWeight?: number;
  applyWeightsDuringEncoding?: boolean;
  includeTemporal?: boolean;
  temporalSource?: string;
  temporalConfig?: TemporalConfig | null;
  layers?: LayerConfig[];
}

export class EncoderConfig {
  readonly dimension: number;
  readonly seed: number;
  readonly similarityWeight: number;
  readonly securityWeight: number;
  readonly applyWeightsDuringEncoding: boolean;
  readonly includeTemporal: boolean;
  readonly temporalSource: string;
  readonly temporalConfig: TemporalConfig | null;
  readonly layers: LayerConfig[];

  constructor(input: EncoderConfigInput) {
    this.dimension = input.dimension;
    this.seed = input.seed;
    this.similarityWeight = input.similarityWeight ?? 1.0;
    this.securityWeight = input.securityWeight ?? 1.0;
    this.applyWeightsDuringEncoding = input.applyWeightsDuringEncoding ?? false;
    this.includeTemporal = input.includeTemporal ?? true;
    this.temporalSource = input.temporalSource ?? "auto";
    this.temporalConfig = input.temporalConfig ?? null;
    this.layers = input.layers ?? [];
  }

  /** Python float rendering: integral floats print with a trailing `.0`. */
  private static pyFloat(n: number): string {
    return Number.isInteger(n) ? `${n}.0` : `${n}`;
  }

  /** `json.dumps(self.to_dict(), sort_keys=True)` byte-for-byte: sorted keys,
   *  `", "` and `": "` separators, Python bool/float renderings. Built by
   *  hand rather than JSON.stringify so float formatting matches. */
  toJson(): string {
    const layersJson = `[${this.layers.map((l) => EncoderConfig.layerJson(l)).join(", ")}]`;
    return (
      `{"apply_weights_during_encoding": ${this.applyWeightsDuringEncoding}, ` +
      `"dimension": ${this.dimension}, ` +
      `"include_temporal": ${this.includeTemporal}, ` +
      `"layers": ${layersJson}, ` +
      `"security_weight": ${EncoderConfig.pyFloat(this.securityWeight)}, ` +
      `"seed": ${this.seed}, ` +
      `"similarity_weight": ${EncoderConfig.pyFloat(this.similarityWeight)}, ` +
      (this.temporalConfig
        ? `"temporal_config": {"auto_increment": ${this.temporalConfig.autoIncrement ?? true}, ` +
          `"format": ${this.temporalConfig.format == null ? "null" : JSON.stringify(this.temporalConfig.format)}, ` +
          `"signal_type": ${JSON.stringify(this.temporalConfig.signalType)}}, `
        : "") +
      `"temporal_source": ${JSON.stringify(this.temporalSource)}}`
    );
  }

  private static layerJson(l: LayerConfig): string {
    const segs = `[${l.segments.map((s) => EncoderConfig.segmentJson(s)).join(", ")}]`;
    return (
      `{"name": ${JSON.stringify(l.name)}, ` +
      `"security_weight": ${EncoderConfig.pyFloat(l.securityWeight)}, ` +
      `"segments": ${segs}, ` +
      `"similarity_weight": ${EncoderConfig.pyFloat(l.similarityWeight)}}`
    );
  }

  private static segmentJson(s: SegmentConfig): string {
    const roles = `[${s.roles.map((r) => EncoderConfig.roleJson(r)).join(", ")}]`;
    return (
      `{"name": ${JSON.stringify(s.name)}, ` +
      `"roles": ${roles}, ` +
      `"security_weight": ${EncoderConfig.pyFloat(s.securityWeight)}, ` +
      `"similarity_weight": ${EncoderConfig.pyFloat(s.similarityWeight)}}`
    );
  }

  private static roleJson(r: RoleConfig): string {
    return (
      `{"key_part": ${r.keyPart}, ` +
      `"name": ${JSON.stringify(r.name)}, ` +
      `"security_weight": ${EncoderConfig.pyFloat(r.securityWeight)}, ` +
      `"similarity_weight": ${EncoderConfig.pyFloat(r.similarityWeight)}}`
    );
  }
}

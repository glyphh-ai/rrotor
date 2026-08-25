/**
 * fact-tree.ts — the auditable FactTree. FAITHFUL PORT of
 * `glyphh/fact_tree/builder.py` (glyphh-ai/ada @ 041c0a20).
 *
 * Every node carries citations back to source glyphs — `glyph_id` in the
 * stamped `primary_key@timestamp#version` form, component-addressed down to
 * cortex/layer/segment/role — plus optional data samples, math explanations,
 * and context. `toJson()` and `toText()` reproduce the canon's renderings.
 */

export interface Citation {
  glyphId: string;
  component: string;
  /** ISO timestamp. */
  timestamp: string;
  version: string;
  dataHash: string;
}

const citationToDict = (c: Citation): Record<string, unknown> => ({
  glyph_id: c.glyphId,
  component: c.component,
  timestamp: c.timestamp,
  version: c.version,
  data_hash: c.dataHash,
});

export class FactNode {
  description: string;
  value: unknown;
  children: FactNode[] = [];
  citations: Citation[] = [];
  dataSample: unknown = null;
  mathExplanation: string | null = null;
  dataContext: Record<string, unknown> = {};

  constructor(description: string, value: unknown = null) {
    this.description = description;
    this.value = value;
  }

  toDict(): Record<string, unknown> {
    const result: Record<string, unknown> = {
      description: this.description,
      value: this.value ?? null,
      children: this.children.map((c) => c.toDict()),
      citations: this.citations.map(citationToDict),
      data_context: this.dataContext,
    };
    if (this.dataSample != null) result.data_sample = this.dataSample;
    if (this.mathExplanation != null) result.math_explanation = this.mathExplanation;
    return result;
  }

  toText(indent = 0): string {
    const lines: string[] = [];
    const prefix = "  ".repeat(indent);
    if (this.value != null) lines.push(`${prefix}+- ${this.description}: ${formatValue(this.value)}`);
    else lines.push(`${prefix}+- ${this.description}`);
    if (this.mathExplanation) lines.push(`${prefix}|  +- Formula: ${this.mathExplanation}`);
    if (this.dataSample != null) lines.push(`${prefix}|  +- Data: ${formatValue(this.dataSample)}`);
    if (Object.keys(this.dataContext).length) lines.push(`${prefix}|  +- Context: ${formatValue(this.dataContext)}`);
    if (this.citations.length) {
      lines.push(`${prefix}|  +- Citations:`);
      for (const c of this.citations) {
        lines.push(`${prefix}|     +- ${c.glyphId}/${c.component} (hash: ${c.dataHash.slice(0, 8)}...)`);
      }
    }
    for (const child of this.children) lines.push(child.toText(indent + 1));
    return lines.join("\n");
  }
}

function formatValue(value: unknown): string {
  if (typeof value === "number" && !Number.isInteger(value)) return value.toFixed(4);
  if (Array.isArray(value)) {
    const shown = value.length > 5 ? value.slice(0, 5) : value;
    const inner = shown.map(formatValue).join(", ");
    return value.length > 5 ? `[${inner}, ...]` : `[${inner}]`;
  }
  if (value && typeof value === "object") {
    const items = Object.entries(value as Record<string, unknown>).map(([k, v]) => `${k}=${formatValue(v)}`);
    return `{${items.join(", ")}}`;
  }
  return String(value);
}

export class FactTree {
  readonly root: FactNode;

  constructor(rootDescription = "Similarity Computation") {
    this.root = new FactNode(rootDescription, null);
  }

  /** Add a fact at `path` — intermediate nodes are created on demand; the
   *  path element matches a child by the first `:`-delimited word of its
   *  description (the canon's navigation rule, verbatim). */
  addFact(input: {
    path: string[];
    description: string;
    value: unknown;
    citations?: Citation[];
    dataSample?: unknown;
    mathExplanation?: string;
    dataContext?: Record<string, unknown>;
  }): void {
    let current = this.root;
    const { path } = input;
    for (let i = 0; i < path.length; i++) {
      const element = path[i]!;
      const found = current.children.find((c) => (c.description.split(":")[0] ?? "").trim() === element) ?? null;
      if (i === path.length - 1) {
        const node = new FactNode(input.description, input.value);
        node.citations = input.citations ?? [];
        node.dataSample = input.dataSample ?? null;
        node.mathExplanation = input.mathExplanation ?? null;
        node.dataContext = input.dataContext ?? {};
        current.children.push(node);
      } else if (found === null) {
        const intermediate = new FactNode(element, null);
        current.children.push(intermediate);
        current = intermediate;
      } else {
        current = found;
      }
    }
  }

  toJson(): Record<string, unknown> {
    return this.root.toDict();
  }

  toText(indent = 0): string {
    const lines = [this.root.description];
    for (const child of this.root.children) lines.push(child.toText(indent));
    return lines.join("\n");
  }
}

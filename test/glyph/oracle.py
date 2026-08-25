#!/usr/bin/env python3
"""
oracle.py — emits the CANON's ground truth for the TS glyph port to match.

Runs the vendored ada/runtime code (glyphh/glyphh — verbatim, provenance in
its __init__) over fixed fixtures and prints one JSON document. The vitest
oracle suite runs this and asserts the TS port reproduces every byte:
atoms, space ids, config JSON, per-level cortices, identifiers (shape),
fact-tree renderings. Dev-time only — production ships no Python.
"""

import base64
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "glyphh"))

from glyphh.core.ops import bind, bundle, cosine_similarity, generate_symbol
from glyphh.core.config import EncoderConfig, Layer, Segment, Role, TemporalConfig
from glyphh.core.types import Concept
from glyphh.encoder.base import Encoder
from glyphh.fact_tree.builder import FactTree, Citation

b64 = lambda v: base64.b64encode(v.astype("int8").tobytes()).decode()

out = {}

# ── atoms ──
out["atoms"] = {
    key: b64(generate_symbol(42, key, 256))
    for key in ["color", "red", "entity.kind", "GOOD", "chris", "value", "2026-01-01T00:00:00"]
}

# ── ops on known atoms ──
a = generate_symbol(42, "a", 256); b = generate_symbol(42, "b", 256); c = generate_symbol(42, "c", 256)
out["ops"] = {
    "bind_ab": b64(bind(a, b)),
    "bundle_abc": b64(bundle([a, b, c])),
    "cos_ab": cosine_similarity(a, b),
}

# ── default config: json + space id ──
cfg = EncoderConfig(dimension=256, seed=42)
enc = Encoder(cfg)
out["default_config_json"] = cfg.to_json()
out["default_space_id"] = enc.space_id

# ── explicit config: json + space id ──
cfg2 = EncoderConfig(dimension=256, seed=42, layers=[
    Layer(name="semantic", segments=[
        Segment(name="attributes", roles=[
            Role(name="kind", key_part=True), Role(name="subject"), Role(name="value", similarity_weight=0.5),
        ]),
    ]),
])
enc2 = Encoder(cfg2)
out["explicit_config_json"] = cfg2.to_json()
out["explicit_space_id"] = enc2.space_id

# ── legacy encode (temporal pinned via monkeypatched datetime? — instead:
#    include_temporal=False for a deterministic glyph, temporal exercised
#    separately with a pinned source role) ──
cfg3 = EncoderConfig(dimension=256, seed=42, include_temporal=False)
enc3 = Encoder(cfg3)
g = enc3.encode(Concept(
    name="deploy rule",
    attributes={"kind": "preference", "subject": "deploys", "value": "through ci"},
    relationships=[("holder", "chris"), ("applies_to", "server")],
    metadata={"domain": "ops"},
))
out["legacy_glyph"] = {
    "space_id": g.space_id,
    "identifier_prefix": g.identifier.split("@")[0],
    "version": g.version,
    "global_cortex": b64(g.global_cortex.data),
    "layers": {
        ln: {
            "cortex": b64(layer.cortex.data),
            "segments": {
                sn: {"cortex": b64(seg.cortex.data), "roles": {rn: b64(rv.data) for rn, rv in seg.roles.items()}}
                for sn, seg in layer.segments.items()
            },
        }
        for ln, layer in g.layers.items()
    },
}

# ── temporal layer pinned through a source role (deterministic) ──
cfg4 = EncoderConfig(dimension=256, seed=42, temporal_source="semantic.attributes.when", temporal_config=TemporalConfig(signal_type="sequence"))
enc4 = Encoder(cfg4)
g4 = enc4.encode(Concept(name="t", attributes={"kind": "event", "when": "2026-01-01T00:00:00"}, relationships=[], metadata={}))
out["temporal_glyph"] = {
    "identifier": g4.identifier,
    "global_cortex": b64(g4.global_cortex.data),
    "temporal_cortex": b64(g4.layers["_temporal"].cortex.data),
}

# ── explicit-config encode with weights baked in ──
cfg5 = EncoderConfig(dimension=256, seed=42, include_temporal=False, apply_weights_during_encoding=True, layers=[
    Layer(name="semantic", segments=[
        Segment(name="attributes", roles=[
            Role(name="kind", key_part=True), Role(name="subject", similarity_weight=0.25),
        ]),
    ]),
])
g5 = Encoder(cfg5).encode(Concept(name="w", attributes={"kind": "k1", "subject": "s1"}, relationships=[], metadata={}))
out["weighted_glyph"] = {
    "identifier_prefix": g5.identifier.split("@")[0],
    "global_cortex": b64(g5.global_cortex.data),
}

# ── fact tree renderings ──
t = FactTree()
t.add_fact(
    path=["computation", "raw_similarity"],
    description="Raw Similarity",
    value=0.87,
    citations=[Citation(glyph_id="car_red@2024-01-15T10:30:00Z#v1", component="cortex",
                        timestamp=datetime(2024, 1, 15, 10, 30), version="v1", data_hash="a3f2e8b1")],
    data_sample={"dot_product": 8700, "dimensions_compared": 256},
    math_explanation="cos(v1, v2) = (v1 . v2) / (||v1|| * ||v2||)",
)
t.add_fact(path=["computation", "verdict"], description="Verdict", value="same fact")
out["fact_tree_json"] = t.to_json()
out["fact_tree_text"] = t.to_text()

print(json.dumps(out))

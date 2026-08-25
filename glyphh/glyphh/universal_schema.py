"""
The universal schema — every fact maps into one fixed shape.

Instead of growing ad-hoc predicates ("favorite_color", "has_dream_loop",
"quantity_of_bones") as an LLM invents them, every fact gets MAPPED into
a fixed 7-layer lattice. The substrate stays stable; structured queries
(count / distribution / intersection) address facts by layer.role slot.

LAYERS (fixed):
    entity        — what kind of thing is this
    perceptual    — color / size / shape / sound / texture / temperature
    spatial       — location / origin / direction
    temporal      — time / duration / age / era / frequency
    relational    — subject / predicate / object / possessor / agent
    quantitative  — count / magnitude / unit
    epistemic     — source / certainty / modality

Every fact fills SOME of these slots (the rest are absent). An absent
slot is a structural ∅ — the substrate refuses rather than confabulates.

This is the canonical, shared schema imported by both the rotor engine and
the memory substrate: the HDC encoder binds role atoms named "layer.role"
against filler atoms, so the schema below IS the roles_config that seeds
`compute_space_id`. Keep it stable — changing a role name changes the space.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ── The universal schema (7 layers × 33 roles) ───────────────────────

UNIVERSAL_SCHEMA: dict[str, list[str]] = {
    "entity":       ["name", "kind", "subkind"],
    "perceptual":   ["color", "size", "shape", "texture", "sound",
                     "smell", "taste", "temperature"],
    "spatial":      ["location", "origin", "direction"],
    "temporal":     ["time", "duration", "age", "era", "frequency"],
    "relational":   ["subject", "predicate", "object", "possessor",
                     "agent", "patient", "instrument"],
    "quantitative": ["count", "magnitude", "unit", "ratio"],
    "epistemic":    ["source", "certainty", "modality"],
}

ALL_LAYERS = list(UNIVERSAL_SCHEMA)
ALL_ROLES = [(layer, role) for layer, roles in UNIVERSAL_SCHEMA.items() for role in roles]


def roles_config() -> dict[str, list[str]]:
    """The schema as the deterministic roles_config that seeds a space_id.

    A copy, so callers cannot mutate the module-level schema in place.
    """
    return {layer: list(roles) for layer, roles in UNIVERSAL_SCHEMA.items()}


def _sanitize_universal(facts: dict) -> dict[str, dict[str, str]]:
    """Keep only schema-valid layers/roles with non-empty values.

    Deterministic, no model call. Drops anything an enricher may have
    hallucinated that is not in the fixed schema, and normalizes values to
    stripped strings — refusing empty / ``none`` / ``null`` / ``n/a`` fills
    rather than storing a confabulated ∅.
    """
    clean: dict[str, dict[str, str]] = {}
    if not isinstance(facts, dict):
        return clean
    for layer, roles in facts.items():
        if layer not in UNIVERSAL_SCHEMA:
            continue
        if not isinstance(roles, dict):
            continue  # malformed layer (e.g. a list) — drop it
        kept: dict[str, str] = {}
        for role, value in (roles or {}).items():
            if role not in UNIVERSAL_SCHEMA[layer]:
                continue
            if value is None:
                continue
            v = str(value).strip()
            if not v or v.lower() in ("none", "null", "n/a"):
                continue
            kept[role] = v
        if kept:
            clean[layer] = kept
    return clean


def universal_role_fillers(facts: dict) -> list[tuple[str, str]]:
    """Flatten a sanitized universal fact into ``(layer.role, value)`` pairs.

    The ``layer.role`` string is exactly the role-atom name the HDC encoder
    binds against (see ``hdc.Encoder.encode``), so this is the bridge from a
    structured fact to a hypervector.
    """
    clean = _sanitize_universal(facts)
    return [
        (f"{layer}.{role}", value)
        for layer, roles in clean.items()
        for role, value in roles.items()
    ]

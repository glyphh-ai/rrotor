"""
glyphh — the canonical Glyph substrate, VENDORED VERBATIM into rrotor.

Source of truth (do not edit these modules here — fix upstream and re-vendor):
    glyphh-ai/ada     @ 041c0a20b4b4269f59ef70cbfab3dcb662f3431e
        core/{types,ops,config}.py   Vector/Concept/Segment/Layer/Glyph + algebra
        encoder/*                    the strict-contract Glyph constructor
        fact_tree/*                  the auditable FactTree (Citations carry
                                     glyph_id@timestamp#version, component-addressed)
        exceptions.py, licensing.py
    glyphh-ai/runtime @ 011f0b3b7b9a9d66328a918b11a6c0a1e6e81df0
        universal_schema.py          the universal 7-layer x 33-role schema

ONLY this __init__ differs from upstream: ada's package init imports the whole
assistant SDK (edges, gql, visualization, ...) which the fact substrate does
not need. The modules below are byte-identical to their sources.
"""

from glyphh.core.types import Vector, Concept, Segment, Layer, Glyph  # noqa: F401
from glyphh.core import ops  # noqa: F401
from glyphh.fact_tree.builder import FactTree, FactNode, Citation  # noqa: F401
